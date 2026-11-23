"""Verification plan artifact.

Records requirement-to-checker mapping, proof dependencies, configuration scope,
reset plan, exclusions, and closure criteria.  The plan is derived from the
contract and bundle; it is a planning/signoff artifact, not a proof.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fv_common import project_root, read_json, rel_to, sha256_file, write_json, write_text


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build(manifest: dict[str, Any], contract: dict[str, Any] | None = None,
          bundle: dict[str, Any] | None = None, results: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    contract = contract or read_json(paths["contract"])
    bundle = bundle or read_json(paths["bundle"])
    if results is None and paths["results"].is_file():
        results = read_json(paths["results"])
    results = results or {}

    props_by_req: dict[str, list[dict[str, Any]]] = {}
    for p in contract.get("properties") or []:
        props_by_req.setdefault(str(p.get("req")), []).append(p)
    res_by_id = {str(p.get("id")): p for p in results.get("properties") or []}

    req_rows = []
    for req in bundle.get("requirements") or []:
        rid = str(req.get("id"))
        c_req = next((r for r in contract.get("requirements") or [] if str(r.get("id")) == rid), {})
        plist = props_by_req.get(rid, [])
        checks = []
        for p in plist:
            r = res_by_id.get(str(p.get("id"))) or {}
            checks.append({
                "property": p.get("id"),
                "kind": p.get("kind"),
                "role": p.get("role"),
                "obligation": p.get("obligation"),
                "status": r.get("status"),
                "proof_method": (p.get("proof") or {}).get("method"),
            })
        req_rows.append({
            "requirement": rid,
            "feature": req.get("feature"),
            "disposition": c_req.get("disposition"),
            "owner": c_req.get("owner"),
            "checkers": checks,
            "proof_dependencies": [h for p in plist for h in ((p.get("proof") or {}).get("helpers") or [])],
            "closure": "closed" if c_req.get("disposition") in ("not_formalizable", "out_of_scope")
                       else "open",
        })

    scope = contract.get("scope") or {}
    plan = {
        "schema_version": "1.0",
        "generated_at": _now(),
        "design": contract.get("design") or {},
        "contract_sha256": sha256_file(paths["contract"]) if paths["contract"].is_file() else "",
        "config_scope": scope.get("configuration") or {},
        "configurations": scope.get("configurations") or [],
        "reset_plan": scope.get("reset_plan") or [{
            "name": "cold_reset", "description": "Reset asserted for configured reset_cycles before checks arm."
        }],
        "exclusions": scope.get("exclusions") or [],
        "requirements": req_rows,
        "closure_criteria": [
            "All formalized requirements have discharged required assertions for the declared configuration.",
            "All required covers are reached or explicitly proven unreachable.",
            "All assumptions are satisfiable and reviewed for ownership/vacuity.",
            "Review has no blocking ACTION_REQUIRED findings.",
            "Regression baseline is updated only after review and signoff decision.",
        ],
    }
    return plan


def markdown(plan: dict[str, Any]) -> str:
    d = plan.get("design") or {}
    lines = [
        f"# Verification plan: {d.get('name') or d.get('top')}",
        "",
        f"- **Top:** `{d.get('top')}`",
        f"- **Generated:** {plan.get('generated_at')}",
        f"- **Configuration:** `{plan.get('config_scope')}`",
        "",
        "## Requirement-to-checker mapping",
        "",
        "| Requirement | Disposition | Checkers | Dependencies | Closure |",
        "|---|---|---|---|---|",
    ]
    for r in plan.get("requirements") or []:
        checks = ", ".join(f"`{c['property']}`({c.get('status') or 'pending'})" for c in r.get("checkers") or [])
        deps = ", ".join(f"`{x}`" for x in r.get("proof_dependencies") or [])
        lines.append(f"| `{r['requirement']}` | {r.get('disposition')} | {checks} | {deps} | {r.get('closure')} |")
    lines += ["", "## Reset plan", ""]
    for item in plan.get("reset_plan") or []:
        lines.append(f"- **{item.get('name')}**: {item.get('description','')}")
    lines += ["", "## Exclusions", ""]
    for item in plan.get("exclusions") or []:
        lines.append(f"- {item}")
    if not plan.get("exclusions"):
        lines.append("- (none declared)")
    lines += ["", "## Closure criteria", ""]
    for item in plan.get("closure_criteria") or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write(manifest: dict[str, Any], plan: dict[str, Any]) -> dict[str, str]:
    root = project_root(manifest)
    reports = Path(manifest["_paths"]["reports"])
    paths = {"plan_json": reports / "verification_plan.json", "plan_md": reports / "verification_plan.md"}
    plan["artifacts"] = {k: rel_to(root, v) for k, v in paths.items()}
    write_json(paths["plan_json"], plan)
    write_text(paths["plan_md"], markdown(plan))
    return plan["artifacts"]
