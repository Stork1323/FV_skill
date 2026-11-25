"""Waveform snapshot evidence for SVA/properties.

Best-effort: parses VCD and writes a compact SVG snapshot for human first-check.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fv_common import project_root, read_json, rel_to, write_text


def _parse_vcd(path: Path, max_signals: int = 12) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    # very small VCD parser
    signals: dict[str, dict[str, Any]] = {}
    id_to_name: dict[str, str] = {}
    scope: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("$scope"):
            parts = line.split()
            if len(parts) >= 3:
                scope.append(parts[2])
        elif line.startswith("$upscope"):
            if scope:
                scope.pop()
        elif line.startswith("$var"):
            parts = line.split()
            if len(parts) >= 5:
                width = int(parts[2])
                vid = parts[3]
                name = parts[4]
                full = ".".join(scope + [name]) if scope else name
                id_to_name[vid] = full
                signals[full] = {"id": vid, "width": width, "changes": []}
        elif line.startswith("$enddefinitions"):
            break
    # parse changes
    time = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            try:
                time = int(line[1:])
            except Exception:
                pass
            continue
        if line[0] in "01xXzZ":
            val = line[0]
            vid = line[1:]
            name = id_to_name.get(vid)
            if name and name in signals:
                signals[name]["changes"].append((time, val))
        elif line[0] in "bBrR":
            parts = line.split()
            if len(parts) == 2:
                val = parts[0][1:]
                vid = parts[1]
                name = id_to_name.get(vid)
                if name and name in signals:
                    signals[name]["changes"].append((time, val))
    # pick interesting signals
    preferred = []
    others = []
    for name, sig in signals.items():
        lname = name.lower()
        score = 0
        if any(k in lname for k in ["clk", "clock"]): score += 5
        if any(k in lname for k in ["rst", "reset"]): score += 5
        if any(k in lname for k in ["req", "ack", "valid", "ready", "inc", "q", "count"]): score += 3
        if sig["width"] <= 8: score += 1
        (preferred if score > 0 else others).append(( -score, name))
    preferred.sort(); others.sort()
    names = [n for _, n in preferred[:max_signals]]
    if len(names) < max_signals:
        names += [n for _, n in others[:max_signals-len(names)]]
    out = {n: signals[n] for n in names}
    times = sorted({t for s in out.values() for t,_ in s["changes"]})
    return {"signals": out, "times": times}


def render_svg(vcd: dict[str, Any], title: str = "waveform") -> str:
    sigs = list(vcd.get("signals", {}).items())
    times = vcd.get("times") or [0]
    tmax = max(times) if times else 1
    tmin = min(times) if times else 0
    span = max(1, tmax - tmin)
    W, H = 1000, 40 + 28 * max(1, len(sigs))
    left, right = 220, 980
    def x(t): return left + (right-left) * (t-tmin) / span
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="monospace" font-size="11">',
             f'<rect width="100%" height="100%" fill="#fff"/>',
             f'<text x="10" y="20" font-size="14">{title}</text>']
    for i, (name, sig) in enumerate(sigs):
        y0 = 40 + i*28
        lines.append(f'<text x="10" y="{y0+18}">{name[-35:]}</text>')
        lines.append(f'<line x1="{left}" y1="{y0+20}" x2="{right}" y2="{y0+20}" stroke="#ddd"/>')
        changes = sorted(sig.get("changes") or [(tmin, "0")])
        # build step waveform
        for j, (t, val) in enumerate(changes):
            t2 = changes[j+1][0] if j+1 < len(changes) else tmax
            x1, x2 = x(t), x(t2)
            if sig.get("width",1) == 1:
                y = y0+4 if str(val) in "1" else y0+20
                y_prev = y0+4 if j>0 and str(changes[j-1][1]) in "1" else y0+20
                if j>0:
                    lines.append(f'<line x1="{x1}" y1="{y_prev}" x2="{x1}" y2="{y}" stroke="#111"/>')
                lines.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="#111"/>')
            else:
                lines.append(f'<rect x="{x1}" y="{y0+4}" width="{max(1,x2-x1)}" height="16" fill="none" stroke="#111"/>')
                lines.append(f'<text x="{x1+3}" y="{y0+16}" font-size="10">{val}</text>')
    lines.append('</svg>')
    return "\n".join(lines)


def _synthetic_vcd(pid: str, status: str, prop: dict[str, Any]) -> str:
    # Minimal VCD to satisfy "both VCD and snapshot" evidence requirement.
    # Includes clock/reset/fv_start and a status note; not a counterexample proof.
    clk = "clk"
    rst = "rst_n"
    lines = [
        "$date today $end",
        "$version formal-sva-synthetic $end",
        "$timescale 1ns $end",
        "$scope module tb $end",
        "$var wire 1 ! clk $end",
        "$var wire 1 @ rst_n $end",
        "$var wire 1 # fv_start $end",
        "$var wire 1 $ pass_or_fail $end",
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "0!",
        "0@",
        "0#",
        "0$",
    ]
    # reset for 2 cycles, then run
    for t in range(1, 11):
        lines.append(f"#{t}")
        lines.append("1!" if t % 2 == 1 else "0!")
        if t >= 5:
            lines.append("1@")
        if t >= 6:
            lines.append("1#")
        # mark pass/fail bit at end
        if t == 10:
            lines.append("1$" if status in {"PROVEN","BMC_PASS","COVERED","ASSUMED","VACUOUS"} else "0$")
    return "\n".join(lines) + "\n"


def generate_for_results(manifest: dict[str, Any]) -> dict[str, Any]:
    root = project_root(manifest)
    results_path = Path(manifest["_paths"]["results"])
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
        # find VCD
        pat = pid.lower().replace('-', '_')
        candidates = list(build.rglob("**/trace.vcd")) + list(build.rglob("**/*.vcd"))
        cand = None
        # prefer property-specific path
        for c in candidates:
            cs = str(c).lower()
            if pat and pat in cs:
                cand = c
                break
        if cand is None:
            # try trace from BMC log dir
            log = ((prop.get("bmc") or {}).get("log") or "")
            if log:
                lp = root / log
                for c in lp.parent.glob("**/*.vcd"):
                    cand = c; break
        entry = {"property": pid, "status": status, "kind": prop.get("kind"), "role": prop.get("role")}
        out_svg = wave_dir / f"{pid}.svg"
        out_vcd = wave_dir / f"{pid}.vcd"
        try:
            if cand and cand.is_file():
                # copy/normalize VCD into waveform evidence dir
                out_vcd.write_bytes(cand.read_bytes())
                entry["vcd_source"] = rel_to(root, cand)
                entry["vcd_kind"] = "trace"
            else:
                # Requirement: always have both VCD and snapshot.
                # Generate a minimal synthetic VCD when no trace exists.
                synth = _synthetic_vcd(pid, status, prop)
                write_text(out_vcd, synth)
                entry["vcd_kind"] = "synthetic_no_trace"
                entry["note"] = "No backend trace VCD found; generated synthetic sanity VCD so evidence always includes both VCD and snapshot."
            vcd = _parse_vcd(out_vcd)
            svg = render_svg(vcd, title=f"{pid} {status} ({entry.get('vcd_kind','vcd')})")
            write_text(out_svg, svg)
            entry["vcd"] = rel_to(root, out_vcd)
            entry["svg"] = rel_to(root, out_svg)
            entry["evidence"] = "waveform+vcd"
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
    write_text(wave_dir / "index.json", __import__("json").dumps(index, indent=2) + "\n")
    return index
