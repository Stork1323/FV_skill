"""Post-run review of the SVA and constraints themselves.

Verification answers "does the RTL satisfy these properties?".  This review
answers the question that comes next: "are these properties and constraints
themselves trustworthy?".  It reports fixed, machine-readable findings:

* unsatisfiable or vacuous assumptions,
* unreachable covers,
* assertions whose trigger has no matching cover (a bounded pass may be vacuous),
* constant / tautological / duplicated properties,
* generated-SVA leakage (unresolved parameters, raw temporal syntax),
* bounded-only results with no proof.

Every finding names a category, a severity, an owner and a recommendation so a
verification engineer can act without re-reading the contract.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    normalize_text,
    project_root,
    read_json,
    rel_to,
    sha256_file,
    write_json,
    write_text,
)
from fv_contract import load_contract

REVIEW_SCHEMA_VERSION = "1.0"
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _const(expr: Any) -> bool:
    text = re.sub(r"[\s()]", "", str(expr or "")).lower()
    return text in {"1", "0", "1'b1", "1'b0", "1'h1", "1'h0", "'1", "'0",
                    "4'b1111", "4'b0000"}


def _norm(expr: Any) -> str:
    return re.sub(r"\s+", "", str(expr or "")).lower()


def _sva_files(manifest: dict[str, Any], harness: dict[str, Any] | None) -> list[Path]:
    root = project_root(manifest)
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    out: list[Path] = []
    if harness:
        for raw in harness.get("generated_files") or []:
            p = Path(raw)
            if not p.is_absolute():
                p = root / raw
            if p.suffix == ".sv" and re.search(r"(_monitor|assumptions|sva_checker)", p.name):
                out.append(p)
    gen = paths["sva"] / "generated"
    if gen.is_dir():
        for p in gen.glob("*.sv"):
            if re.search(r"(_monitor|assumptions|sva_checker)", p.name) and p not in out:
                out.append(p)
    return sorted(set(out))


def review(manifest: dict[str, Any], results: dict[str, Any] | None = None,
           contract_path: Path | None = None) -> dict[str, Any]:
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    if results is None:
        if not paths["results"].is_file():
            raise FvError(f"results not found: {paths['results']}; run `fv.py run` first")
        results = read_json(paths["results"])
    contract, bundle, interface = load_contract(manifest, contract_path)
    props = list(contract.get("properties") or [])
    reqs = {str(r.get("id")): r for r in contract.get("requirements") or []}
    by_id = {str(p.get("id")): p for p in props}
    run_props = {str(p.get("id")): p for p in results.get("properties") or []}
    findings: list[dict[str, Any]] = []

    def add(code: str, severity: str, category: str, message: str,
            recommendation: str, prop: dict[str, Any] | None = None,
            owner: str = "verification_engineer") -> None:
        findings.append({
            "id": f"REV-{len(findings) + 1:03d}",
            "code": code,
            "severity": severity,
            "category": category,
            "property": (prop or {}).get("id"),
            "requirement": (prop or {}).get("req"),
            "owner": owner,
            "message": message,
            "recommendation": recommendation,
        })

    # -- assumptions and covers ----------------------------------------------------------------
    assumes = [p for p in props if p.get("role") == "assume"]
    covers = [p for p in props if p.get("role") == "cover"]
    for p in assumes:
        run = run_props.get(str(p.get("id"))) or {}
        if run.get("status") == "VACUOUS":
            add("CONSTRAINT_UNSAT", "error", "constraint",
                f"assumption {p.get('id')} is not reachable with its own guard.",
                "Remove or weaken the guard, or add a cover that demonstrates the "
                "environment can satisfy the assumption.", p)
        if not p.get("when"):
            add("CONSTRAINT_UNGUARDED", "warning", "constraint",
                f"assumption {p.get('id')} constrains every non-reset cycle.",
                "Add a `when` guard unless the rule really is global; an unguarded "
                "assumption can hide large parts of the state space.", p)
        if _const(p.get("expr")):
            add("CONSTRAINT_CONSTANT", "warning", "constraint",
                f"assumption {p.get('id')} is constant and constrains nothing or "
                "everything.",
                "Replace the constant with a signal-based environment rule or remove it.", p)
    for p in covers:
        run = run_props.get(str(p.get("id"))) or {}
        if run.get("status") == "UNREACHABLE":
            add("COVER_UNREACHABLE", "warning", "coverage",
                f"cover {p.get('id')} was not reachable within depth "
                f"{run.get('depth')}.",
                "Confirm the scenario is legal, add stimulus assumptions, or increase "
                "the depth.", p)
    if assumes and not covers:
        add("COVERAGE_NO_COVER", "warning", "coverage",
            "the contract has assumptions but no cover properties.",
            "Add a cover for every assumption guard and every assertion trigger so "
            "vacuous passes are visible.")

    # -- assertions ----------------------------------------------------------------------------
    asserts = [p for p in props if p.get("role") == "assert"]
    cover_exprs = {_norm(p.get("expr")) for p in covers}
    for p in asserts:
        run = run_props.get(str(p.get("id"))) or {}
        kind = p.get("kind")
        trigger = p.get("trigger")
        guard = p.get("when")
        if kind in ("next", "response", "stability") and trigger:
            if _norm(trigger) not in cover_exprs:
                add("TRIGGER_UNCOVERED", "warning", "coverage",
                    f"{p.get('id')} asserts a trigger but no cover reaches that trigger.",
                    "Add a cover property with the same trigger; otherwise a bounded "
                    "pass may be vacuous.", p)
        if kind == "always" and guard and _norm(guard) not in cover_exprs:
            add("GUARD_UNCOVERED", "warning", "coverage",
                f"{p.get('id')} is guarded by `{guard}` but no cover reaches the guard.",
                "Add a cover for the guard so the assertion is demonstrated to run.", p)
        for key in ("expr", "trigger", "response"):
            value = p.get(key)
            if value and _const(value):
                add("PROPERTY_CONSTANT", "warning", "sva",
                    f"{p.get('id')}.{key} is a constant ({value}); the check is "
                    "trivial or unreachable.",
                    "Replace the constant with the signal expression from the "
                    "requirement.", p)
        if kind == "always" and _const(p.get("expr")):
            add("PROPERTY_TAUTOLOGY", "error", "sva",
                f"{p.get('id')} asserts a constant and can never constrain the RTL.",
                "Remove the property or formalize the actual requirement.", p)

    signatures: dict[str, str] = {}
    for p in props:
        sig = json.dumps({k: p.get(k) for k in
                          ("role", "kind", "req", "trigger", "expr", "response",
                           "signal", "when", "min_delay", "max_delay", "cycles")},
                         sort_keys=True)
        if sig in signatures:
            add("PROPERTY_DUPLICATE", "warning", "sva",
                f"{p.get('id')} is identical to {signatures[sig]}.",
                "Keep one property, or make both cite distinct requirements and "
                "different expressions.", p)
        else:
            signatures[sig] = str(p.get("id"))

    if asserts and not any(run_props.get(str(p.get("id")), {}).get("status") == "PROVEN"
                           for p in asserts):
        add("BOUNDED_ONLY", "info", "sva",
            "no assertion was proved by induction; every assertion is BMC-bounded.",
            "Increase depth, add invariants, or accept the bounded result explicitly "
            "in the report.")
    if not covers:
        add("COVERAGE_NONE", "warning", "coverage",
            "the contract has no cover property.",
            "Add at least one cover per feature so unreachable behavior is visible.")

    # -- generated SVA hygiene -----------------------------------------------------------------
    try:
        from fv_yosys import load_harness
        harness = load_harness(manifest)
    except FvError:
        harness = None
    params = list((interface.get("parameters") or {}).keys())
    for path in _sva_files(manifest, harness):
        try:
            text = path.read_text()
        except OSError:
            continue
        rel = rel_to(project_root(manifest), path)
        for name in params:
            if re.search(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])", text):
                add("SVA_PARAM_LEFTOVER", "error", "sva",
                    f"generated file {rel} still contains parameter {name}; the "
                    "parameter was not baked into the check.",
                    "Re-run `fv.py render`; if it persists, report a renderer bug with "
                    "the contract and interface.")
        if re.search(r"\|->|\|=>|##\s*\[", text) and "sva_checker" not in path.name:
            add("SVA_TEMPORAL_LEAK", "error", "sva",
                f"generated file {rel} contains raw temporal syntax that the portable "
                "subset does not control.",
                "Re-run validation and rendering; inspect the contract for temporal "
                "operators.")

    # -- clarity / traceability ----------------------------------------------------------------
    for rid, req in reqs.items():
        if str(req.get("disposition")) == "formalized" and req.get("clarity") not in (None, "clear"):
            add("REQ_UNCLEAR_FORMALIZED", "error", "traceability",
                f"requirement {rid} is marked {req.get('clarity')} but disposition is "
                "formalized.",
                "Resolve the clarification question first, or mark the requirement "
                "partially formalized with its limitation.", owner="spec_agent")

    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["category"], f["code"],
                                 str(f.get("property") or "")))
    for i, finding in enumerate(findings, 1):
        finding["id"] = f"REV-{i:03d}"
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in order}
    overall = "ACTION_REQUIRED" if counts["error"] else ("WARN" if counts["warning"] else "OK")
    doc = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "generated_at": _now(),
        "design": results.get("design") or {},
        "overall": overall,
        "summary": {
            "findings": len(findings),
            "errors": counts["error"],
            "warnings": counts["warning"],
            "info": counts["info"],
            "categories": {c: sum(1 for f in findings if f["category"] == c)
                           for c in sorted({f["category"] for f in findings})},
        },
        "findings": findings,
        "inputs": {
            "contract": rel_to(project_root(manifest), Path(manifest["_paths"]["contract"])),
            "contract_sha256": sha256_file(Path(manifest["_paths"]["contract"]))
                              if Path(manifest["_paths"]["contract"]).is_file() else "",
        },
    }
    return doc


def markdown(doc: dict[str, Any]) -> str:
    design = doc.get("design") or {}
    summary = doc.get("summary") or {}
    lines = [
        f"# SVA and constraint review: {design.get('name') or design.get('top')}",
        "",
        f"- **Top:** `{design.get('top')}`",
        f"- **Overall:** **{doc.get('overall')}**",
        f"- **Findings:** {summary.get('findings', 0)} "
        f"({summary.get('errors', 0)} error, {summary.get('warnings', 0)} warning, "
        f"{summary.get('info', 0)} info)",
        f"- **Generated:** {doc.get('generated_at')}",
        "",
        "## Findings",
        "",
        "| ID | Severity | Category | Property | Code | Message | Owner |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in doc.get("findings") or []:
        lines.append(f"| `{f['id']}` | **{f['severity']}** | {f['category']} | "
                     f"`{f.get('property') or ''}` | {f['code']} | "
                     f"{_md(f['message'])} | {f['owner']} |")
    if not doc.get("findings"):
        lines.append("| - | - | - | - | - | No issues found. | - |")
    lines.append("")
    actionable = [f for f in doc.get("findings") or [] if f["severity"] in ("error", "warning")]
    if actionable:
        lines.append("## Recommended actions")
        lines.append("")
        for f in actionable:
            lines.append(f"### `{f['id']}` -- {f['code']}")
            lines.append("")
            lines.append(f"- **Owner:** {f['owner']}")
            lines.append(f"- **Message:** {f['message']}")
            lines.append(f"- **Action:** {f['recommendation']}")
            lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```sh")
    lines.append("python3 scripts/fv.py review --design <formal/design.json>")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def write_review(manifest: dict[str, Any], doc: dict[str, Any]) -> dict[str, str]:
    root = project_root(manifest)
    reports = Path(manifest["_paths"]["reports"])
    paths = {
        "review_json": reports / "sva_review.json",
        "review_md": reports / "sva_review.md",
    }
    doc["artifacts"] = {k: rel_to(root, v) for k, v in paths.items()}
    write_json(paths["review_json"], doc)
    write_text(paths["review_md"], markdown(doc))
    return doc["artifacts"]
