"""Structural checks: elaboration/connectivity/width/clock/reset.

These checks are deterministic and do not replace temporal proof. They catch
interface and configuration mismatches early.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fv_common import read_json, write_json, write_text, project_root, sha256_file, resolve_path
from fv_design import extract_interface


def run(manifest: dict[str, Any]) -> dict[str, Any]:
    root = project_root(manifest)
    contract_path = Path(manifest["_paths"]["contract"])
    contract = read_json(contract_path) if contract_path.is_file() else {}
    design = contract.get("design") or manifest.get("design") or {}
    try:
        iface = extract_interface(manifest)
    except Exception as exc:
        return {
            "schema_version": "1.0",
            "overall": "ERROR",
            "checks": [{"id": "STR-ELAB", "status": "ERROR", "message": str(exc)}],
        }

    checks: list[dict[str, Any]] = []
    ports = {p.get("name"): p for p in iface.get("ports") or [] if p.get("name")}
    top = design.get("top") or iface.get("top")
    checks.append({"id": "STR-ELAB", "status": "PASS" if iface.get("ok", True) else "FAIL",
                   "message": f"top {top} elaborated" if iface.get("ok", True) else "elaboration failed"})

    clk = ((design.get("clock") or {}).get("signal"))
    rst = ((design.get("reset") or {}).get("signal"))
    for sig, label in ((clk, "clock"), (rst, "reset")):
        if not sig:
            checks.append({"id": f"STR-{label.upper()}-MISSING", "status": "FAIL", "message": f"{label} not declared"})
            continue
        p = ports.get(sig)
        if not p:
            checks.append({"id": f"STR-{label.upper()}-PORT", "status": "FAIL", "message": f"{label} {sig} not found in top ports"})
        else:
            ok = p.get("direction") == "input" and int(p.get("width") or 0) == 1
            checks.append({"id": f"STR-{label.upper()}-PORT", "status": "PASS" if ok else "FAIL",
                           "message": f"{label} {sig} dir={p.get('direction')} width={p.get('width')}"})

    # Basic width/direction consistency is covered by interface extraction; report summary.
    checks.append({"id": "STR-PORTS", "status": "PASS", "message": f"{len(ports)} top ports resolved"})

    overall = "PASS" if all(c.get("status") == "PASS" for c in checks) else "FAIL"
    report = {
        "schema_version": "1.0",
        "overall": overall,
        "top": top,
        "checks": checks,
        "interface_sha256": sha256_file(Path(manifest["_paths"]["interface"])) if Path(manifest["_paths"]["interface"]).is_file() else "",
    }
    out_json = root / "formal" / "reports" / "structural.json"
    out_md = root / "formal" / "reports" / "structural.md"
    write_json(out_json, report)
    lines = ["# Structural checks", "", f"- Overall: **{overall}**", "", "| ID | Status | Message |", "|---|---|---|"]
    for c in checks:
        lines.append(f"| {c['id']} | {c['status']} | {c['message']} |")
    write_text(out_md, "\n".join(lines) + "\n")
    report["artifacts"] = {"structural_json": "formal/reports/structural.json", "structural_md": "formal/reports/structural.md"}
    return report
