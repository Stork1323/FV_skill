"""Minimal waveform evidence for formal SVA runs.

Generates:
- per-property .svg waveform snapshot (human first-check)
- per-property .vcd (always present; real trace if available, else synthetic)
- per-property .gtkw and .signals.json for quick waveform debugging

This is intentionally dependency-light and stdlib-only.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from fv_common import project_root, read_json, rel_to, run_cmd, write_text


def _parse_vcd(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    vars_: list[dict[str, Any]] = []
    id2 = {}
    t = 0
    scope = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("$scope"):
            parts = line.split()
            scope.append(parts[2] if len(parts) > 2 else "")
        elif line.startswith("$upscope"):
            if scope:
                scope.pop()
        elif line.startswith("$var"):
            parts = line.split()
            if len(parts) >= 5:
                width = int(parts[2])
                vid = parts[3]
                name = parts[4]
                full = ".".join([s for s in scope if s] + [name])
                rec = {"id": vid, "name": full, "width": width, "samples": []}
                vars_.append(rec)
                id2[vid] = rec
        elif line.startswith("$dumpvars") or line.startswith("$enddefinitions"):
            pass
        elif line.startswith("#"):
            try:
                t = int(line[1:])
            except Exception:
                pass
        elif line[0] in "01xzXZbB" and len(line) >= 2 and line[1] != "[":
            val = line[0]
            vid = line[1:].strip()
            if vid in id2:
                id2[vid]["samples"].append((t, val))
        elif line[0] == "b":
            parts = line.split()
            if len(parts) == 2:
                val = parts[0][1:]
                vid = parts[1]
                if vid in id2:
                    id2[vid]["samples"].append((t, val))
        i += 1
    # normalize to per-signal timeline
    max_t = 0
    for v in vars_:
        if v["samples"]:
            max_t = max(max_t, max(tt for tt, _ in v["samples"]))
    for v in vars_:
        samp = sorted(v["samples"], key=lambda x: x[0])
        # forward fill
        out = []
        cur = "x" if v["width"] == 1 else "x"
        si = 0
        for tt in range(max_t + 1):
            while si < len(samp) and samp[si][0] <= tt:
                cur = samp[si][1]
                si += 1
            out.append(cur)
        v["timeline"] = out
    return {"max_t": max_t, "vars": vars_}


def render_svg(vcd: dict[str, Any], title: str = "waveform", max_signals: int = 24) -> str:
    vars_ = [v for v in vcd.get("vars", []) if v.get("timeline")]
    vars_ = vars_[:max_signals]
    max_t = max(1, int(vcd.get("max_t", 1)))
    # layout
    name_w = 260
    col_w = 28
    row_h = 28
    pad = 10
    width = name_w + (max_t + 1) * col_w + pad * 2
    height = pad * 2 + 40 + len(vars_) * row_h
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" font-family="monospace" font-size="12">']
    lines.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>')
    lines.append(f'<text x="{pad}" y="{pad+18}" font-size="14" font-weight="700">{_esc(title)}</text>')
    # grid
    top = pad + 35
    for i in range(max_t + 1):
        x = name_w + i * col_w
        lines.append(f'<line x1="{x}" y1="{top-10}" x2="{x}" y2="{height-pad}" stroke="#eee"/>')
        if i % 2 == 0:
            lines.append(f'<text x="{x+2}" y="{top-14}" fill="#666" font-size="10">{i}</text>')
    for r, v in enumerate(vars_):
        y = top + r * row_h
        lines.append(f'<text x="{pad}" y="{y+18}" fill="#111">{_esc(v["name"][-40:])}</text>')
        tl = v["timeline"]
        w = int(v.get("width", 1))
        if w == 1:
            # step line
            pts = []
            for i, val in enumerate(tl):
                x = name_w + i * col_w
                yv = y + (20 if str(val) in "0xXzZ" else 4)
                pts.append(f"{x},{yv}")
                x2 = name_w + (i + 1) * col_w
                pts.append(f"{x2},{yv}")
            lines.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#0a7" stroke-width="2"/>')
        else:
            for i, val in enumerate(tl):
                x = name_w + i * col_w
                lines.append(f'<rect x="{x}" y="{y+2}" width="{col_w-1}" height="22" fill="#f5faff" stroke="#8ab"/>')
                lines.append(f'<text x="{x+3}" y="{y+17}" fill="#135" font-size="10">{_esc(str(val)[:4])}</text>')
    lines.append('</svg>')
    return "\n".join(lines)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _synthetic_vcd(pid: str, status: str, prop: dict[str, Any]) -> str:
    clk = "clk"
    rst = "rst_n"
    sig = (prop.get("signal") or prop.get("expr") or "sig").split()[0]
    sig = re.sub(r"[^A-Za-z0-9_]", "_", sig)[:24] or "sig"
    lines = [
        "$timescale 1ns $end",
        "$scope module tb $end",
        f"$var wire 1 ! {clk} $end",
        f"$var wire 1 @ {rst} $end",
        f"$var wire 1 # {sig} $end",
        "$var wire 1 $ status_pass $end",
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "0!",
        "0@",
        "0#",
        "0$",
    ]
    for t in range(1, 11):
        lines.append(f"#{t}")
        lines.append("1!" if t % 2 == 1 else "0!")
        if t >= 5:
            lines.append("1@")
        if t >= 6:
            lines.append("1#")
        if t == 10:
            lines.append("1$" if status in {"PROVEN","BMC_PASS","COVERED","ASSUMED","VACUOUS"} else "0$")
    return "\n".join(lines) + "\n"


def _norm_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _trace_candidates(prop: dict[str, Any], root: Path, build: Path) -> list[Path]:
    pid = str(prop.get("id") or "")
    pid_norm = _norm_id(pid)
    found: list[Path] = []

    def add(path: Path) -> None:
        path = path if path.is_absolute() else root / path
        if path.is_file() and path not in found:
            found.append(path)

    for row in prop.get("counterexample") or []:
        text = str(row).strip()
        match = re.search(r"trace_(?:vcd|fst):\s*(.+)$", text)
        if match:
            add(Path(match.group(1).strip()))
        elif re.search(r"\.(?:vcd|fst)$", text, re.I):
            add(Path(text))
    for directory in build.rglob("*"):
        if not directory.is_dir():
            continue
        if pid_norm and pid_norm in _norm_id(directory.name):
            for ext in ("*.vcd", "*.fst"):
                for path in sorted(directory.rglob(ext)):
                    add(path)
    log = ((prop.get("bmc") or {}).get("log") or "")
    if log:
        log_path = root / log
        if log_path.parent.is_dir():
            for ext in ("*.vcd", "*.fst"):
                for path in sorted(log_path.parent.rglob(ext)):
                    add(path)
    found.sort(key=lambda p: (p.suffix.lower() != ".vcd", str(p)))
    return found


def _materialize_vcd(candidate: Path, out_vcd: Path) -> tuple[str, str]:
    suffix = candidate.suffix.lower()
    if suffix == ".vcd":
        out_vcd.write_bytes(candidate.read_bytes())
        return "trace", ""
    if suffix == ".fst":
        exe = shutil.which("fst2vcd")
        if exe:
            res = run_cmd([exe, "-o", str(out_vcd), str(candidate)],
                          cwd=candidate.parent, timeout_s=60)
            if res.get("returncode") == 0 and out_vcd.is_file() and out_vcd.stat().st_size:
                return "fst_converted", ""
            if out_vcd.exists():
                out_vcd.unlink()
        return "fst_unconverted", (
            "Found an FST trace but fst2vcd is not available; a synthetic VCD was "
            "written so the evidence still has both files.")
    return "unsupported_trace", f"unsupported trace format {candidate.suffix!r}"


def _expr_signals(prop: dict[str, Any]) -> list[str]:
    try:
        from fv_contract import expr_identifiers
    except Exception:
        expr_identifiers = None
    names: list[str] = []
    for key in ("expr", "trigger", "response", "when", "signal"):
        val = prop.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        if expr_identifiers:
            try:
                names.extend(expr_identifiers(val))
            except Exception:
                names.extend(re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", val))
        else:
            names.extend(re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", val))
    seen = set()
    out = []
    for n in names:
        if n in seen or n in {"prev", "rose", "fell", "stable", "past"}:
            continue
        seen.add(n)
        out.append(n)
    return out


def _vcd_var_names(vcd: dict[str, Any]) -> list[str]:
    return [str(v.get("name") or "") for v in (vcd.get("vars") or []) if v.get("name")]


def _pick_relevant_vcd_signals(prop: dict[str, Any], vcd: dict[str, Any], limit: int = 24) -> list[str]:
    wanted = _expr_signals(prop)
    for n in ("fv_start", "fv_cycle"):
        if n not in wanted:
            wanted.append(n)
    vnames = _vcd_var_names(vcd)
    if not vnames:
        return wanted[:limit]

    def norm(x: str) -> str:
        return re.sub(r"[^a-z0-9_]", "", x.lower())

    selected: list[str] = []
    seen = set()

    def add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            selected.append(name)

    for w in wanted:
        wn = norm(w)
        exact = [vn for vn in vnames if norm(vn) == wn or vn == w]
        if exact:
            for vn in exact[:3]:
                add(vn)
            continue
        suffix = [vn for vn in vnames if norm(vn).endswith(wn) or wn in norm(vn)]
        for vn in suffix[:2]:
            add(vn)

    for vn in vnames:
        lvn = vn.lower()
        if any(k in lvn for k in ("clk", "clock", "rst", "reset")) and len(selected) < limit:
            add(vn)
    if len(selected) < min(8, limit):
        for vn in vnames:
            if vn.startswith(("anyinit", "anyseq", "smt_")):
                continue
            add(vn)
            if len(selected) >= limit:
                break
    return selected[:limit]


def _write_gtkw(path: Path, vcd_path: Path, title: str, signals: list[str]) -> None:
    lines = [
        "[*] formal-sva waveform",
        f'[dumpfile] "{vcd_path.name}"',
        f'[savefile] "{path.name}"',
        "[timestart] 0",
        "[size] 1600 900",
        "@200",
        f"-{title}",
    ]
    for sig in signals:
        lines.append("@28")
        lines.append(sig)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _counterexample_summary(vcd: dict[str, Any], signals: list[str], max_steps: int = 8) -> dict[str, Any]:
    vars_by_name = {str(v.get("name")): v for v in (vcd.get("vars") or [])}
    max_t = int(vcd.get("max_t", 0))
    if max_t < 0:
        max_t = 0
    # Pick last steps; CEXs are usually near the end.
    start = max(0, max_t - max_steps + 1)
    steps = list(range(start, max_t + 1))
    rows = []
    for name in signals[:16]:
        v = vars_by_name.get(name)
        if not v:
            # try suffix match
            for vn, vv in vars_by_name.items():
                if vn.endswith(name) or name.endswith(vn):
                    v = vv
                    name = vn
                    break
        if not v:
            continue
        tl = v.get("timeline") or []
        vals = [str(tl[t]) if t < len(tl) else "?" for t in steps]
        rows.append({"signal": name, "values": vals})
    return {"times": steps, "rows": rows}


def generate_for_results(manifest: dict[str, Any], results: dict[str, Any] | None = None) -> dict[str, Any]:
    root = project_root(manifest)
    results_path = Path(manifest["_paths"]["results"])
    if results is None:
        if not results_path.is_file():
            return {"generated": 0, "artifacts": []}
        results = read_json(results_path)
    build = Path(manifest["_paths"]["build"])
    reports = Path(manifest["_paths"]["reports"])
    wave_dir = reports / "waveforms"
    wave_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for prop in results.get("properties") or []:
        pid = str(prop.get("id", "?"))
        status = str(prop.get("status", "UNKNOWN"))
        candidates = _trace_candidates(prop, root, build)
        cand = candidates[0] if candidates else None
        entry = {"property": pid, "status": status, "kind": prop.get("kind"), "role": prop.get("role")}
        out_svg = wave_dir / f"{pid}.svg"
        out_vcd = wave_dir / f"{pid}.vcd"
        out_gtkw = wave_dir / f"{pid}.gtkw"
        out_signals = wave_dir / f"{pid}.signals.json"
        try:
            if cand and cand.is_file():
                kind, note = _materialize_vcd(cand, out_vcd)
                entry["vcd_source"] = rel_to(root, cand)
                entry["vcd_kind"] = kind
                if note:
                    entry["note"] = note
                if kind not in ("trace", "fst_converted"):
                    write_text(out_vcd, _synthetic_vcd(pid, status, prop))
                    entry["vcd_kind"] = "synthetic_no_trace"
            else:
                write_text(out_vcd, _synthetic_vcd(pid, status, prop))
                entry["vcd_kind"] = "synthetic_no_trace"
                entry["note"] = "No backend trace VCD found; generated synthetic sanity VCD so evidence always includes both VCD and snapshot."
            vcd = _parse_vcd(out_vcd)
            svg = render_svg(vcd, title=f"{pid} {status} ({entry.get('vcd_kind','vcd')})")
            write_text(out_svg, svg)
            signals = _pick_relevant_vcd_signals(prop, vcd)
            summary = _counterexample_summary(vcd, signals)
            _write_gtkw(out_gtkw, out_vcd, f"{pid} {status}", signals)
            write_text(out_signals, json.dumps({"property": pid, "status": status, "signals": signals, "expr_signals": _expr_signals(prop), "counterexample_summary": summary}, indent=2) + "\n")
            entry["vcd"] = rel_to(root, out_vcd)
            entry["svg"] = rel_to(root, out_svg)
            entry["gtkw"] = rel_to(root, out_gtkw)
            entry["signals_json"] = rel_to(root, out_signals)
            entry["signals_of_interest"] = signals
            entry["counterexample_summary"] = summary
            entry["evidence"] = "waveform+vcd+gtkw"
        except Exception as exc:
            entry["evidence"] = "waveform_error"
            entry["error"] = str(exc)
            try:
                if not out_vcd.exists():
                    write_text(out_vcd, _synthetic_vcd(pid, status, prop))
                    entry["vcd"] = rel_to(root, out_vcd)
                if not out_svg.exists():
                    write_text(out_svg, f"<svg xmlns='http://www.w3.org/2000/svg' width='900' height='80'><text x='10' y='30'>{pid} {status}</text><text x='10' y='55'>waveform error: {exc}</text></svg>")
                    entry["svg"] = rel_to(root, out_svg)
            except Exception:
                pass
        artifacts.append(entry)
    index = {"schema_version": "1.0", "generated": len(artifacts), "artifacts": artifacts}
    write_text(wave_dir / "index.json", json.dumps(index, indent=2) + "\n")
    return index
