"""Render a validated contract into standard SVA and Yosys monitor RTL.

Two artifacts are produced from the same contract:

* ``fv_sva_checker.sv`` -- portable SystemVerilog assertions bound to the DUT.
  Commercial tools run this file.  It uses standard SVA temporal operators.
* ``generated/`` -- Yosys-compatible immediate-assertion monitors and one
  wrapper top per property.  Yosys cannot elaborate ``assert property`` with
  the temporal subset used here, so the monitors implement the temporal
  semantics explicitly with history/delay registers.

The monitor style is deliberately simple and uniform:

* every monitor has a free input ``fv_start`` controlled by the runner with
  ``sat -set-at``; it is 0 on the first state and 1 afterwards.  This removes
  all dependence on the unconstrained initial value of monitor flops.
* history registers are unconditional; they are only read after ``fv_start``
  is high, so there is no need to reset them.
* trigger/delay registers are gated by the armed signal, and therefore flush
  during reset before any check can observe them.

Only top-level ports and resolved parameters are in scope, so the renderer is
design-independent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    project_root,
    rel_to,
    sha256_file,
    source_files,
    write_json,
    write_text,
)
from fv_contract import (
    HISTORY_FUNCS,
    Validation,
    arg_type,
    find_calls,
    map_calls,
    validate_design,
)

RENDER_SCHEMA_VERSION = "1.0"


# -- small helpers -------------------------------------------------------------------------------

def _hist_key(arg: str) -> str:
    return re.sub(r"\s+", "", arg or "")


def _sanitize_ident(name: str, upper: bool = False) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
    if not s:
        s = "x"
    if s[0].isdigit():
        s = "n_" + s
    return s.upper() if upper else s.lower()


def _unique(name: str, used: set[str]) -> str:
    base = name
    i = 2
    while name in used:
        name = f"{base}_{i}"
        i += 1
    used.add(name)
    return name


def _port_decl(port: dict[str, Any], direction: str, name: str | None = None) -> str:
    dims = " ".join((port.get("dims") or []))
    signed = " signed" if port.get("signed") else ""
    return f"{direction} logic{signed} {dims + ' ' if dims else ''}{name or port['name']}"


def _wire_decl(port: dict[str, Any]) -> str:
    dims = " ".join((port.get("dims") or []))
    signed = " signed" if port.get("signed") else ""
    # Keep DUT outputs that this property does not reference: they are shown in
    # counterexamples, and Yosys would otherwise delete the unused wire.
    return f"(* keep *) logic{signed} {dims + ' ' if dims else ''}{port['name']};"


def _check_interface(interface: dict[str, Any]) -> None:
    top = interface.get("top") or "<top>"
    for p in interface.get("ports") or []:
        name = str(p.get("name") or "")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
            raise FvError(
                f"top {top}: port {name!r} is not a simple identifier; escaped or "
                "non-ANSI port names are not supported by the generated harness"
            )
        if p.get("direction") == "inout":
            raise FvError(
                f"top {top}: inout port {name!r} is not supported by the formal harness; "
                "split it into input and output ports or add a wrapper"
            )
        if p.get("unpacked_dims"):
            raise FvError(
                f"top {top}: port {name!r} has unpacked dimensions "
                f"{p.get('unpacked_dims')}; only packed ports are supported"
            )
        if not p.get("dims") and int(p.get("width") or 1) > 1:
            raise FvError(
                f"top {top}: port {name!r} has width {p.get('width')} but no packed "
                "dimensions; re-run `fv.py prepare` after fixing the interface extraction"
            )
        if name in ("fv_start", "fv_cover") or name.startswith("fv_"):
            raise FvError(
                f"top {top}: port {name!r} collides with the generated harness prefix 'fv_'"
            )


def _resolve_named(obj_map: list[dict[str, Any]], key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    for obj in obj_map:
        if str(obj.get("name") or "") == key or str(obj.get("signal") or "") == key:
            return obj
    return None


def _design_clock(design: dict[str, Any], prop: dict[str, Any] | None = None) -> tuple[str, str]:
    clock = design.get("clock") or {}
    if prop and prop.get("clock"):
        resolved = _resolve_named(list(design.get("clocks") or []), str(prop.get("clock")))
        if resolved:
            clock = resolved
        else:
            clock = {**clock, "signal": str(prop["clock"])}
    signal = str(clock.get("signal") or "")
    edge = str(clock.get("edge") or "posedge")
    if not signal:
        raise FvError("contract design.clock.signal is empty")
    if edge not in ("posedge", "negedge"):
        raise FvError(f"unsupported clock edge {edge!r}")
    return signal, edge


def _design_reset(design: dict[str, Any], prop: dict[str, Any] | None = None) -> tuple[str, str, str]:
    reset = design.get("reset") or {}
    if prop and prop.get("reset"):
        resolved = _resolve_named(list(design.get("resets") or []), str(prop.get("reset")))
        if resolved:
            reset = resolved
        else:
            reset = {**reset, "signal": str(prop["reset"])}
    signal = str(reset.get("signal") or "")
    active = str(reset.get("active") or "low")
    kind = str(reset.get("kind") or "unknown")
    if not signal:
        raise FvError("contract design.reset.signal is empty; the formal harness needs a reset")
    if active not in ("low", "high"):
        raise FvError(f"unsupported reset polarity {active!r}")
    return signal, active, kind


def _reset_active_expr(design: dict[str, Any], prop: dict[str, Any] | None = None) -> str:
    signal, active, _kind = _design_reset(design, prop)
    return f"!{signal}" if active == "low" else signal


def _signal_info(signal: str, interface: dict[str, Any], where: str) -> dict[str, Any]:
    v = Validation()
    info = arg_type(signal, interface, v, where)
    if info is None or v.errors:
        detail = "; ".join(str(e) for e in v.errors) or "unknown signal"
        raise FvError(f"{where}: {detail}")
    return info


def _collect_history(expressions: list[str], interface: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for expr in expressions:
        if not expr:
            continue
        for name, arg, _start, _end in find_calls(expr):
            if name not in HISTORY_FUNCS:
                continue
            key = _hist_key(arg)
            if key in entries:
                continue
            info = _signal_info(arg, interface, f"history argument {name}({arg})")
            ident = _unique("fv_prev_" + _sanitize_ident(arg), used)
            entries[key] = {
                "arg": arg.strip(),
                "dims": list(info.get("dims") or []),
                "width": int(info.get("width") or 1),
                "signed": bool(info.get("signed")),
                "name": ident,
            }
    return entries


def _history_decl(entry: dict[str, Any]) -> str:
    dims = " ".join(entry.get("dims") or [])
    if not dims and int(entry.get("width") or 1) > 1:
        dims = f"[{int(entry['width']) - 1}:0]"
    signed = " signed" if entry.get("signed") else ""
    decl = f"  logic{signed} {dims + ' ' if dims else ''}{entry['name']};"
    init = f"  initial {entry['name']} = '0;"
    return decl + "\n" + init


def _history_block(history: dict[str, dict[str, Any]], clock: str, edge: str,
                   arm: str | None = None) -> str:
    if not history:
        return ""
    lines = [f"  always_ff @({edge} {clock}) begin"]
    if arm:
        lines.append(f"    if ({arm}) begin")
        for entry in history.values():
            lines.append(f"      {entry['name']} <= {entry['arg']};")
        lines.append("    end")
    else:
        for entry in history.values():
            lines.append(f"    {entry['name']} <= {entry['arg']};")
    lines.append("  end")
    return "\n".join(lines)


def substitute_params(expr: str, params: dict[str, Any] | None) -> str:
    """Replace resolved parameter identifiers with their integer values.

    The contract may use top-level parameters (``WIDTH - 1``); the generated
    monitor and checker modules have no parameter declarations, so the values
    must be baked in exactly as the DUT is elaborated.
    """
    if not params:
        return expr

    def repl(match: re.Match) -> str:
        name = match.group(0)
        if name in params:
            return str(int(params[name]))
        return name

    return re.sub(r"(?<![A-Za-z0-9_$\\])[A-Za-z_][A-Za-z0-9_$]*", repl, expr)


def map_expr(expr: str, history: dict[str, dict[str, Any]], mode: str = "monitor",
             params: dict[str, Any] | None = None) -> str:
    """Map the portable functions to monitor RTL or standard SVA."""
    expr = substitute_params(expr, params)
    def mapper(name: str, raw: str, mapped: str) -> str:
        key = _hist_key(raw)
        entry = history.get(key)
        if mode == "sva":
            if name == "prev":
                return f"$past({mapped})"
            if name == "rose":
                return f"$rose({mapped})"
            if name == "fell":
                return f"$fell({mapped})"
            if name == "stable":
                return f"$stable({mapped})"
            if name == "onehot":
                return f"$onehot({mapped})"
            if name == "onehot0":
                return f"$onehot0({mapped})"
            raise FvError(f"internal render error: unsupported function {name}")
        if name in HISTORY_FUNCS and not entry:
            raise FvError(f"internal render error: no history register for {name}({raw})")
        if name == "prev":
            return entry["name"]  # type: ignore[index]
        if name == "rose":
            return f"({mapped} && !{entry['name']})"  # type: ignore[index]
        if name == "fell":
            return f"(!{mapped} && {entry['name']})"  # type: ignore[index]
        if name == "stable":
            return f"({mapped} == {entry['name']})"  # type: ignore[index]
        if name == "onehot":
            return f"$onehot({mapped})"
        if name == "onehot0":
            return f"$onehot0({mapped})"
        raise FvError(f"internal render error: unsupported function {name}")

    return map_calls(expr, mapper)


def _trigger_full(prop: dict[str, Any], history: dict[str, dict[str, Any]],
                  mode: str, params: dict[str, Any] | None = None) -> str:
    trigger = map_expr(str(prop.get("trigger") or ""), history, mode, params)
    when = str(prop.get("when") or "").strip()
    if when:
        return f"({map_expr(when, history, mode, params)}) && ({trigger})"
    return f"({trigger})"


def _disable_default(prop: dict[str, Any]) -> bool:
    if prop.get("kind") == "reset_value":
        return False
    return bool(prop.get("disable_reset", True))


def _uses_history(prop: dict[str, Any]) -> bool:
    """Whether the property reads a value from a previous cycle.

    ``stability`` uses a history register even though its expression has no
    history function.  Only actual history-dependent functions create a
    previous-cycle dependency; pure combinational helpers such as onehot()
    and onehot0() must not add history gating.
    """
    if prop.get("kind") == "stability":
        return True
    for key in ("when", "trigger", "expr", "response"):
        expr = str(prop.get(key) or "")
        if not expr:
            continue
        if any(name in HISTORY_FUNCS for name, _arg, _s, _e in find_calls(expr)):
            return True
    return False


def _sva_history_guard(prop: dict[str, Any], design: dict[str, Any]) -> str:
    """Skip the first evaluation cycle (and the first cycle after reset).

    The monitor has an explicit ``fv_hist_valid`` register; standard SVA has no
    harness signal, so the equivalent condition is expressed with ``$past``.
    """
    if not _uses_history(prop):
        return ""
    parts = ["$past(1'b1)"]
    if _disable_default(prop):
        parts.append(f"$past(!({_reset_active_expr(design, prop)}))")
    return " && ".join(parts)


def _history_valid_lines(arm: str, clock: str, edge: str) -> str:
    return (
        "  logic fv_hist_valid;\n"
        "  initial fv_hist_valid = 1'b0;\n"
        f"  always_ff @({edge} {clock}) fv_hist_valid <= ({arm});"
    )


# -- monitor modules -----------------------------------------------------------------------------

def _monitor_header(name: str, interface: dict[str, Any], cover: bool) -> str:
    ports = ["  " + _port_decl(p, "input") for p in interface.get("ports") or []]
    ports.append("  input logic fv_start")
    if cover:
        ports.append("  output logic fv_cover")
    return f"module {name} (\n" + ",\n".join(ports) + "\n);"


def _common_arm_lines(design: dict[str, Any], prop: dict[str, Any] | None = None) -> str:
    reset = _reset_active_expr(design, prop)
    return (
        f"  wire fv_armed = fv_start && !({reset});\n"
        f"  wire fv_rst_armed = fv_start && ({reset});"
    )


def _history_for_prop(prop: dict[str, Any], interface: dict[str, Any]) -> dict[str, dict[str, Any]]:
    exprs = [str(prop.get(k) or "") for k in ("when", "trigger", "expr", "response")]
    history = _collect_history(exprs, interface)
    if prop.get("kind") == "stability":
        signal = str(prop.get("signal") or "").strip()
        key = _hist_key(signal)
        if key and key not in history:
            info = _signal_info(signal, interface, f"stability signal {signal}")
            used = {e["name"] for e in history.values()}
            history[key] = {
                "arg": signal,
                "dims": list(info.get("dims") or []),
                "width": int(info.get("width") or 1),
                "signed": bool(info.get("signed")),
                "name": _unique("fv_prev_" + _sanitize_ident(signal), used),
            }
    return history


def _assert_monitor(prop: dict[str, Any], design: dict[str, Any], interface: dict[str, Any],
                    module: str) -> str:
    pid = str(prop["id"])
    kind = str(prop["kind"])
    clock, edge = _design_clock(design, prop)
    params = design.get("parameters") or {}
    history = _history_for_prop(prop, interface)
    uses_history = _uses_history(prop)
    arm = "fv_armed" if _disable_default(prop) else "fv_start"
    lines = [
        f"// {pid}: {prop.get('title', '')}",
        f"// kind={kind} req={prop.get('req', '')}",
        _monitor_header(module, interface, False),
        _common_arm_lines(design, prop),
    ]
    for entry in history.values():
        lines.append(_history_decl(entry))
    if uses_history:
        lines.append(_history_valid_lines(arm, clock, edge))
    hist_block = _history_block(history, clock, edge, arm if uses_history else None)
    if hist_block:
        lines.append(hist_block)

    if kind == "always":
        when = str(prop.get("when") or "").strip()
        cond = arm + (" && fv_hist_valid" if uses_history else "")
        if when:
            cond += f" && ({map_expr(when, history, 'monitor', params)})"
        expr = map_expr(str(prop["expr"]), history, "monitor", params)
        lines.append("  always @* begin")
        lines.append(f"    if ({cond}) assert({expr});")
        lines.append("  end")

    elif kind == "reset_value":
        when = str(prop.get("when") or "").strip()
        cond = "fv_rst_armed" + (f" && ({map_expr(when, history, 'monitor', params)})" if when else "")
        expr = map_expr(str(prop["expr"]), history, "monitor", params)
        lines.append("  always @* begin")
        lines.append(f"    if ({cond}) assert({expr});")
        lines.append("  end")

    elif kind == "next":
        trigger = _trigger_full(prop, history, "monitor", params)
        expr = map_expr(str(prop["expr"]), history, "monitor", params)
        hist_gate = " && fv_hist_valid" if uses_history else ""
        lines.append("  logic fv_trig_d1;")
        lines.append("  initial fv_trig_d1 = 1'b0;")
        lines.append(f"  always_ff @({edge} {clock}) begin")
        lines.append(f"    fv_trig_d1 <= ({arm}){hist_gate} && ({trigger});")
        lines.append("  end")
        lines.append("  always @* begin")
        lines.append(f"    if ({arm}{hist_gate} && fv_trig_d1) assert({expr});")
        lines.append("  end")

    elif kind == "response":
        trigger = _trigger_full(prop, history, "monitor", params)
        response = map_expr(str(prop["response"]), history, "monitor", params)
        min_delay = int(prop.get("min_delay") or 1)
        max_delay = int(prop.get("max_delay") or 1)
        hist_gate = " && fv_hist_valid" if uses_history else ""
        for d in range(1, max_delay + 1):
            lines.append(f"  logic fv_trig_d{d};")
            lines.append(f"  initial fv_trig_d{d} = 1'b0;")
        resp_delays = max_delay - min_delay
        for d in range(1, resp_delays + 1):
            lines.append(f"  logic fv_resp_d{d};")
            lines.append(f"  initial fv_resp_d{d} = 1'b0;")
        lines.append(f"  always_ff @({edge} {clock}) begin")
        lines.append(f"    fv_trig_d1 <= ({arm}){hist_gate} && ({trigger});")
        for d in range(2, max_delay + 1):
            lines.append(f"    fv_trig_d{d} <= fv_trig_d{d - 1} && ({arm});")
        for d in range(1, resp_delays + 1):
            src = response if d == 1 else f"fv_resp_d{d - 1}"
            lines.append(f"    fv_resp_d{d} <= ({src}) && ({arm});")
        lines.append("  end")
        response_or = [response] + [f"fv_resp_d{d}" for d in range(1, resp_delays + 1)]
        lines.append("  always @* begin")
        lines.append(f"    if ({arm}{hist_gate} && fv_trig_d{max_delay})")
        lines.append("      assert(" + " || ".join(f"({x})" for x in response_or) + ");")
        lines.append("  end")

    elif kind == "stability":
        trigger = _trigger_full(prop, history, "monitor", params)
        signal = str(prop["signal"]).strip()
        cycles = int(prop.get("cycles") or 1)
        hist = history[_hist_key(signal)]["name"]
        for d in range(1, cycles + 1):
            lines.append(f"  logic fv_trig_d{d};")
            lines.append(f"  initial fv_trig_d{d} = 1'b0;")
        lines.append(f"  always_ff @({edge} {clock}) begin")
        lines.append(f"    fv_trig_d1 <= ({arm}) && fv_hist_valid && ({trigger});")
        for d in range(2, cycles + 1):
            lines.append(f"    fv_trig_d{d} <= fv_trig_d{d - 1} && ({arm});")
        lines.append("  end")
        lines.append("  always @* begin")
        for d in range(1, cycles + 1):
            lines.append(f"    if ({arm} && fv_hist_valid && fv_trig_d{d}) "
                         f"assert(({signal}) == {hist});")
        lines.append("  end")

    else:
        raise FvError(f"property {pid}: unsupported kind {kind!r}")

    lines.append("endmodule")
    lines.append("")
    return "\n".join(lines)


def _cover_monitor(prop: dict[str, Any], design: dict[str, Any], interface: dict[str, Any],
                   module: str, mode: str) -> str:
    """A cover or assumption-satisfiability monitor with an ``fv_cover`` output."""
    pid = str(prop["id"])
    clock, edge = _design_clock(design, prop)
    params = design.get("parameters") or {}
    history = _collect_history([str(prop.get(k) or "") for k in ("when", "expr")], interface)
    uses_history = _uses_history(prop)
    if mode == "assume":
        arm = "fv_start" + (f" && !({_reset_active_expr(design, prop)})" if _disable_default(prop) else "")
    else:
        arm = "fv_armed" if _disable_default(prop) else "fv_start"
    lines = [
        f"// {pid}: {prop.get('title', '')}",
        f"// role={prop.get('role', '')} kind={prop.get('kind', '')} req={prop.get('req', '')}",
        _monitor_header(module, interface, True),
        _common_arm_lines(design, prop),
    ]
    for entry in history.values():
        lines.append(_history_decl(entry))
    if uses_history:
        lines.append(_history_valid_lines(arm, clock, edge))
    hist_block = _history_block(history, clock, edge, arm if uses_history else None)
    if hist_block:
        lines.append(hist_block)
    when = str(prop.get("when") or "").strip()
    expr = map_expr(str(prop["expr"]), history, "monitor", params)
    cond = [f"({arm})"]
    if uses_history:
        cond.append("(fv_hist_valid)")
    if when:
        cond.append(f"({map_expr(when, history, 'monitor', params)})")
    cond.append(f"({expr})")
    lines.append("  assign fv_cover = " + " && ".join(cond) + ";")
    lines.append("endmodule")
    lines.append("")
    return "\n".join(lines)


def _assumption_monitor(assumptions: list[dict[str, Any]], design: dict[str, Any],
                        interface: dict[str, Any]) -> str:
    params = design.get("parameters") or {}
    # Group assumptions by clock domain for history sampling.
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for p in assumptions:
        clk, edge = _design_clock(design, p)
        key = (clk, edge)
        g = groups.setdefault(key, {"clock": clk, "edge": edge, "props": [], "exprs": []})
        g["props"].append(p)
        g["exprs"].extend([str(p.get("when") or ""), str(p.get("expr") or "")])
    lines = [
        "// Environment assumptions of the formal contract.",
        _monitor_header("fv_assume_all", interface, False),
        _common_arm_lines(design, None),
    ]
    # per-group history
    group_hist: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for key, g in groups.items():
        hist = _collect_history(g["exprs"], interface)
        group_hist[key] = hist
        for entry in hist.values():
            lines.append(_history_decl(entry))
    # per-group history valid and history blocks
    for gi, (key, g) in enumerate(groups.items()):
        hist = group_hist[key]
        uses_history = any(_uses_history(p) for p in g["props"])
        hv = f"fv_hist_valid_g{gi}"
        if uses_history:
            lines.append(f"  logic {hv};")
            lines.append(f"  initial {hv} = 1'b0;")
            lines.append(f"  always_ff @({g['edge']} {g['clock']}) {hv} <= fv_start;")
            hb = _history_block(hist, g["clock"], g["edge"], "fv_start")
            if hb:
                lines.append(hb)
        else:
            # still emit history block if any history exists (harmless)
            hb = _history_block(hist, g["clock"], g["edge"], "fv_start")
            if hb:
                lines.append(hb)
        g["hist_valid"] = hv if uses_history else "1'b1"
        g["hist"] = hist

    lines.append("  always @* begin")
    for key, g in groups.items():
        hist = g["hist"]
        hv = g["hist_valid"]
        for p in g["props"]:
            pid = str(p["id"])
            arm = "fv_start"
            if _disable_default(p):
                arm += f" && !({_reset_active_expr(design, p)})"
            when = str(p.get("when") or "").strip()
            cond = f"({arm})"
            if _uses_history(p):
                cond += f" && {hv}"
            if when:
                cond += f" && ({map_expr(when, hist, 'monitor', params)})"
            expr = map_expr(str(p["expr"]), hist, "monitor", params)
            lines.append(f"    // {pid}")
            lines.append(f"    if ({cond}) assume({expr});")
    lines.append("  end")
    lines.append("endmodule")
    lines.append("")
    return "\n".join(lines)


# -- wrappers and SVA checker --------------------------------------------------------------------

def _param_override(design: dict[str, Any]) -> str:
    params = design.get("parameters") or {}
    if not params:
        return ""
    pairs = ", ".join(f".{name}({int(value)})" for name, value in params.items())
    return f" #({pairs})"


def _wrapper(prop: dict[str, Any], design: dict[str, Any], interface: dict[str, Any],
             top_module: str, monitor_module: str, has_assumptions: bool,
             cover: bool) -> str:
    lines = [
        f"// Wrapper for {prop['id']}: {prop.get('title', '')}",
        f"module {top_module} (",
    ]
    ports: list[str] = []
    for p in interface.get("ports") or []:
        if p.get("direction") == "input":
            ports.append("  " + _port_decl(p, "input"))
    ports.append("  input logic fv_start")
    if cover:
        ports.append("  output logic fv_cover")
    lines.append(",\n".join(ports))
    lines.append(");")
    for p in interface.get("ports") or []:
        if p.get("direction") != "input":
            lines.append("  " + _wire_decl(p))
    conns = ", ".join(f".{p['name']}({p['name']})" for p in interface.get("ports") or [])
    lines.append(f"  (* keep *) {design['top']}{_param_override(design)} u_dut ({conns});")
    if has_assumptions:
        aconns = ", ".join(
            [f".{p['name']}({p['name']})" for p in interface.get("ports") or []]
            + [".fv_start(fv_start)"]
        )
        lines.append(f"  (* keep *) fv_assume_all u_assume ({aconns});")
    mconns = ", ".join(
        [f".{p['name']}({p['name']})" for p in interface.get("ports") or []]
        + [".fv_start(fv_start)"]
        + ([".fv_cover(fv_cover)"] if cover else [])
    )
    lines.append(f"  (* keep *) {monitor_module} u_mon ({mconns});")
    lines.append("endmodule")
    lines.append("")
    return "\n".join(lines)


def _sva_property(prop: dict[str, Any], design: dict[str, Any]) -> str:
    kind = str(prop["kind"])
    params = design.get("parameters") or {}
    when = str(prop.get("when") or "").strip()
    reset = _reset_active_expr(design, prop)
    disable = _disable_default(prop)
    hist_guard = _sva_history_guard(prop, design)

    def antecedent(expr: str) -> str:
        if hist_guard:
            return f"({hist_guard}) && ({expr})"
        return f"({expr})"

    if kind == "cover":
        body = map_expr(str(prop["expr"]), {}, "sva", params)
        if when:
            body = f"({map_expr(when, {}, 'sva', params)}) && ({body})"
        body = antecedent(body)
        if disable:
            return f"cover property (@({_design_clock(design, prop)[1]} {_design_clock(design, prop)[0]}) disable iff ({reset}) ({body}));"
        return f"cover property (@({_design_clock(design, prop)[1]} {_design_clock(design, prop)[0]}) ({body}));"

    if kind == "reset_value":
        body = map_expr(str(prop["expr"]), {}, "sva", params)
        if when:
            guard = f"({reset}) && ({map_expr(when, {}, 'sva', params)})"
        else:
            guard = f"({reset})"
        return (
            f"assert property (@({_design_clock(design, prop)[1]} {_design_clock(design, prop)[0]}) "
            f"({guard}) |-> ({body}))"
        )

    if kind == "always":
        body = map_expr(str(prop["expr"]), {}, "sva", params)
        if when:
            body = f"{antecedent(map_expr(when, {}, 'sva', params))} |-> ({body})"
        elif hist_guard:
            body = f"({hist_guard}) |-> ({body})"
        else:
            body = f"({body})"
    elif kind == "next":
        trigger = antecedent(_trigger_full(prop, {}, "sva", params))
        body = f"{trigger} |=> ({map_expr(str(prop['expr']), {}, 'sva', params)})"
    elif kind == "response":
        mn = int(prop.get("min_delay") or 1)
        mx = int(prop.get("max_delay") or 1)
        trigger = antecedent(_trigger_full(prop, {}, "sva", params))
        body = (
            f"{trigger} |-> ##[{mn}:{mx}] "
            f"({map_expr(str(prop['response']), {}, 'sva', params)})"
        )
    elif kind == "stability":
        cyc = int(prop.get("cycles") or 1)
        signal = str(prop["signal"]).strip()
        rep = f"$stable({signal})" + (f"[*{cyc}]" if cyc > 1 else "")
        trigger = antecedent(_trigger_full(prop, {}, "sva", params))
        body = f"{trigger} |=> ({rep})"
    else:
        raise FvError(f"property {prop.get('id')}: unsupported kind {kind!r}")

    clock, edge = _design_clock(design, prop)
    dis = f"disable iff ({reset}) " if disable else ""
    return f"assert property (@({edge} {clock}) {dis}({body}))"


def _sva_checker(contract: dict[str, Any], design: dict[str, Any],
                 interface: dict[str, Any]) -> str:
    lines = [
        "// Generated by formal-sva/fv_render.py -- standard SVA for commercial tools.",
        "// Bind this checker to the DUT compiled with the same parameters as the contract.",
        "module fv_sva_checker (",
        ",\n".join("  " + _port_decl(p, "input") for p in interface.get("ports") or []),
        ");",
        "",
    ]
    used: set[str] = set()
    props = contract.get("properties") or []
    # Assumptions first so a reader sees the environment rules before the checks.
    for prop in [p for p in props if p.get("role") == "assume"]:
        name = _unique("fv_" + _sanitize_ident(str(prop["id"])), used)
        body = _sva_property({**prop, "kind": "always"}, design)
        body = body.replace("assert property", "assume property", 1)
        lines.append(f"  // {prop['id']}: {prop.get('title', '')}")
        lines.append(f"  {name}: {body};")
        lines.append("")
    for prop in [p for p in props if p.get("role") != "assume"]:
        name = _unique("fv_" + _sanitize_ident(str(prop["id"])), used)
        body = _sva_property(prop, design)
        lines.append(f"  // {prop['id']}: {prop.get('title', '')}")
        if prop.get("role") == "cover":
            lines.append(f"  {name}: {body}")
        else:
            lines.append(f"  {name}: {body} else $error(\"formal-sva: {prop['id']} failed\");")
        lines.append("")
    lines.append("endmodule")
    lines.append("")
    lines.append(f"bind {design['top']} fv_sva_checker fv_bind (.*);")
    lines.append("")
    return "\n".join(lines)


# -- top-level render ----------------------------------------------------------------------------

def _old_generated(gen_dir: Path, root: Path) -> list[Path]:
    manifest_path = gen_dir / "fv_manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[Path] = []
    for raw in old.get("generated_files") or []:
        p = Path(raw)
        if not p.is_absolute():
            p = (root / raw).resolve()
        out.append(p)
    return out


def _cleanup_paths(paths: list[Path], keep: set[Path]) -> list[str]:
    removed: list[str] = []
    for p in paths:
        if p.is_file() and p.resolve() not in keep:
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                pass
    return removed


def render(manifest: dict[str, Any], contract_path: Path | None = None,
           depth_override: int | None = None, quiet: bool = False) -> dict[str, Any]:
    validation, contract, bundle, interface = validate_design(manifest, contract_path)
    if not validation.ok:
        raise FvError(
            "contract validation failed; fix the errors before rendering:\n"
            + "\n".join(f"  ERROR {e}" for e in validation.errors)
        )
    _check_interface(interface)
    root = project_root(manifest)
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    gen_dir = paths["sva"] / "generated"
    gen_dir.mkdir(parents=True, exist_ok=True)
    old_generated = _old_generated(gen_dir, root)

    design = contract.get("design") or {}
    clock, edge = _design_clock(design)
    reset_signal, reset_active, reset_kind = _design_reset(design)
    reset_cycles = int((design.get("reset") or {}).get("cycles") or
                       (manifest.get("_formal") or {}).get("reset_cycles") or 2)
    if reset_cycles < 2:
        raise FvError(
            f"reset_cycles={reset_cycles} is too small; the monitor arms after two "
            "reset cycles so that synchronous resets have taken effect"
        )
    if reset_signal not in {p["name"] for p in interface["ports"]}:
        raise FvError(f"reset signal {reset_signal!r} is not a top-level port")
    if clock not in {p["name"] for p in interface["ports"]}:
        raise FvError(f"clock signal {clock!r} is not a top-level port")
    for name in (clock, reset_signal):
        port = next(p for p in interface["ports"] if p["name"] == name)
        if int(port.get("width") or 1) != 1:
            raise FvError(f"{name!r} must be a 1-bit port")

    formal = manifest.get("_formal") or {}
    default_depth = int(depth_override or formal.get("depth") or 12)
    props = list(contract.get("properties") or [])
    assumptions = [p for p in props if p.get("role") == "assume"]
    has_assumptions = bool(assumptions)

    used_files: set[str] = set()
    prop_entries: list[dict[str, Any]] = []
    generated: list[Path] = []

    assumptions_file: Path | None = None
    if has_assumptions:
        assumptions_file = gen_dir / "fv_assumptions.sv"
        write_text(assumptions_file, _assumption_monitor(assumptions, design, interface))
        generated.append(assumptions_file)

    # The standard SVA file is independent of the Yosys harness.
    sva_file = gen_dir / "fv_sva_checker.sv"
    write_text(sva_file, _sva_checker(contract, design, interface))
    generated.append(sva_file)

    src = source_files(manifest)
    dut_sources = [str(p.resolve()) for p in src["sources"]]
    include_dirs = [str(p.resolve()) for p in src["include_dirs"]]
    defines = list(src["defines"])

    for prop in props:
        pid = str(prop["id"])
        file_san = _unique(_sanitize_ident(pid), used_files)
        kind = str(prop["kind"])
        role = str(prop["role"])
        cover = role in ("cover", "assume")
        monitor_module = f"fv_mon_{file_san}"
        top_module = f"fv_top_{file_san}"

        if role == "assume":
            monitor_module = f"fv_cov_{file_san}"
            monitor_code = _cover_monitor(prop, design, interface, monitor_module, "assume")
        elif role == "cover":
            monitor_module = f"fv_cov_{file_san}"
            monitor_code = _cover_monitor(prop, design, interface, monitor_module, "cover")
        else:
            monitor_code = _assert_monitor(prop, design, interface, monitor_module)

        monitor_file = gen_dir / f"fv_{file_san}_monitor.sv"
        top_file = gen_dir / f"fv_{file_san}_top.sv"
        write_text(monitor_file, monitor_code)
        write_text(top_file, _wrapper(prop, design, interface, top_module, monitor_module,
                                      has_assumptions, cover))
        generated.extend([monitor_file, top_file])

        depth = int(prop.get("depth") or default_depth)
        if depth <= reset_cycles:
            raise FvError(
                f"property {pid}: depth {depth} must be greater than reset_cycles "
                f"({reset_cycles}); use --depth or set formal.depth"
            )
        if kind == "next" and depth < reset_cycles + 2:
            raise FvError(
                f"property {pid}: kind next needs depth >= reset_cycles + 2 "
                f"({reset_cycles + 2})"
            )
        if kind == "stability" and depth < reset_cycles + int(prop.get("cycles") or 1):
            raise FvError(
                f"property {pid}: kind stability needs depth >= reset_cycles + cycles "
                f"({reset_cycles + int(prop.get('cycles') or 1)})"
            )
        if kind == "response" and depth < reset_cycles + int(prop.get("max_delay") or 1):
            raise FvError(
                f"property {pid}: kind response needs depth >= reset_cycles + max_delay "
                f"({reset_cycles + int(prop.get('max_delay') or 1)})"
            )

        if role in ("cover", "assume"):
            mode = "cover" if role == "cover" else "assume"
            skip = 0
            induction = False
        else:
            mode = "assert"
            if kind == "response":
                skip = int(prop.get("max_delay") or 1)
            elif kind == "stability":
                skip = int(prop.get("cycles") or 1)
            else:
                skip = 1
            induction = bool(formal.get("induction", True)) and kind != "reset_value"

        prop_entries.append({
            "id": pid,
            "file_san": file_san,
            "req": prop.get("req"),
            "feature": prop.get("feature"),
            "kind": kind,
            "role": role,
            "title": prop.get("title"),
            "severity": prop.get("severity") or ("info" if role in ("cover", "assume") else "error"),
            "mode": mode,
            "top": top_module,
            "monitor": monitor_module,
            "monitor_file": rel_to(root, monitor_file),
            "top_file": rel_to(root, top_file),
            "depth": depth,
            "reset_cycles": reset_cycles,
            "skip": skip,
            "induction": induction,
            "cover_signal": "fv_cover" if cover else None,
            "tags": list(prop.get("tags") or []),
            "evidence": prop.get("evidence"),
        })

    # Standard SVA path and bind command.
    contract_used = Path(contract_path) if contract_path else paths["contract"]
    harness_path = gen_dir / "fv_manifest.json"
    harness = {
        "schema_version": RENDER_SCHEMA_VERSION,
        "generated_by": "formal-sva/fv_render.py",
        "design": {
            "name": design.get("name"),
            "top": design.get("top"),
            "clock": {"signal": clock, "edge": edge},
            "reset": {"signal": reset_signal, "active": reset_active, "kind": reset_kind,
                      "cycles": reset_cycles},
            "parameters": design.get("parameters") or {},
            "reset_cycles": reset_cycles,
            "inputs": [p["name"] for p in interface.get("ports") or []
                       if p.get("direction") == "input"],
            "outputs": [p["name"] for p in interface.get("ports") or []
                        if p.get("direction") != "input"],
        },
        "interface": {
            "top": interface.get("top"),
            "ports": interface.get("ports") or [],
            "parameters": interface.get("parameters") or {},
        },
        "formal": {
            "backend": formal.get("backend") or "yosys",
            "depth": default_depth,
            "timeout_s": int(formal.get("timeout_s") or 300),
            "jobs": int(formal.get("jobs") or 1),
            "induction": bool(formal.get("induction", True)),
            "reset_cycles": reset_cycles,
            "memory_map": bool(formal.get("memory_map", True)),
        },
        "start_signal": "fv_start",
        "files": {
            "dut": dut_sources,
            "include_dirs": include_dirs,
            "defines": defines,
            "assumptions": rel_to(root, assumptions_file) if assumptions_file else None,
            "sva_checker": rel_to(root, sva_file),
            "bind": f"bind {design.get('top')} fv_sva_checker fv_bind (.*);",
        },
        "contract": {
            "path": rel_to(root, contract_used),
            "sha256": sha256_file(contract_used),
        },
        "bundle": {
            "path": rel_to(root, Path(manifest["_paths"]["bundle"])),
            "sha256": sha256_file(Path(manifest["_paths"]["bundle"])),
        },
        "properties": prop_entries,
        "generated_files": [rel_to(root, p) for p in generated],
    }
    write_json(harness_path, harness)
    generated.append(harness_path)
    # Clean only files that the previous run generated.
    removed = _cleanup_paths(old_generated, {p.resolve() for p in generated})

    if not quiet:
        print(f"rendered: {rel_to(root, gen_dir)}")
        print(f"  standard SVA: {rel_to(root, sva_file)}")
        if assumptions_file:
            print(f"  assumptions:  {rel_to(root, assumptions_file)}")
        print(f"  harness:      {rel_to(root, harness_path)}")
        print(f"  {len(prop_entries)} property harness(es)")
        for p in prop_entries:
            print(f"    {p['id']:<24} {p['role']:<7} {p['kind']:<12} "
                  f"{p['mode']:<7} depth={p['depth']} top={p['top']}")
        if removed:
            print(f"  removed {len(removed)} stale generated file(s)")
    return harness


if __name__ == "__main__":
    import argparse
    from fv_common import load_manifest

    ap = argparse.ArgumentParser(description="render standard SVA and Yosys monitors")
    ap.add_argument("--design", required=True)
    ap.add_argument("--contract")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    m = load_manifest(args.design)
    h = render(m, Path(args.contract) if args.contract else None, args.depth)
    if args.json:
        print(json.dumps(h, indent=2))
