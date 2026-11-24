"""Configuration matrix reporting.

P2: execute/report configuration closure. This module reports declared
configurations and whether the current results cover them. Full multi-config
execution can be driven by running fv.py with per-config design overrides.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fv_common import project_root, read_json, write_json, write_text, rel_to


def report(manifest: dict[str, Any]) -> dict[str, Any]:
    root = project_root(manifest)
    contract = read_json(Path(manifest["_paths"]["contract"])) if Path(manifest["_paths"]["contract"]).is_file() else {}
    results_path = Path(manifest["_paths"]["results"])
    results = read_json(results_path) if results_path.is_file() else {}
    scope = contract.get("scope") or {}
    cfgs = scope.get("configurations") or []
    current = scope.get("configuration") or (contract.get("design") or {}).get("parameters") or {}
    if not cfgs and current:
        cfgs = [current]
    covered = [current] if results else []
    rows = []
    for i, cfg in enumerate(cfgs):
        is_current = cfg == current
        rows.append({
            "index": i,
            "configuration": cfg,
            "covered_by_current_run": bool(is_current and results),
            "status": "COVERED" if (is_current and results and (results.get("summary") or {}).get("overall") == "PASS") else ("EXECUTED" if is_current and results else "NOT_EXECUTED"),
        })
    doc = {
        "schema_version": "1.0",
        "overall": "PASS" if rows and all(r["status"] in ("COVERED", "EXECUTED") for r in rows) else ("INCOMPLETE" if rows else "NO_CONFIGS"),
        "declared_configurations": len(rows),
        "current_configuration": current,
        "rows": rows,
    }
    out_json = root / "formal" / "reports" / "config_matrix.json"
    out_md = root / "formal" / "reports" / "config_matrix.md"
    write_json(out_json, doc)
    lines = ["# Configuration matrix", "", f"- Overall: **{doc['overall']}**", "", "| # | Status | Covered by current run | Configuration |", "|---:|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['index']} | {r['status']} | {'yes' if r['covered_by_current_run'] else 'no'} | `{r['configuration']}` |")
    write_text(out_md, "\n".join(lines) + "\n")
    doc["artifacts"] = {"config_matrix_json": rel_to(root, out_json), "config_matrix_md": rel_to(root, out_md)}
    return doc
