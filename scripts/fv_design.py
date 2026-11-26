"""Deterministic design-interface extraction (no LLM).

Reads the top module's source header for the *source-level* packed dimensions
(needed for multi-dimensional ports such as `o_sram_we[banks]`) and uses Yosys
for the authoritative port directions and flat widths.  Parameter and localparam
values come from the final configuration, checked against the RTL defaults.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    project_root,
    read_json,
    resolve_path,
    run_cmd,
    source_files,
    which_yosys,
    write_json,
)

IDENT_RE = re.compile(r"\\[^\s]+|[A-Za-z_][A-Za-z0-9_$]*")
VERILOG_NUM_RE = re.compile(r"^(\d*)'([sS])?([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)$")
SIMPLE_IDENT_RE = re.compile(r"^\\?[A-Za-z_][A-Za-z0-9_$]*$")


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def find_matching(text: str, start: int, open_ch: str = "(", close_ch: str = ")") -> int:
    depth = 0
    i = start
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise FvError(f"unbalanced {open_ch}{close_ch} in module header")


def split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    in_string = False
    for i, ch in enumerate(text):
        if in_string:
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def module_header(text: str, top: str) -> tuple[str, str, str]:
    """Return (parameter list text, port list text, module body) of `module <top>`."""
    text = strip_comments(text)
    m = re.search(r"\bmodule\s+" + re.escape(top) + r"\b", text)
    if not m:
        raise FvError(f"module '{top}' not found")
    i = m.end()
    while i < len(text) and text[i].isspace():
        i += 1
    params = ""
    if i < len(text) and text[i] == "#":
        i += 1
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] != "(":
            raise FvError(f"module '{top}': expected '(' after '#'")
        end = find_matching(text, i)
        params = text[i + 1:end]
        i = end + 1
        while i < len(text) and text[i].isspace():
            i += 1
    if i >= len(text) or text[i] != "(":
        raise FvError(
            f"module '{top}' has no ANSI port list; the formal flow needs a "
            "self-contained ANSI header (or add explicit ports to the manifest)"
        )
    end = find_matching(text, i)
    body_end = re.search(r"\bendmodule\b", text[end + 1:])
    body = text[end + 1:end + 1 + body_end.start()] if body_end else text[end + 1:]
    return params, text[i + 1:end], body


def _verilog_number(value: str) -> int | None:
    value = value.strip().replace("_", "")
    if value in ("'0",):
        return 0
    if value in ("'1",):
        return 1
    if re.fullmatch(r"[0-9]+", value):
        return int(value, 10)
    m = VERILOG_NUM_RE.match(value)
    if m:
        base = {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(3).lower()]
        digits = m.group(4).replace("x", "0").replace("z", "0").replace("?", "0")
        try:
            return int(digits, base)
        except ValueError:
            return None
    return None


def _preprocess_const(expr: str) -> str:
    s = expr.strip()
    # Verilog sized literals: 32'd8, 8'hFF, '0, '1.
    def lit(m: re.Match) -> str:
        return str(_verilog_number(m.group(0)) if _verilog_number(m.group(0)) is not None else 0)
    s = re.sub(r"\d*'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+", lit, s)
    s = re.sub(r"'[01]\b", lambda m: "0" if m.group(0) == "'0" else "1", s)
    s = re.sub(r"\$clog2\s*\(", "clog2(", s)
    s = re.sub(r"\$bits\s*\(", "bits(", s)
    s = s.replace("&&", " and ").replace("||", " or ")
    s = re.sub(r"!(?!=)", " not ", s)
    # Verilog part-select / sized literals are not used in dimension expressions.
    return s


def const_eval(expr: str, env: dict[str, int]) -> int | None:
    """Evaluate a small constant expression (parameters, arithmetic, $clog2)."""
    if expr is None:
        return None
    raw = str(expr).strip().rstrip(";")
    if not raw:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        pass
    num = _verilog_number(raw)
    if num is not None:
        return num
    s = _preprocess_const(raw)
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError:
        return None

    def ev(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return int(node.value)
            if isinstance(node.value, int):
                return node.value
            raise ValueError("non-integer constant")
        if isinstance(node, ast.Name):
            if node.id in env and isinstance(env[node.id], int):
                return env[node.id]
            raise ValueError(f"unknown constant {node.id}")
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
            if isinstance(node.op, ast.Invert):
                return ~v
            if isinstance(node.op, ast.Not):
                return int(not v)
            raise ValueError("unary operator")
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return a + b
            if isinstance(op, ast.Sub):
                return a - b
            if isinstance(op, ast.Mult):
                return a * b
            if isinstance(op, ast.Div):
                if b == 0:
                    raise ValueError("division by zero")
                return int(a / b)
            if isinstance(op, ast.FloorDiv):
                return a // b
            if isinstance(op, ast.Mod):
                return a % b
            if isinstance(op, ast.Pow):
                return a ** b
            if isinstance(op, ast.LShift):
                return a << b
            if isinstance(op, ast.RShift):
                return a >> b
            if isinstance(op, ast.BitAnd):
                return a & b
            if isinstance(op, ast.BitOr):
                return a | b
            if isinstance(op, ast.BitXor):
                return a ^ b
            raise ValueError("binary operator")
        if isinstance(node, ast.Compare):
            a = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                b = ev(comp)
                if isinstance(op, ast.Eq):
                    ok = a == b
                elif isinstance(op, ast.NotEq):
                    ok = a != b
                elif isinstance(op, ast.Lt):
                    ok = a < b
                elif isinstance(op, ast.LtE):
                    ok = a <= b
                elif isinstance(op, ast.Gt):
                    ok = a > b
                elif isinstance(op, ast.GtE):
                    ok = a >= b
                else:
                    raise ValueError("comparison")
                if not ok:
                    return 0
                a = b
            return 1
        if isinstance(node, ast.BoolOp):
            vals = [ev(v) for v in node.values]
            return int(all(vals)) if isinstance(node.op, ast.And) else int(any(vals))
        if isinstance(node, ast.IfExp):
            return ev(node.body) if ev(node.test) else ev(node.orelse)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("clog2", "bits"):
                args = [ev(a) for a in node.args]
                if node.func.id == "clog2":
                    n = max(1, args[0])
                    return (n - 1).bit_length()
                return max(1, args[0])
            raise ValueError("function")
        raise ValueError(f"unsupported expression node {type(node).__name__}")

    try:
        return int(ev(tree))
    except Exception:
        return None


def parse_param_decls(header: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in split_top_level(header):
        item = re.sub(r"\(\*.*?\*\)", " ", item, flags=re.S).strip()
        if not item:
            continue
        kind = "localparam" if re.match(r"\blocalparam\b", item) else "parameter"
        if "=" in item:
            left, right = item.split("=", 1)
            value = right.strip()
        else:
            left, value = item, None
        ids = IDENT_RE.findall(left)
        if not ids:
            continue
        name = ids[-1]
        # A trailing range/value comma is already split; strip obvious punctuation.
        out.append({"name": name, "value": value, "kind": kind, "source": item})
    return out


def _stmt_end(text: str, start: int) -> int:
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            return i
        i += 1
    return len(text)


def parse_body_param_decls(body: str) -> list[dict[str, Any]]:
    """Top-level/local `parameter` and `localparam` declarations in the module body.

    Port declarations such as `[LPARA_STRB_WD-1:0]` frequently depend on a
    localparam declared after the header, so the interface extractor must read
    those too.  The evaluator ignores constructs it cannot resolve (for example
    a Verilog ternary) and the dimension then falls back to the Yosys flat width.
    """
    out: list[dict[str, Any]] = []
    for m in re.finditer(r"\b(localparam|parameter)\b", body):
        end = _stmt_end(body, m.end())
        decl = body[m.start():end]
        kind = m.group(1)
        for item in split_top_level(decl):
            item = re.sub(r"^\s*(localparam|parameter)\b", "", item).strip()
            if not item or "=" not in item:
                continue
            left, right = item.split("=", 1)
            ids = IDENT_RE.findall(left)
            if not ids:
                continue
            out.append({
                "name": ids[-1],
                "value": right.strip(),
                "kind": kind,
                "source": item,
            })
    return out


def _scan_identifiers_and_brackets(text: str) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str]]]:
    """Top-level identifiers (outside brackets) and bracket spans, with positions."""
    idents: list[tuple[int, int, str]] = []
    brackets: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "[":
            depth = 1
            j = i + 1
            while j < len(text) and depth:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                j += 1
            brackets.append((i, j, text[i:j]))
            i = j
            continue
        if ch == "\\":
            j = i + 1
            while j < len(text) and not text[j].isspace():
                j += 1
            idents.append((i, j, text[i:j]))
            i = j
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_$]*", text[i:])
        if m:
            idents.append((i, i + m.end(), m.group(0)))
            i += m.end()
            continue
        i += 1
    return idents, brackets


def parse_port_decls(port_text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in split_top_level(port_text):
        item = re.sub(r"\(\*.*?\*\)", " ", item, flags=re.S).strip()
        m = re.match(r"^(input|output|inout)\b", item)
        if not m:
            continue  # non-ANSI or continuation; Yosys remains authoritative
        direction = m.group(1)
        rest = item[m.end():].strip()
        signed = bool(re.search(r"\bsigned\b", rest))
        # Drop a default value (not used by Q3TUI RTL) and unpacked dimensions
        # after the name are recorded but not used for the monitor declaration.
        idents, brackets = _scan_identifiers_and_brackets(rest)
        if not idents:
            continue
        name_start, name_end, name = idents[-1]
        packed = [b for b in brackets if b[1] <= name_start]
        unpacked = [b for b in brackets if b[0] >= name_end]
        out.append({
            "name": name,
            "direction": direction,
            "signed": signed,
            "dims_src": [b[2] for b in packed],
            "unpacked_dims": [b[2] for b in unpacked],
            "source": item,
        })
    return out


def resolve_dims(dims_src: list[str], env: dict[str, int]) -> tuple[list[str], int, bool]:
    """Resolve '[MSB:LSB]' dimensions to integer bounds. Returns (dims, width, ok)."""
    dims: list[str] = []
    width = 1
    ok = True
    for raw in dims_src:
        body = raw.strip()
        if not (body.startswith("[") and body.endswith("]")):
            ok = False
            dims.append(raw)
            continue
        body = body[1:-1]
        if ":" not in body:
            # [SIZE] is an unpacked dimension in port lists; do not treat it here.
            ok = False
            dims.append(raw)
            continue
        msb_s, lsb_s = body.split(":", 1)
        msb = const_eval(msb_s, env)
        lsb = const_eval(lsb_s, env)
        if msb is None or lsb is None:
            ok = False
            dims.append(raw)
            continue
        dims.append(f"[{msb}:{lsb}]")
        width *= abs(msb - lsb) + 1
    return dims, width, ok


def _decl_dims(dims: list[str]) -> str:
    return " ".join(dims)


def decl(port: dict[str, Any], name: str | None = None, direction: str = "input") -> str:
    """A SystemVerilog declaration of a monitor/harness signal."""
    name = name or port["name"]
    dims = _decl_dims(port.get("dims") or [])
    return f"{direction} logic {dims + ' ' if dims else ''}{name}"


def _find_top_source(sources: list[Path], top: str) -> Path:
    pattern = re.compile(r"\bmodule\s+" + re.escape(top) + r"\b")
    for path in sources:
        try:
            if pattern.search(strip_comments(path.read_text(encoding="utf-8"))):
                return path
        except OSError:
            continue
    raise FvError(f"top module '{top}' is not declared in the RTL sources: {', '.join(map(str, sources))}")


def yosys_port_widths(sources: list[Path], include_dirs: list[Path], defines: list[str],
                      top: str, out_json: Path, timeout_s: int = 180,
                      params: dict[str, int] | None = None) -> dict[str, Any]:
    exe = which_yosys()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    def qpath(p: Path) -> str:
        s = str(p)
        return f'"{s}"' if any(c.isspace() for c in s) else s

    parts = ["read_verilog -formal -sv -DSYNTHESIS -DVLSIT_SYNTHESIS"]
    for inc in include_dirs:
        parts.append(f"-I{qpath(inc)}")
    for d in defines:
        parts.append(f"-D{d}")
    for src in sources:
        parts.append(qpath(src))
    chparams = ""
    for name, value in (params or {}).items():
        chparams += f" -chparam {name} {value}"
    script = (
        " ".join(parts)
        + f"; hierarchy -top {top}{chparams}; proc; write_json {qpath(out_json)}"
    )
    res = run_cmd([exe, "-p", script], cwd=out_json.parent, timeout_s=timeout_s)
    text = res["output"]
    if res["timed_out"]:
        raise FvError(f"Yosys interface extraction timed out after {timeout_s}s")
    if not out_json.is_file():
        errors = "\n".join(line for line in text.splitlines() if "ERROR" in line or "error" in line)
        raise FvError(
            "Yosys could not elaborate the RTL for interface extraction.\n"
            + (errors or text[-3000:])
        )
    try:
        data = read_json(out_json)
    except FvError:
        raise FvError(f"Yosys wrote an invalid JSON file: {out_json}")
    modules = data.get("modules") or {}
    if top not in modules:
        raise FvError(f"Yosys JSON has no module '{top}' (modules: {', '.join(sorted(modules))[:500]})")
    return data


def extract_interface(manifest: dict[str, Any], out_json: Path | None = None) -> dict[str, Any]:
    root = project_root(manifest)
    top = manifest.get("top")
    if not top:
        raise FvError("design manifest has no `top` module")
    src = source_files(manifest)
    if src["missing"]:
        raise FvError("missing RTL source(s): " + ", ".join(src["missing"]))
    if not src["sources"]:
        raise FvError("design manifest has no RTL sources")
    top_source = _find_top_source(src["sources"], top)
    text = strip_comments(top_source.read_text(encoding="utf-8"))
    params_text, ports_text, body_text = module_header(text, top)

    parsed_params = parse_param_decls(params_text) + parse_body_param_decls(body_text)
    parsed_ports = parse_port_decls(ports_text)

    # Values from the final configuration override the RTL defaults.
    config = {}
    spec = manifest.get("spec") or {}
    cfg_path = resolve_path(root, spec["config"]) if spec.get("config") else None
    if cfg_path and cfg_path.is_file():
        cfg = read_json(cfg_path)
        for name, entry in (cfg.get("parameters") or {}).items():
            value = entry.get("value", entry.get("default")) if isinstance(entry, dict) else entry
            num = const_eval(str(value), {})
            if num is not None:
                config[name] = num
    for name, value in (manifest.get("parameters") or {}).items():
        num = const_eval(str(value), config)
        if num is None:
            raise FvError(f"parameter override {name}={value!r} is not an integer")
        config[name] = num

    env: dict[str, int] = dict(config)
    parameters: dict[str, int] = {}
    localparams: dict[str, int] = {}
    unresolved: list[str] = []
    # Iterate: localparams may depend on parameters and on earlier localparams.
    for _ in range(12):
        changed = False
        for p in parsed_params:
            name = p["name"]
            if p["kind"] == "parameter" and name in config:
                if parameters.get(name) != config[name]:
                    parameters[name] = config[name]
                    env[name] = config[name]
                    changed = True
                continue
            if p["kind"] == "parameter" and name in parameters:
                continue
            num = const_eval(p["value"], env) if p["value"] is not None else None
            if num is None:
                if name not in unresolved:
                    unresolved.append(name)
                continue
            target = parameters if p["kind"] == "parameter" else localparams
            if target.get(name) != num:
                target[name] = num
                env[name] = num
                changed = True
            if name in unresolved:
                unresolved.remove(name)
        if not changed:
            break
    # Any parameter with no value at all is a hard error (it would change widths).
    for p in parsed_params:
        if p["kind"] == "parameter" and p["name"] not in parameters:
            raise FvError(
                f"top parameter '{p['name']}' has no value; set it in final_config.json "
                "or in formal/design.json `parameters`"
            )

    out_json = out_json or (Path(manifest["_paths"]["interface"]).with_suffix(".yosys.json"))
    # Pass only explicit overrides as chparams; passing all resolved defaults
    # can trigger Yosys bufnorm issues on some designs.
    explicit_params = {k: v for k, v in parameters.items() if k in config}
    ydata = yosys_port_widths(src["sources"], src["include_dirs"], src["defines"], top, out_json,
                              params=explicit_params)
    mod = ydata["modules"][top]
    yports = mod.get("ports") or {}

    by_name = {p["name"]: p for p in parsed_ports}
    ports: list[dict[str, Any]] = []
    for name, yp in yports.items():
        flat_width = len(yp.get("bits") or []) or 1
        srcp = by_name.get(name)
        if srcp:
            dims, width, ok = resolve_dims(srcp.get("dims_src") or [], env)
            if ok and width == flat_width:
                ports.append({
                    "name": name,
                    "direction": yp.get("direction", srcp["direction"]),
                    "signed": bool(srcp.get("signed") or yp.get("signed")),
                    "width": flat_width,
                    "dims": dims,
                    "ndims": len(dims),
                    "resolved": True,
                    "source_dims": srcp.get("dims_src") or [],
                    "unpacked_dims": srcp.get("unpacked_dims") or [],
                })
                continue
            if srcp.get("dims_src"):
                # Keep the source dimensions only when they are trustworthy; otherwise
                # fall back to the flat Yosys width and record the reason.
                ports.append({
                    "name": name,
                    "direction": yp.get("direction", srcp["direction"]),
                    "signed": bool(srcp.get("signed") or yp.get("signed")),
                    "width": flat_width,
                    "dims": [f"[{flat_width - 1}:0]"] if flat_width > 1 else [],
                    "ndims": 1 if flat_width > 1 else 0,
                    "resolved": False,
                    "source_dims": srcp.get("dims_src") or [],
                    "note": "source dimensions did not resolve; using flat width",
                })
                continue
        ports.append({
            "name": name,
            "direction": yp.get("direction", "input"),
            "signed": bool(yp.get("signed")),
            "width": flat_width,
            "dims": [f"[{flat_width - 1}:0]"] if flat_width > 1 else [],
            "ndims": 1 if flat_width > 1 else 0,
            "resolved": False,
            "source_dims": [],
            "note": "port was not found in the ANSI header; using flat width",
        })

    return {
        "schema_version": "1.0",
        "top": top,
        "top_source": str(top_source.resolve()),
        "parameters": parameters,
        "localparams": localparams,
        "unresolved_constants": unresolved,
        "ports": ports,
        "sources": [str(p.resolve()) for p in src["sources"]],
        "include_dirs": [str(p.resolve()) for p in src["include_dirs"]],
        "defines": src["defines"],
        "yosys_json": str(Path(out_json).resolve()),
        "port_count": len(ports),
    }


def port_map(interface: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in interface.get("ports") or []}


def interface_text(interface: dict[str, Any], limit: int = 80) -> str:
    lines = [f"top {interface['top']}  ({interface['port_count']} ports)"]
    for p in interface.get("ports", [])[:limit]:
        dims = _decl_dims(p.get("dims") or [])
        lines.append(f"  {p['direction']:<6} {dims + ' ' if dims else ''}{p['name']}")
    if interface["port_count"] > limit:
        lines.append(f"  ... {interface['port_count'] - limit} more")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="extract the design interface")
    ap.add_argument("--design", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    from fv_common import load_manifest

    manifest = load_manifest(args.design)
    iface = extract_interface(manifest)
    if args.json:
        print(json.dumps(iface, indent=2))
    else:
        print(interface_text(iface))
