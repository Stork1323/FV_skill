"""SymbiYosys backend (default).

SymbiYosys has no ``sat -set-at`` equivalent, so the backend generates a small
driver wrapper that makes reset and ``fv_start`` functions of a cycle counter:
reset is active for ``reset_cycles`` and ``fv_start`` is low on cycle 0 and high
afterwards.  Induction uses the same wrapper with reset tied inactive and
``fv_start`` tied high, because induction starts from an arbitrary state.

One ``.sby`` file is generated per property and phase (BMC, induction/cover).
If ``sby`` is not installed, the backend reports a clear configuration error;
``fv.py doctor`` shows the same information before a run starts.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    eprint,
    project_root,
    rel_to,
    run_cmd,
    sanitize,
    write_text,
)
from fv_render import (
    _design_clock,
    _design_reset,
    _port_decl,
    _reset_active_expr,
)
from fv_yosys import harness_files


def available() -> bool:
    return shutil.which("sby") is not None


def _qpath(path: Path | str) -> str:
    s = str(path)
    return f'"{s}"' if any(c.isspace() for c in s) else s


def _driver_lines(design: dict[str, Any], prop: dict[str, Any], induction: bool) -> list[str]:
    reset_sig, active, _kind = _design_reset(design, prop)
    clock, edge = _design_clock(design, prop)
    # per-domain reset cycles
    reset_obj = design.get("reset") or {}
    if prop.get("reset"):
        for r in (design.get("resets") or []):
            if str(r.get("name") or r.get("signal")) == str(prop.get("reset")):
                reset_obj = r; break
    reset_cycles = int(reset_obj.get("cycles") or design.get("reset_cycles") or
                       (design.get("reset") or {}).get("cycles") or 0)
    active_value = 0 if active == "low" else 1
    inactive_value = 1 - active_value
    lines: list[str] = []
    if induction:
        lines.append(f"  wire {reset_sig} = 1'b{inactive_value};")
        lines.append("  wire fv_start = 1'b1;")
    else:
        lines.append("  logic [15:0] fv_cycle;")
        lines.append("  initial fv_cycle = 16'd0;")
        lines.append(f"  always_ff @({edge} {clock}) begin")
        lines.append("    if (fv_cycle != 16'hffff) fv_cycle <= fv_cycle + 16'd1;")
        lines.append("  end")
        lines.append(f"  wire {reset_sig} = (fv_cycle < 16'd{reset_cycles}) "
                     f"? 1'b{active_value} : 1'b{inactive_value};")
        lines.append("  wire fv_start = (fv_cycle != 16'd0);")
    return lines


def _wrapper(harness: dict[str, Any], prop: dict[str, Any], top_module: str,
             induction: bool, cover: bool) -> str:
    design = harness.get("design") or {}
    interface = harness.get("interface") or {}
    reset_sig, _active, _kind = _design_reset(design, prop)
    ports = list(interface.get("ports") or [])
    input_ports = [p for p in ports
                   if p.get("direction") == "input" and p["name"] != reset_sig]
    output_ports = [p for p in ports if p.get("direction") != "input"]
    lines = [
        f"// SymbiYosys wrapper for {prop['id']}: {prop.get('title', '')}",
        f"module {top_module} (",
    ]
    decls = ["  " + _port_decl(p, "input") for p in input_ports]
    decls.extend("  " + _port_decl(p, "output") for p in output_ports)
    lines.append(",\n".join(decls))
    lines.append(");")
    lines.extend(_driver_lines(design, prop, induction))
    conns = []
    for p in ports:
        name = p["name"]
        conns.append(f".{name}({name})")
    lines.append(f"  (* keep *) {design['top']} {_param_override(design)} u_dut ("
                 + ", ".join(conns) + ");")
    has_assumptions = bool((harness.get("files") or {}).get("assumptions"))
    if has_assumptions:
        aconns = [f".{p['name']}({p['name']})" for p in ports] + [".fv_start(fv_start)"]
        lines.append("  (* keep *) fv_assume_all u_assume ("
                     + ", ".join(aconns) + ");")
    monitor = str(prop["monitor"])
    mconns = [f".{p['name']}({p['name']})" for p in ports] + [".fv_start(fv_start)"]
    if cover:
        mconns.append(".fv_cover(fv_cover)")
        lines.append("  logic fv_cover;")
    lines.append(f"  (* keep *) {monitor} u_mon (" + ", ".join(mconns) + ");")
    if cover:
        lines.append("  always @* begin")
        lines.append("    cover(fv_cover);")
        lines.append("  end")
    lines.append("endmodule")
    lines.append("")
    return "\n".join(lines)


def _param_override(design: dict[str, Any]) -> str:
    params = design.get("parameters") or {}
    if not params:
        return ""
    pairs = ", ".join(f".{name}({int(value)})" for name, value in params.items())
    return f" #({pairs})"


def _read_command(harness: dict[str, Any], files: list[Path]) -> str:
    hfiles = harness.get("files") or {}
    parts = ["read_verilog -formal -sv -DSYNTHESIS"]
    for inc in hfiles.get("include_dirs") or []:
        parts.append(f"-I{_qpath(inc)}")
    for define in hfiles.get("defines") or []:
        parts.append(f"-D{define}")
    for path in files:
        if not path.is_file():
            raise FvError(f"harness source not found: {path}")
        parts.append(_qpath(path))
    return " ".join(parts)


def _sby_text(harness: dict[str, Any], prop: dict[str, Any], mode: str,
              depth: int, files: list[Path], cfg: dict[str, Any],
              top: str | None = None) -> str:
    formal = harness.get("formal") or {}
    engine = str(cfg.get("engine") or formal.get("sby_engine") or "smtbmc")
    lines = [
        "[options]",
        f"mode {mode}",
        f"depth {depth}",
        "",
        "[engines]",
        engine,
        "",
        "[script]",
        _read_command(harness, files),
        f"prep -top {top or prop['top']} -flatten",
        "async2sync",
        "memory_map",
        "opt -fast",
        "",
    ]
    return "\n".join(lines)


def _parse(output: str, mode: str) -> dict[str, Any]:
    lines = output.splitlines()
    has_timeout = any(re.search(r"DONE\s+\(TIMEOUT", line, re.I) for line in lines)
    has_error = any(re.search(r"DONE\s+\(ERROR", line, re.I) for line in lines)
    has_pass = bool(re.search(r"DONE\s+\(PASS", output, re.I) or "no model found: SUCCESS!" in output)
    has_fail = bool(re.search(r"DONE\s+\(FAIL", output, re.I) or "model found: FAIL!" in output)
    has_proven = "Induction step proven: SUCCESS!" in output
    if has_pass and has_fail:
        return {"result": "error", "line": "conflicting SBY markers",
                "detail": "log contains both PASS and FAIL markers"}
    if has_proven and has_fail:
        return {"result": "error", "line": "conflicting SBY markers",
                "detail": "log contains both proven and fail markers"}
    if has_timeout:
        return {"result": "timeout", "line": next(line.strip() for line in lines if re.search(r"DONE\s+\(TIMEOUT", line, re.I))}
    if has_error and not (has_pass or has_fail or has_proven):
        return {"result": "error", "line": next(line.strip() for line in lines if re.search(r"DONE\s+\(ERROR", line, re.I))}
    if has_proven:
        return {"result": "proven", "line": "Induction step proven: SUCCESS!"}
    if has_pass:
        return {"result": "pass", "line": next(
            (x.strip() for x in lines if re.search(r"DONE\s+\(PASS", x, re.I) or "no model found: SUCCESS!" in x), "PASS")}
    if has_fail:
        return {"result": "fail", "line": next(
            (x.strip() for x in lines if re.search(r"DONE\s+\(FAIL", x, re.I) or "model found: FAIL!" in x), "FAIL")}
    if has_error:
        return {"result": "error", "line": next(line.strip() for line in lines if re.search(r"DONE\s+\(ERROR", line, re.I))}
    return {"result": "unknown", "line": ""}


def _counterexample(output: str, build: Path | None = None, limit: int = 160) -> list[str]:
    rows = [line.rstrip() for line in output.splitlines()
            if re.match(r"^\s*\d+\s+[\\$A-Za-z_]", line)]
    if rows:
        return rows[-limit:]
    if build is not None:
        # SBY usually writes traces to files; include a compact trace_tb excerpt.
        for tb in sorted(build.rglob("trace_tb.v")):
            try:
                tb_lines = tb.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            excerpt: list[str] = [f"trace_tb: {tb}"]
            for line in tb_lines:
                s = line.rstrip()
                if not s:
                    continue
                if re.search(r"\b(state \d+|PI_|UUT\.|cycle ==|Assert failed|initial begin|anyinit)", s):
                    excerpt.append(s[:220])
                if len(excerpt) >= limit:
                    break
            if len(excerpt) > 1:
                return excerpt
        for vcd in sorted(build.rglob("trace.vcd")):
            return [f"trace_vcd: {vcd}"]
        for yw in sorted(build.rglob("trace.yw")):
            return [f"trace_yw: {yw}"]
    return []


def _find_log(build: Path, stem: str) -> Path | None:
    for candidate in (build / stem / "logfile.txt", build / "logfile.txt"):
        if candidate.is_file():
            return candidate
    hits = sorted(build.rglob("logfile.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def run_property(harness: dict[str, Any], prop: dict[str, Any], cfg: dict[str, Any],
                 depth: int | None = None, timeout_s: int | None = None,
                 induction: bool | None = None, quiet: bool = False) -> dict[str, Any]:
    if not available():
        raise FvError(
            "SymbiYosys backend selected but `sby` is not on PATH. "
            "Install OSS CAD Suite or select the 'yosys' backend in formal/tools.json."
        )
    root = Path(harness["_root"])
    design = harness.get("design") or {}
    formal = harness.get("formal") or {}
    pid = str(prop["id"])
    file_san = str(prop.get("file_san") or sanitize(pid).lower())
    paths = harness.get("_paths") or {}
    build = Path(paths.get("build") or (root / "formal" / "build")) / "sby" / file_san
    logs = Path(paths.get("reports") or (root / "formal" / "reports")) / "logs"
    build.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    actual_depth = int(depth or prop.get("depth") or formal.get("depth") or 12)
    actual_timeout = int(timeout_s or cfg.get("timeout_s") or formal.get("timeout_s") or 300)
    want_induction = bool(formal.get("induction", True) if induction is None else induction)
    want_induction = want_induction and bool(prop.get("induction", True))
    mode = str(prop.get("mode") or "assert")
    cover = mode in ("cover", "assume")

    def run_phase(phase: str, ind: bool) -> dict[str, Any]:
        phase_top = f"{prop['top']}_{phase}"
        wrapper = build / f"{phase}_top.sv"
        write_text(wrapper, _wrapper(harness, prop, phase_top, ind, cover))
        files = harness_files(harness, prop) + [wrapper]
        sby_mode = "cover" if cover else ("prove" if ind else "bmc")
        sby_file = build / f"{phase}.sby"
        write_text(sby_file, _sby_text(harness, prop, sby_mode, actual_depth, files, cfg,
                                       top=phase_top))
        if not quiet:
            eprint(f"[{pid}] sby {phase} mode={sby_mode} depth={actual_depth}")
        res = run_cmd(["sby", "-f", str(sby_file)], cwd=build, timeout_s=actual_timeout)
        output = res.get("output") or ""
        log_path = _find_log(build, phase)
        if log_path:
            output += "\n" + log_path.read_text(encoding="utf-8", errors="replace")
        log_file = logs / f"{file_san}.{phase}.log"
        write_text(log_file, output)
        parsed = _parse(output, sby_mode)
        return {
            "result": parsed["result"],
            "line": parsed.get("line", ""),
            "returncode": res.get("returncode"),
            "timed_out": bool(res.get("timed_out")),
            "elapsed_s": res.get("elapsed_s"),
            "script": rel_to(root, sby_file),
            "log": rel_to(root, log_file),
            "wrapper": rel_to(root, wrapper),
            "output": output,
        }

    bmc = run_phase("bmc", False)
    result: dict[str, Any] = {
        "id": pid,
        "kind": prop.get("kind"),
        "role": prop.get("role"),
        "mode": mode,
        "depth": actual_depth,
        "reset_cycles": int(design.get("reset_cycles") or
                            (design.get("reset") or {}).get("cycles") or 0),
        "timeout_s": actual_timeout,
        "backend": cfg.get("name"),
        "bmc": {k: bmc.get(k) for k in
                ("result", "line", "returncode", "timed_out", "elapsed_s", "script", "log")},
        "induction": None,
        "counterexample": [],
        "status": "ERROR",
        "error": "",
        "elapsed_s": bmc.get("elapsed_s") or 0.0,
    }
    if bmc.get("timed_out") or bmc["result"] == "timeout":
        result["status"] = "TIMEOUT"
        result["error"] = f"BMC timed out after {actual_timeout}s"
        result["counterexample"] = _counterexample(bmc.get("output") or "", build)
        return result
    if bmc["result"] == "error":
        result["status"] = "ERROR"
        result["error"] = bmc.get("line") or "sby reported an error"
        return result
    if bmc["result"] == "unknown":
        result["status"] = "ERROR"
        result["error"] = "no sby result line in the log; inspect " + str(bmc.get("log"))
        return result

    if cover:
        reachable = bmc["result"] == "pass"
        result["counterexample"] = _counterexample(bmc.get("output") or "", build)
        result["status"] = (("COVERED" if mode == "cover" else "ASSUMED")
                            if reachable else
                            ("NOT_REACHED_WITHIN_BOUND" if mode == "cover" else "VACUOUS"))
        result["status_reason"] = (
            "Witness found within bound." if reachable else
            "No witness found within bound; this is not a proof of unreachability.")
        if not quiet:
            eprint(f"[{pid}] {result['status']} ({result['elapsed_s']}s)")
        return result

    if bmc["result"] == "fail":
        result["status"] = "FAIL"
        result["counterexample"] = _counterexample(bmc.get("output") or "", build)
        result["error"] = bmc.get("line") or "sby found a counterexample"
        return result

    result["status"] = "BMC_PASS"
    if not want_induction:
        return result
    induct = run_phase("induct", True)
    result["elapsed_s"] = round(float(result["elapsed_s"]) + float(induct.get("elapsed_s") or 0), 3)
    result["induction"] = {
        "result": induct["result"],
        "line": induct.get("line", ""),
        "returncode": induct.get("returncode"),
        "timed_out": bool(induct.get("timed_out")),
        "elapsed_s": induct.get("elapsed_s"),
        "script": induct.get("script"),
        "log": induct.get("log"),
    }
    if induct.get("timed_out") or induct["result"] == "timeout":
        result["induction"]["reason"] = f"induction timed out after {actual_timeout}s"
    elif induct["result"] in ("proven", "pass"):
        result["status"] = "PROVEN"
    elif induct["result"] == "error":
        result["induction"]["reason"] = induct.get("line") or "sby error during induction"
    elif induct["result"] == "unknown":
        result["induction"]["reason"] = "no induction result line in the sby log"
    else:
        result["induction"]["reason"] = (induct.get("line")
                                          or "induction did not prove the property")
        if induct["result"] == "fail" and not result["counterexample"]:
            result["counterexample"] = _counterexample(induct.get("output") or "", build)
    if not quiet:
        eprint(f"[{pid}] {result['status']} ({result['elapsed_s']}s)")
    return result
