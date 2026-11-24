"""Replay SBY/Yosys counterexample traces with Icarus Verilog.

This is a best-effort replay for debugging: it adapts SBY-generated trace_tb.v
files to plain simulation by wiring the DUT clock to the testbench clock and
removing formal-only witness initialization. It does not replace formal proof.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fv_common import FvError, project_root, read_json, rel_to, resolve_path, run_cmd, write_text


def available() -> bool:
    return shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def _adapt_sby_trace_tb(text: str, dut_top: str | None = None) -> str:
    s = text
    # Wire DUT clock to testbench-generated clock.
    s = s.replace("  reg [0:0] PI_i_clk;\n", "  wire PI_i_clk = clock;\n")
    s = re.sub(r"^\s*PI_i_clk\s*(?:<=|=)\s*[^;]+;\s*\n", "", s, flags=re.M)
    # Remove formal-only witness anyinit assignments; keep a comment.
    # Remove formal-only witness anyinit assignments.
    s = re.sub(r"^\s*UUT\._witness_\..*;\s*\n", "", s, flags=re.M)
    return s


def _find_trace_tb(build: Path) -> Path | None:
    tbs = sorted(build.rglob("trace_tb.v"), key=lambda p: p.stat().st_mtime, reverse=True)
    return tbs[0] if tbs else None


def replay_property(manifest: dict[str, Any], property_id: str, timeout_s: int = 60) -> dict[str, Any]:
    if not available():
        raise FvError("replay requires iverilog and vvp on PATH")
    root = project_root(manifest)
    results_path = Path(manifest["_paths"]["results"])
    results = read_json(results_path)
    prop = next((p for p in results.get("properties") or [] if str(p.get("id")) == property_id), None)
    if not prop:
        raise FvError(f"property not found in results: {property_id}")
    if prop.get("status") not in ("FAIL",):
        return {"id": property_id, "status": "SKIPPED", "reason": f"property status is {prop.get('status')}; replay expects FAIL"}

    bmc = prop.get("bmc") or {}
    log_rel = bmc.get("log") or ""
    log_path = root / log_rel
    build = Path(manifest["_paths"]["build"]) / "sby" / str(prop.get("id", "")).lower().replace("-", "_")
    if not build.exists():
        # try yosys layout
        build = Path(manifest["_paths"]["build"]) / "sat" / str(prop.get("id", "")).lower().replace("-", "_")
    tb = _find_trace_tb(build)
    if not tb:
        return {"id": property_id, "status": "UNAVAILABLE", "reason": "no trace_tb.v found; replay currently supports SBY trace_tb.v"}

    harness = read_json(Path(manifest["_paths"]["sva"]) / "generated" / "fv_manifest.json")
    hfiles = harness.get("files") or {}
    # gather sources from SBY script if present
    sby = next(build.rglob("bmc.sby"), None)
    sources: list[Path] = []
    if sby:
        txt = sby.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"read_verilog[^\n]*", txt)
        if m:
            for tok in m.group(0).split()[2:]:
                if tok.startswith("-"):
                    continue
                p = Path(tok)
                if not p.is_absolute():
                    p = (sby.parent / p).resolve()
                if p.suffix in (".sv", ".v", ".svh") and p.is_file():
                    sources.append(p)
    if not sources:
        sources = [Path(p) for p in hfiles.get("design", []) + hfiles.get("monitors", [])]
        sources = [p if p.is_absolute() else root / p for p in sources]

    adapted = _adapt_sby_trace_tb(tb.read_text(encoding="utf-8", errors="replace"))
    adapted_path = build / "replay_tb.v"
    write_text(adapted_path, adapted)
    out = build / "replay.out"
    cmd = ["iverilog", "-g2012", "-s", "testbench", "-o", str(out), *[str(s) for s in sources], str(adapted_path)]
    comp = run_cmd(cmd, cwd=root, timeout_s=timeout_s)
    if comp.get("returncode") != 0:
        return {"id": property_id, "status": "ERROR", "phase": "compile", "output": (comp.get("output") or "")[-2000:]}
    run = run_cmd(["vvp", str(out)], cwd=root, timeout_s=timeout_s)
    output = run.get("output") or ""
    failed = bool(re.search(r"(ASSERT|ERROR|FATAL|assertion)", output, re.I)) or run.get("returncode") not in (0, None)
    return {
        "id": property_id,
        "status": "REPLAYED_FAIL" if failed else "REPLAYED_NO_FAIL",
        "command": cmd,
        "compile_returncode": comp.get("returncode"),
        "run_returncode": run.get("returncode"),
        "output": output[-4000:],
        "adapted_tb": rel_to(root, adapted_path),
        "source_tb": rel_to(root, tb),
    }
