"""Run all rendered properties, write results/reports/JUnit, and triage.

The run is only as trustworthy as its reporting: every property carries its
requirement, evidence, BMC/induction outcome, log path and, on failure, the SAT
counterexample rows.  ``BMC_PASS`` is deliberately reported separately from
``PROVEN``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    eprint,
    project_root,
    read_json,
    rel_to,
    sha256_file,
    write_json,
    write_text,

)
from fv_backends import run_property
from fv_yosys import load_harness

RUN_SCHEMA_VERSION = "1.1"

BAD_ERROR = {"FAIL", "ERROR", "TIMEOUT", "INCOMPLETE_OBLIGATION"}
WARN_ERROR = {"UNREACHABLE", "VACUOUS", "NOT_REACHED_WITHIN_BOUND", "ASSUMED_UNCHECKED"}

STATUS_REASONS = {
    "PROVEN": "The base case passed and the induction step proved the property for all reachable states under the recorded assumptions and configuration.",
    "BMC_PASS": "No counterexample was found within the bounded search, but induction did not prove the property; this is bounded evidence, not an unbounded proof.",
    "PARTIALLY_PROVEN": "Some required proof obligations discharged, but at least one required obligation remains bounded, missing, or inconclusive.",
    "COVERED": "The solver found a witness trace that reaches the cover condition within the configured bound.",
    "NOT_REACHED_WITHIN_BOUND": "No witness was found within the configured bound; this does not prove the scenario unreachable.",
    "PROVEN_UNREACHABLE": "The backend proved the cover condition is unreachable under the recorded assumptions and configuration.",
    "UNREACHABLE": "The backend reported the cover condition unreachable within the selected proof mode; treat as not reached/proven-unreachable only when the backend classifies it explicitly.",
    "VACUOUS": "The assumption's active guard/antecedent was not reached, so the assumption may be vacuous and provide no useful constraint.",
    "ASSUMED": "The assumption was reachable and active in at least one witness within the bound.",
    "ASSUMED_UNCHECKED": "The assumption was not checked for satisfiability/reachability in this run.",
    "INCOMPLETE_OBLIGATION": "The requirement has required proof obligations that were not fully discharged.",
    "FAIL": "The solver found a counterexample or the obligation was violated.",
    "ERROR": "The backend or harness could not produce a trustworthy result; logs and result classification must be inspected.",
    "TIMEOUT": "The solver did not finish within the configured time/bound budget.",
    "SKIPPED": "The check was intentionally not executed; record the reason and closure impact.",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _md_escape(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _phase_display(prop: dict[str, Any], phase: str) -> str:
    """Human meaning of a raw Yosys phase result (cover runs invert pass/fail)."""
    sub = prop.get(phase) or {}
    result = str(sub.get("result") or "")
    if phase == "bmc":
        if result and prop.get("mode") in ("cover", "assume"):
            if result == "fail":
                return "reachable"
            if result == "pass":
                return "unreachable"
        return result
    if result == "induction_fail":
        return "not inductive"
    if result == "proven":
        return "proven"
    if result == "skipped":
        return "skipped"
    return result


def _find_prop(harness: dict[str, Any], pid: str) -> dict[str, Any]:
    for p in harness.get("properties") or []:
        if str(p.get("id")) == pid:
            return p
    raise FvError(f"property {pid!r} is not in the rendered harness")


def _contract_hash(manifest: dict[str, Any], contract_path: Path | None) -> str:
    path = contract_path or Path(manifest["_paths"]["contract"])
    if not Path(path).is_file():
        raise FvError(f"contract not found: {path}")
    return sha256_file(Path(path))


def _bundle_hash(manifest: dict[str, Any]) -> str:
    path = Path(manifest["_paths"]["bundle"])
    if not path.is_file():
        return ""
    return sha256_file(path)


def ensure_harness(manifest: dict[str, Any], contract_path: Path | None = None,
                   depth: int | None = None, quiet: bool = False) -> dict[str, Any]:
    """Load the harness, re-rendering it if the contract/bundle changed."""
    try:
        harness = load_harness(manifest)
    except FvError:
        harness = None  # type: ignore[assignment]
    current_contract = _contract_hash(manifest, contract_path)
    current_bundle = _bundle_hash(manifest)
    stale = harness is None
    if harness is not None:
        recorded = (harness.get("contract") or {}).get("sha256")
        recorded_bundle = (harness.get("bundle") or {}).get("sha256")
        if recorded != current_contract or (current_bundle and recorded_bundle != current_bundle):
            stale = True
    if stale:
        from fv_render import render
        if not quiet:
            eprint("harness missing or stale; rendering")
        render(manifest, contract_path, depth, quiet=quiet)
        harness = load_harness(manifest)
    return harness


def status_reason(prop: dict[str, Any]) -> str:
    status = str(prop.get("status") or "UNKNOWN")
    base = STATUS_REASONS.get(status, "No status explanation is available.")
    extra: list[str] = []
    bmc = prop.get("bmc") or {}
    ind = prop.get("induction") or {}
    if bmc.get("line"):
        extra.append(f"BMC: {bmc['line']}")
    if ind and ind.get("line"):
        extra.append(f"Induction: {ind['line']}")
    if prop.get("error"):
        extra.append(f"Detail: {prop['error']}")
    if extra:
        return base + " " + " ".join(extra[:2])
    return base


def triage(prop: dict[str, Any]) -> str:
    status = prop.get("status")
    if status == "FAIL":
        return ("BMC found a counterexample. Check the evidence quote and the property "
                "against the RTL; if the RTL is correct, the requirement may be "
                "misread or the property too strong. Inspect the counterexample and "
                "the BMC log.")
    if status == "ERROR":
        return ("The backend could not elaborate or solve this property. Read the "
                "first ERROR line in the log; common causes are an unsupported "
                "construct, a missing include/define, a memory that did not map, or "
                "a parameter mismatch.")
    if status == "TIMEOUT":
        return ("The solver timed out. Increase formal.timeout_s or reduce depth, and "
                "consider splitting the property or adding legal environment "
                "assumptions.")
    if status in ("UNREACHABLE", "NOT_REACHED_WITHIN_BOUND"):
        return ("The cover condition was not reached within the bound. Confirm the "
                "scenario is legal, add environment assumptions/stimulus, or increase "
                "the depth. Do not treat this as proven unreachable unless the backend "
                "explicitly proves it.")
    if status == "VACUOUS":
        return ("The assumption's active condition is unreachable within the depth, so "
                "the assumption may be vacuous. Review its guard or add a cover for it.")
    if status == "BMC_PASS":
        return ("BMC passed within the depth but induction did not prove the property. "
                "This is not a proof; increase depth, add invariants, or inspect the "
                "induction log.")
    if status == "PROVEN":
        return "Temporal induction succeeded after a passing BMC base case."
    if status == "COVERED":
        return "A witness was found within the depth."
    if status == "ASSUMED":
        return ("The assumption is satisfiable and active in at least one reachable "
                "state within the depth.")
    return STATUS_REASONS.get(str(status), "")


def _property_aggregate(props: list[dict[str, Any]], disposition: str = "") -> str:
    if not props:
        return "NOT_FORMALIZED"
    statuses = [str(p.get("status")) for p in props]
    roles = [str(p.get("role")) for p in props]
    for bad in ("ERROR", "TIMEOUT", "FAIL"):
        if bad in statuses:
            return bad
    # An assumption-only requirement is satisfied only by assumption checks.
    if disposition == "assumption":
        if "VACUOUS" in statuses:
            return "VACUOUS"
        if statuses and all(s == "ASSUMED" for s in statuses):
            return "ASSUMED"
        return "INCOMPLETE_OBLIGATION"
    assertions = [p for p in props if p.get("role") == "assert"]
    covers = [p for p in props if p.get("role") == "cover"]
    assert_statuses = [str(p.get("status")) for p in assertions]
    cover_statuses = [str(p.get("status")) for p in covers]
    if any(p.get("status") in ("UNREACHABLE", "NOT_REACHED_WITHIN_BOUND")
           and p.get("severity") == "error" for p in covers):
        return "FAIL"
    if disposition == "formalized":
        # Every required assertion must be discharged; covers cannot replace them.
        if assertions and not all(s in ("PROVEN", "BMC_PASS") for s in assert_statuses):
            if any(s in BAD_ERROR for s in assert_statuses):
                return next(s for s in assert_statuses if s in BAD_ERROR)
            return "INCOMPLETE_OBLIGATION"
        if assertions and all(s == "PROVEN" for s in assert_statuses):
            if covers and any(s not in ("COVERED", "PROVEN_UNREACHABLE") for s in cover_statuses):
                # Proofs are complete, but reachability evidence is incomplete.
                return "PARTIALLY_PROVEN"
            return "PROVEN"
        if assertions and all(s in ("PROVEN", "BMC_PASS") for s in assert_statuses):
            return "BMC_PASS"
    # Partially formalized / mixed: report strongest available evidence but never
    # claim full proof.
    if "PROVEN" in statuses and "BMC_PASS" not in statuses and "FAIL" not in statuses:
        return "PARTIALLY_PROVEN" if disposition == "partially_formalized" else "PROVEN"
    if "BMC_PASS" in statuses:
        return "BMC_PASS"
    if "COVERED" in statuses:
        return "COVERED"
    if "ASSUMED" in statuses:
        return "ASSUMED"
    if "VACUOUS" in statuses:
        return "VACUOUS"
    if "NOT_REACHED_WITHIN_BOUND" in statuses:
        return "NOT_REACHED_WITHIN_BOUND"
    if "UNREACHABLE" in statuses:
        return "UNREACHABLE"
    return "UNKNOWN"


def _overall(properties: list[dict[str, Any]], requirements: list[dict[str, Any]] | None = None) -> str:
    for p in properties:
        status = p.get("status")
        if status in BAD_ERROR:
            return "FAIL"
        if status in WARN_ERROR and p.get("severity") == "error":
            return "FAIL"
    for req in requirements or []:
        if req.get("status") in BAD_ERROR and req.get("disposition") in (
                "formalized", "partially_formalized", "assumption"):
            return "FAIL"
    return "PASS"


def _summary(properties: list[dict[str, Any]], requirements: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for p in properties:
        counts[p.get("status", "UNKNOWN")] = counts.get(p.get("status", "UNKNOWN"), 0) + 1
    return {
        "overall": _overall(properties, requirements),
        "properties": len(properties),
        "requirements": len(requirements),
        "proven": counts.get("PROVEN", 0),
        "bmc_pass": counts.get("BMC_PASS", 0),
        "covered": counts.get("COVERED", 0),
        "not_reached_within_bound": counts.get("NOT_REACHED_WITHIN_BOUND", 0),
        "unreachable": counts.get("UNREACHABLE", 0) + counts.get("PROVEN_UNREACHABLE", 0),
        "assumed": counts.get("ASSUMED", 0),
        "vacuous": counts.get("VACUOUS", 0),
        "partial": sum(1 for r in requirements if r.get("disposition") == "partially_formalized"),
        "incomplete_obligations": counts.get("INCOMPLETE_OBLIGATION", 0),
        "fail": counts.get("FAIL", 0),
        "error": counts.get("ERROR", 0),
        "timeout": counts.get("TIMEOUT", 0),
        "statuses": counts,
    }


def _requirements(contract: dict[str, Any], bundle: dict[str, Any],
                  properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_req: dict[str, list[dict[str, Any]]] = {}
    for p in properties:
        by_req.setdefault(str(p.get("req")), []).append(p)
    out: list[dict[str, Any]] = []
    for req in bundle.get("requirements") or []:
        rid = str(req.get("id"))
        c_req = next((r for r in contract.get("requirements") or []
                      if str(r.get("id")) == rid), {})
        plist = by_req.get(rid, [])
        disposition = c_req.get("disposition") or "missing"
        status = (_property_aggregate(plist, disposition)
                  if disposition in ("formalized", "partially_formalized", "assumption")
                  else disposition.upper())
        sev_rank = {"info": 0, "warning": 1, "error": 2}
        severity = "info"
        if plist:
            severity = max((p.get("severity") or "error" for p in plist),
                           key=lambda x: sev_rank.get(x, 2))
        req_reason = STATUS_REASONS.get(status, "")
        if disposition in ("not_formalizable", "out_of_scope"):
            req_reason = str(c_req.get("reason") or req_reason)
        out.append({
            "id": rid,
            "feature": req.get("feature"),
            "text": req.get("text"),
            "category": req.get("category"),
            "sva_hint": req.get("sva_hint"),
            "disposition": disposition,
            "reason": c_req.get("reason"),
            "limitations": c_req.get("limitations"),
            "evidence": c_req.get("evidence"),
            "properties": [p.get("id") for p in plist],
            "severity": severity,
            "status": status,
            "status_reason": req_reason,
        })
    return out


def _features(bundle: dict[str, Any], requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate requirements into a feature coverage row.

    ``OK`` means every requirement in the feature is either formally checked
    (and the checks pass) or is an explicit environment assumption.  ``WARN``
    means the feature has documented but unchecked requirements (for example
    ``not_formalizable`` internal-state clauses) and nothing failed.  ``GAP``
    means a verification check failed or a requirement has no disposition.
    """
    req_map = {str(r["id"]): r for r in requirements}
    out: list[dict[str, Any]] = []
    for feat in bundle.get("features") or []:
        f_reqs = [req_map[str(rid)] for rid in (feat.get("requirements") or []) if str(rid) in req_map]
        mandatory = str(feat.get("class", "")).lower().startswith("mand")
        counts: dict[str, int] = {}
        for r in f_reqs:
            key = str(r.get("disposition") or "missing")
            counts[key] = counts.get(key, 0) + 1
        bad = [r["id"] for r in f_reqs if r.get("status") in BAD_ERROR]
        weak = [r["id"] for r in f_reqs
                if r.get("status") in WARN_ERROR and r.get("severity") == "error"]
        unresolved = [r["id"] for r in f_reqs if r.get("disposition") in ("", "missing")]
        warnings = [r["id"] for r in f_reqs
                    if r.get("status") in WARN_ERROR and r.get("severity") != "error"]
        unformalized = [r["id"] for r in f_reqs
                        if r.get("disposition") in ("not_formalizable", "out_of_scope")]
        status = "OK"
        if bad or weak or unresolved:
            status = "GAP" if mandatory else "WARN"
        elif unformalized or warnings:
            status = "WARN"
        out.append({
            "id": feat.get("id"),
            "text": feat.get("text"),
            "class": feat.get("class"),
            "requirements": [r["id"] for r in f_reqs],
            "status": status,
            "counts": {
                "total": len(f_reqs),
                "formalized": counts.get("formalized", 0),
                "partially_formalized": counts.get("partially_formalized", 0),
                "assumption": counts.get("assumption", 0),
                "not_formalizable": counts.get("not_formalizable", 0),
                "out_of_scope": counts.get("out_of_scope", 0),
                "missing": counts.get("missing", 0),
            },
            "gaps": bad + weak + unresolved,
            "unformalized": unformalized,
            "warnings": warnings,
        })
    return out


def _limitations(harness: dict[str, Any], backend: str) -> list[str]:
    design = harness.get("design") or {}
    formal = harness.get("formal") or {}
    if backend == "yosys":
        first = ("Yosys BMC is bounded; BMC_PASS is not a proof.",
                 "Temporal induction proves only properties that are inductive; "
                 "the rest remain BMC_PASS.")
    elif backend == "symbiyosys":
        first = ("SymbiYosys/smtbmc BMC is bounded; a passing BMC is not a proof.",
                 "SymbiYosys `prove` uses k-induction; properties that are not "
                 "inductive remain unproven even when the BMC base case passes.")
    else:
        first = (f"Results come from the configured backend {backend!r}; its result "
                 "classification is only as reliable as `result_pattern` in formal/tools.json.",
                 "A bounded engine pass is not a proof unless the backend explicitly "
                 "reports an unbounded proof.")
    return [
        *first,
        f"The run covers the configuration in scope.configuration, forces reset "
        f"for the first {design.get('reset_cycles')} cycle(s), and checks up to "
        f"depth {formal.get('depth')}.",
        "Cover properties report reachability within the BMC depth, not exhaustive reachability.",
        "The generated standard SVA file is not executed by this run; it is provided for commercial tools.",
        "Assumptions are environment rules. A VACUOUS assumption means its active condition was not reached.",
    ]


def _load_signoff_exceptions(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not manifest:
        return {}
    path = project_root(manifest) / "formal" / "signoff_exceptions.json"
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except FvError:
        return {}
    return data if isinstance(data, dict) else {}


def _exception_ids(exceptions: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in exceptions.get("exceptions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("approved") is False:
            continue
        for key in ("id", "requirement", "property", "scope"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                ids.add(val.strip())
        for val in item.get("ids") or []:
            if isinstance(val, str) and val.strip():
                ids.add(val.strip())
    return ids


def compute_signoff(results: dict[str, Any], manifest: dict[str, Any] | None = None,
                    regression: dict[str, Any] | None = None,
                    allow_review_errors: bool = False) -> dict[str, Any]:
    summary = results.get("summary") or {}
    reqs = results.get("requirements") or []
    props = results.get("properties") or []
    review = results.get("review") or {}
    ledger = results.get("completeness_ledger") or {}
    exceptions = _load_signoff_exceptions(manifest)
    excepted = _exception_ids(exceptions)

    blocking: list[str] = []
    warnings: list[str] = []

    if summary.get("overall") != "PASS":
        blocking.append("verification run is not PASS")

    unresolved_reqs = []
    for r in reqs:
        disp = r.get("disposition")
        status = r.get("status")
        rid = str(r.get("id") or "")
        if disp in ("formalized", "partially_formalized", "assumption"):
            if status in BAD_ERROR or status in WARN_ERROR or status in (
                    "NOT_FORMALIZED", "MISSING", "UNKNOWN", "PARTIALLY_PROVEN", "INCOMPLETE_OBLIGATION"):
                if rid in excepted or f"req:{rid}" in excepted:
                    warnings.append(f"requirement {rid} has approved exception ({status})")
                else:
                    unresolved_reqs.append(rid)
    if unresolved_reqs:
        blocking.append("mandatory/unresolved requirement obligations: " + ", ".join(sorted(unresolved_reqs)))

    bad_assumes = [str(p.get("id")) for p in props
                   if p.get("role") == "assume" and p.get("status") != "ASSUMED"]
    bad_assumes = [pid for pid in bad_assumes if pid not in excepted and f"prop:{pid}" not in excepted]
    if bad_assumes:
        blocking.append("assumptions not satisfiable/checked: " + ", ".join(sorted(bad_assumes)))

    if review.get("overall") == "ACTION_REQUIRED":
        if allow_review_errors:
            warnings.append("SVA/constraint review requires action (explicitly allowed)")
        else:
            blocking.append("SVA/constraint review requires action")
    elif review.get("overall") == "WARN":
        warnings.append("SVA/constraint review has warnings")

    regression = regression or results.get("regression") or {}
    if regression.get("overall") in ("FAIL", "ERROR"):
        blocking.append("regression comparison is not passing")
    elif regression.get("overall") == "WARN":
        warnings.append("regression comparison has warnings")

    ledger_summary = ledger.get("summary") or {}
    unmapped = int(ledger_summary.get("unmapped") or 0)
    if unmapped > 0:
        warnings.append(f"completeness ledger has {unmapped} unmapped normative statement(s)")

    # Strict signoff: mandatory unresolved obligations must block unless excepted.
    strict_blocking = list(blocking)
    strict_ready = not strict_blocking

    return {
        "schema_version": "1.1",
        "execution_complete": all(p.get("status") not in ("ERROR", "TIMEOUT") for p in props),
        "proof_complete": all(r.get("status") == "PROVEN" for r in reqs
                              if r.get("disposition") == "formalized"),
        "reachability_checked": not any(p.get("role") == "cover"
                                        and p.get("status") in ("UNREACHABLE", "NOT_REACHED_WITHIN_BOUND", "PROVEN_UNREACHABLE")
                                        and p.get("severity") == "error"
                                        for p in props),
        "environment_checked": all(p.get("status") == "ASSUMED" for p in props if p.get("role") == "assume"),
        "review_clean": review.get("overall") in (None, "", "OK", "PASS"),
        "regression_clean": regression.get("overall") in (None, "", "PASS"),
        "no_requirement_gaps": not unresolved_reqs,
        "strict_signoff_ready": strict_ready,
        "signoff_ready": strict_ready,
        "blocking_reasons": strict_blocking,
        "warnings": warnings,
        "exceptions_applied": sorted(excepted),
    }


def _signoff(results: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible wrapper; prefer compute_signoff(...)."""
    return compute_signoff(results)


def _markdown(results: dict[str, Any]) -> str:
    design = results.get("design") or {}
    summary = results.get("summary") or {}
    signoff = results.get("signoff") or _signoff(results)
    lines: list[str] = []
    lines.append(f"# Formal verification report: {design.get('name') or design.get('top')}")
    lines.append("")
    lines.append(f"- **Top:** `{design.get('top')}`")
    lines.append(f"- **Clock:** `{(design.get('clock') or {}).get('signal')}` "
                 f"({(design.get('clock') or {}).get('edge')})")
    lines.append(f"- **Reset:** `{(design.get('reset') or {}).get('signal')}` "
                 f"({(design.get('reset') or {}).get('active')}-active, "
                 f"{(design.get('reset') or {}).get('kind')})")
    params = ", ".join(f"`{k}={v}`" for k, v in (design.get("parameters") or {}).items())
    lines.append(f"- **Configuration:** {params or '(none)'}")
    lines.append(f"- **Backend:** {results.get('backend', {}).get('name')} "
                 f"{results.get('backend', {}).get('version')}")
    lines.append(f"- **Generated:** {results.get('generated_at')}")
    lines.append(f"- **Overall:** **{summary.get('overall')}**")
    lines.append("")
    lines.append("## Human-readable summary")
    lines.append("")
    lines.append(f"This run checked **{summary.get('properties', 0)}** properties across "
                 f"**{summary.get('requirements', 0)}** requirements. "
                 f"**{summary.get('proven', 0)}** proven, **{summary.get('bmc_pass', 0)}** bounded-pass only, "
                 f"**{summary.get('covered', 0)}** covered, **{summary.get('fail', 0)}** failed, "
                 f"**{summary.get('error', 0)}** errors, **{summary.get('timeout', 0)}** timeouts.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---:|")
    labels = {
        "properties": "Properties", "requirements": "Requirements",
        "proven": "Proven", "bmc_pass": "BMC pass (not proven)",
        "covered": "Covered",
        "not_reached_within_bound": "Not reached within bound",
        "unreachable": "Unreachable/proven unreachable",
        "assumed": "Assumptions satisfiable", "vacuous": "Vacuous assumptions",
        "partial": "Partially formalized requirements",
        "incomplete_obligations": "Incomplete obligations",
        "fail": "Failed", "error": "Errors", "timeout": "Timeouts",
    }
    for key, label in labels.items():
        if key in summary:
            lines.append(f"| {label} | {summary.get(key, 0)} |")
    lines.append("")
    lines.append("## What to do with this report")
    lines.append("")
    lines.append("### Spec / Architecture")
    lines.append("")
    lines.append("- Confirm each requirement's disposition, evidence, and limitations match the intended specification.")
    lines.append("- Resolve blocking clarification questions before treating a requirement as formalized.")
    lines.append("- For `not_formalizable`/`out_of_scope`, record the reason and the alternative verification plan.")
    lines.append("")
    lines.append("### RTL / Design")
    lines.append("")
    lines.append("- Treat `FAIL` as a counterexample-backed mismatch: inspect trace, requirement evidence, and RTL behavior.")
    lines.append("- Treat `BMC_PASS` as bounded only; do not claim unbounded proof.")
    lines.append("- Treat `UNREACHABLE`/`NOT_REACHED_WITHIN_BOUND` as missing reachability evidence; check assumptions, reset, and stimulus.")
    lines.append("")
    lines.append("## Signoff readiness")
    lines.append("")
    for k, v in signoff.items():
        if k in ("blocking_reasons", "warnings", "exceptions_applied"):
            continue
        if isinstance(v, bool):
            lines.append(f"- **{k.replace('_', ' ').title()}:** {'yes' if v else 'no'}")
        else:
            lines.append(f"- **{k.replace('_', ' ').title()}:** {_md_escape(v)}")
    if signoff.get("blocking_reasons"):
        lines.append("- **Blocking reasons:** " + "; ".join(signoff["blocking_reasons"]))
    if signoff.get("warnings"):
        lines.append("- **Warnings:** " + "; ".join(signoff["warnings"]))
    if signoff.get("exceptions_applied"):
        lines.append("- **Exceptions applied:** " + ", ".join(signoff["exceptions_applied"]))
    lines.append("")
    features = results.get("features") or []
    if features:
        lines.append("")
        lines.append("## Feature coverage")
        lines.append("")
        lines.append("| Feature | Class | Status | Requirements | Formalized | Partial | Assumption | Not formalizable | Out of scope |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
        for f in features:
            c = f.get("counts") or {}
            lines.append(f"| `{f.get('id')}` | {_md_escape(f.get('class'))} | "
                         f"**{f.get('status')}** | {c.get('total', 0)} | "
                         f"{c.get('formalized', 0)} | {c.get('partially_formalized', 0)} | "
                         f"{c.get('assumption', 0)} | {c.get('not_formalizable', 0)} | "
                         f"{c.get('out_of_scope', 0)} |")
    lines.append("")
    lines.append("## Requirement coverage")
    lines.append("")
    lines.append("| Requirement | Feature | Disposition | Status | Properties | Evidence |")
    lines.append("|---|---|---|---|---|---|")
    for r in results.get("requirements") or []:
        ev = (r.get("evidence") or {}).get("section", "")
        lines.append(f"| `{r['id']}` | {_md_escape(r.get('feature'))} | "
                     f"{_md_escape(str(r.get('disposition') or '').replace('_', ' '))} | "
                     f"**{_md_escape(r.get('status'))}** | "
                     f"{_md_escape(', '.join(r.get('properties') or []))} | {_md_escape(ev)} |")
    lines.append("")
    partials = [r for r in results.get("requirements") or []
                if r.get("disposition") == "partially_formalized"]
    if partials:
        lines.append("## Partial formalization limits")
        lines.append("")
        for r in partials:
            lines.append(f"- `{r['id']}`: {_md_escape(r.get('limitations') or '')}")
        lines.append("")
    cat = (results.get("artifacts") or {}).get("catalog_md")
    if cat:
        lines.append(f"- **SVA catalog:** `{cat}`")
        lines.append("")
    lines.append("## Property results")
    lines.append("")
    lines.append("| Property | Req | Title | Kind | Role | Depth | BMC | Induction | Status | Waveform |")
    lines.append("|---|---|---|---|---|---:|---|---|---|---|")
    for p in results.get("properties") or []:
        bmc = _phase_display(p, "bmc")
        ind = _phase_display(p, "induction")
        title = _md_escape(p.get("title") or "")
        wav = p.get("waveform_vcd") or p.get("waveform_svg") or ""
        wav_cell = f"`{_md_escape(wav)}`" if wav else ""
        lines.append(f"| `{p['id']}` | `{p.get('req')}` | {title} | {p.get('kind')} | {p.get('role')} | "
                     f"{p.get('depth')} | {bmc} | {ind} | **{p.get('status')}** | {wav_cell} |")
    lines.append("")
    problems = [p for p in results.get("properties") or []
                if p.get("status") in BAD_ERROR
                or (p.get("status") in WARN_ERROR and p.get("severity") == "error")]
    if problems:
        lines.append("## Failures and triage")
        lines.append("")
        for p in problems:
            lines.append(f"### `{p['id']}` -- {p.get('status')}")
            lines.append("")
            lines.append(f"- **Requirement:** `{p.get('req')}` -- {_md_escape(p.get('title'))}")
            lines.append(f"- **Kind/Role:** `{p.get('kind')}` / `{p.get('role')}`")
            check = p.get('expr') or p.get('trigger') or p.get('signal') or ''
            if check:
                lines.append(f"- **Check:** `{_md_escape(str(check))}`")
            if p.get('trigger') and p.get('expr') and p.get('kind') in ('next','response','stability'):
                lines.append(f"- **Trigger:** `{_md_escape(str(p.get('trigger')))}")
            if p.get('response'):
                lines.append(f"- **Response:** `{_md_escape(str(p.get('response')))}")
            lines.append(f"- **Triage:** {triage(p)}")
            if p.get("error"):
                lines.append(f"- **Error:** {_md_escape(p['error'])}")
            for k, label in (("trace_vcd","Trace VCD"), ("waveform_vcd","Waveform VCD"), ("waveform_gtkw","GTKWave save"), ("waveform_svg","Waveform SVG")):
                if p.get(k):
                    lines.append(f"- **{label}:** `{p[k]}`")
            if p.get("signals_of_interest"):
                sigs = ", ".join(f"`{s}`" for s in p.get("signals_of_interest")[:20])
                lines.append(f"- **Signals of interest:** {sigs}")
            ces = p.get("counterexample_summary") or {}
            if ces.get("rows") and ces.get("times"):
                lines.append("")
                lines.append("Counterexample signal summary (last steps):")
                lines.append("")
                hdr = ["Signal"] + [str(t) for t in ces["times"]]
                lines.append("| " + " | ".join(hdr) + " |")
                lines.append("|" + "|".join(["---"] * len(hdr)) + "|")
                for row in ces["rows"][:12]:
                    vals = [str(v) for v in row.get("values") or []]
                    lines.append("| `" + str(row.get("signal","")) + "` | " + " | ".join(vals) + " |")
            for item in ("bmc", "induction"):
                sub = p.get(item) or {}
                if sub.get("log"):
                    lines.append(f"- **{item.upper()} log:** `{sub['log']}`")
                    if sub.get("line"):
                        lines.append(f"- **{item.upper()} result:** `{_md_escape(sub['line'])}`")
            cx = p.get("counterexample") or []
            trace_rows = [r for r in cx if str(r).startswith(('trace_vcd:','trace_tb:','trace_yw:','trace_fst:'))]
            detail_rows = [r for r in cx if r not in trace_rows]
            if trace_rows:
                lines.append("- **Trace files:**")
                for r in trace_rows[:6]:
                    lines.append(f"  - `{_md_escape(str(r))}`")
            if detail_rows:
                lines.append("")
                lines.append("<details><summary>Counterexample details</summary>")
                lines.append("")
                lines.append("```")
                lines.extend(detail_rows[:40])
                if len(detail_rows) > 40:
                    lines.append(f"... {len(detail_rows) - 40} more rows in the results JSON")
                lines.append("```")
                lines.append("</details>")
            lines.append("")
    if results.get("assumptions"):
        lines.append("## Assumptions")
        lines.append("")
        lines.append("| Property | Req | Status | Guard |")
        lines.append("|---|---|---|---|")
        for a in results["assumptions"]:
            lines.append(f"| `{a['id']}` | `{a.get('req')}` | **{a.get('status')}** | "
                         f"{_md_escape(a.get('when') or '(always)')} |")
        lines.append("")
    review = results.get("review") or {}
    if review:
        rs = review.get("summary") or {}
        lines.append("## SVA and constraint review")
        lines.append("")
        lines.append(f"- **Overall:** **{review.get('overall')}**")
        lines.append(f"- **Findings:** {rs.get('findings', 0)} "
                     f"({rs.get('errors', 0)} error, {rs.get('warnings', 0)} warning, "
                     f"{rs.get('info', 0)} info)")
        if review.get("artifacts"):
            lines.append(f"- **Detail:** `{review['artifacts'].get('review_md')}`")
        lines.append("")
        for finding in results.get("review_findings") or []:
            if finding.get("severity") in ("error", "warning"):
                lines.append(f"- **{finding.get('severity').upper()}** `{finding.get('code')}` "
                             f"({finding.get('property') or 'contract'}): "
                             f"{_md_escape(finding.get('message'))}")
        lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for item in results.get("limitations") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```sh")
    lines.append("python3 scripts/fv.py run --design <formal/design.json>")
    lines.append("python3 scripts/fv.py report --design <formal/design.json>")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _junit(results: dict[str, Any]) -> str:
    design = results.get("design") or {}
    props = results.get("properties") or []
    suite = ET.Element("testsuite", {
        "name": f"formal-sva.{design.get('name') or design.get('top')}",
        "tests": str(len(props)),
    })
    failures = errors = skipped = 0
    for p in props:
        case = ET.SubElement(suite, "testcase", {
            "classname": str(design.get("name") or design.get("top")),
            "name": str(p.get("id")),
            "time": str(p.get("elapsed_s") or 0),
        })
        status = p.get("status")
        if status in ("FAIL",) or (status in WARN_ERROR and p.get("severity") == "error"):
            failures += 1
            fail = ET.SubElement(case, "failure", {"message": str(status)})
            fail.text = (p.get("status_reason") or p.get("error") or "") + "\n" + "\n".join((p.get("counterexample") or [])[:20])
        elif status in ("ERROR", "TIMEOUT"):
            errors += 1
            err = ET.SubElement(case, "error", {"message": str(status)})
            err.text = p.get("status_reason") or p.get("error") or ""
        elif status == "BMC_PASS":
            out = ET.SubElement(case, "system-out")
            out.text = (p.get("status_reason") or "BMC_PASS is bounded, not a proof.") + " " + (triage(p) or "")
        elif status == "COVERED":
            out = ET.SubElement(case, "system-out")
            out.text = p.get("status_reason") or ("Reachable within depth " + str(p.get("depth")) + ".")
        elif status == "ASSUMED":
            out = ET.SubElement(case, "system-out")
            out.text = p.get("status_reason") or "Assumption is satisfiable and active within depth."
        elif status in ("UNREACHABLE", "NOT_REACHED_WITHIN_BOUND", "VACUOUS") and p.get("severity") != "error":
            skipped += 1
            sk = ET.SubElement(case, "skipped", {"message": str(status)})
            sk.text = p.get("status_reason") or triage(p)
    suite.set("failures", str(failures))
    suite.set("errors", str(errors))
    suite.set("skipped", str(skipped))
    return ET.tostring(suite, encoding="unicode") + "\n"


def run_all(manifest: dict[str, Any], contract_path: Path | None = None,
            depth: int | None = None, timeout_s: int | None = None,
            jobs: int | None = None, induction: bool | None = None,
            properties: list[str] | None = None, quiet: bool = False,
            render_if_stale: bool = True, with_review: bool = True,
            backend_override: str | None = None) -> dict[str, Any]:
    started = time.time()
    # A run must use the generated, unmodified project rule set and a bundle
    # built from the current extraction/spec/RTL.
    from fv_rules import ensure_consistent as ensure_rules_consistent

    ensure_rules_consistent(manifest, quiet=True)
    if render_if_stale:
        from fv_contract import load_contract
        load_contract(manifest, contract_path)
    harness = (ensure_harness(manifest, contract_path, depth, quiet) if render_if_stale
               else load_harness(manifest))
    contract, bundle = _load_contract_bundle(manifest, contract_path, harness)
    root = project_root(manifest)
    props = list(harness.get("properties") or [])
    if properties:
        wanted = {str(x) for x in properties}
        missing = wanted - {str(p.get("id")) for p in props}
        if missing:
            raise FvError("unknown property id(s): " + ", ".join(sorted(missing)))
        props = [p for p in props if str(p.get("id")) in wanted]
    if not props:
        raise FvError("no properties selected")

    from fv_tools import (
        backend_name,
        backend_version,
        ensure_runnable,
        load_tools,
    )

    tools = load_tools(manifest)
    backend = backend_name(manifest, tools, backend_override)
    backend_cfg = ensure_runnable(manifest, tools, backend)
    backend_info = {
        "name": backend,
        "kind": backend_cfg.get("kind"),
        "version": backend_version(backend_cfg),
    }

    formal = harness.get("formal") or {}
    workers = int(jobs or formal.get("jobs") or 1)
    workers = max(1, min(workers, len(props)))
    if not quiet:
        eprint(f"running {len(props)} propert{'y' if len(props) == 1 else 'ies'} "
               f"with {workers} worker(s) on backend {backend}")

    results_by_id: dict[str, dict[str, Any]] = {}
    if workers == 1:
        for prop in props:
            results_by_id[str(prop["id"])] = run_property(
                harness, prop, backend_cfg, depth, timeout_s, induction, quiet=quiet)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_property, harness, prop, backend_cfg, depth,
                            timeout_s, induction, quiet): prop
                for prop in props
            }
            for fut in as_completed(futures):
                prop = futures[fut]
                try:
                    results_by_id[str(prop["id"])] = fut.result()
                except Exception as exc:  # keep the other properties running
                    results_by_id[str(prop["id"])] = {
                        "id": prop.get("id"),
                        "kind": prop.get("kind"),
                        "role": prop.get("role"),
                        "mode": prop.get("mode"),
                        "depth": prop.get("depth"),
                        "status": "ERROR",
                        "error": f"runner exception: {exc}",
                        "bmc": None,
                        "induction": None,
                        "counterexample": [],
                        "elapsed_s": 0,
                    }

    run_fingerprint = hashlib.sha256(
        json.dumps({
            "generated_at": _now(),
            "contract": (harness.get("contract") or {}).get("sha256"),
            "bundle": (harness.get("bundle") or {}).get("sha256"),
            "backend": backend_info,
            "properties": [p.get("id") for p in props],
        }, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    contract_props = {str(p.get("id")): p for p in contract.get("properties") or []}
    assumption_set = [p for p in contract.get("properties") or [] if p.get("role") == "assume"]
    assumption_set_sha = hashlib.sha256(
        json.dumps([{k: p.get(k) for k in sorted(p.keys())} for p in assumption_set],
                   sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    ordered: list[dict[str, Any]] = []
    for prop in props:
        run = results_by_id[str(prop["id"])]
        cprop = contract_props.get(str(prop.get("id"))) or {}
        prop_hash = hashlib.sha256(
            json.dumps({k: prop.get(k) for k in sorted(prop.keys())}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        semantic = {
            k: cprop.get(k) for k in (
                "id", "req", "feature", "kind", "role", "title", "when", "expr",
                "trigger", "response", "min_delay", "max_delay", "signal", "cycles",
                "disable_reset", "depth", "severity", "tags", "evidence",
                "obligation", "owner", "intent", "proof", "configurations",
                "delay_source", "induction_helpers", "limitations", "spec_sva_id",
                "clock", "reset", "domain",
            ) if cprop.get(k) is not None
        }
        semantic_sha = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        merged = {
            **run,
            "req": prop.get("req"),
            "feature": prop.get("feature"),
            "title": prop.get("title"),
            "severity": prop.get("severity") or "error",
            "evidence": prop.get("evidence"),
            "tags": prop.get("tags") or [],
            "obligation": cprop.get("obligation") or prop.get("obligation"),
            "owner": cprop.get("owner") or prop.get("owner"),
            "intent": cprop.get("intent"),
            "proof": cprop.get("proof"),
            "configurations": cprop.get("configurations"),
            "delay_source": cprop.get("delay_source"),
            "when": cprop.get("when"),
            "expr": cprop.get("expr"),
            "trigger": cprop.get("trigger"),
            "response": cprop.get("response"),
            "min_delay": cprop.get("min_delay"),
            "max_delay": cprop.get("max_delay"),
            "signal": cprop.get("signal"),
            "cycles": cprop.get("cycles"),
            "disable_reset": cprop.get("disable_reset"),
            "clock": prop.get("clock") or cprop.get("clock"),
            "reset": prop.get("reset") or cprop.get("reset"),
            "domain": prop.get("domain") or cprop.get("domain"),
            "run_id": run_fingerprint,
            "config": {
                "design": (contract.get("design") or {}).get("name"),
                "top": (contract.get("design") or {}).get("top"),
                "parameters": (contract.get("design") or {}).get("parameters"),
                "backend": backend_info.get("name"),
                "backend_kind": backend_info.get("kind"),
                "depth": run.get("depth"),
                "reset_cycles": prop.get("reset_cycles"),
                "clock": (prop.get("clock") or {}).get("signal") if isinstance(prop.get("clock"), dict) else prop.get("clock"),
                "reset": (prop.get("reset") or {}).get("signal") if isinstance(prop.get("reset"), dict) else prop.get("reset"),
                "domain": prop.get("domain") or cprop.get("domain"),
            },
            "semantic": semantic,
            "semantic_sha256": semantic_sha,
            "artifact_hashes": {
                "contract_sha256": (harness.get("contract") or {}).get("sha256"),
                "bundle_sha256": (harness.get("bundle") or {}).get("sha256"),
                "harness_sha256": sha256_file(Path(harness["_path"])) if harness.get("_path") else "",
                "property_sha256": prop_hash,
                "assumption_set_sha256": assumption_set_sha,
            },
        }
        # Reject conflicting/incomplete backend markers: a pass with counterexample
        # trace or fail without diagnostic info is suspect but keep diagnostic; flag.
        if merged.get("status") in ("ERROR", "TIMEOUT", "FAIL") and not (merged.get("error") or merged.get("bmc")):
            merged["status"] = "ERROR"
            merged["error"] = "incomplete backend result: missing diagnostic artifacts"
        merged["status_reason"] = status_reason(merged)
        merged["triage"] = triage(merged)
        ordered.append(merged)
    requirements = _requirements(contract, bundle, ordered)
    features = _features(bundle, requirements)
    assumptions = [
        {
            "id": p.get("id"),
            "req": p.get("req"),
            "status": p.get("status"),
            "when": next((x.get("when") for x in contract.get("properties") or []
                          if str(x.get("id")) == str(p.get("id"))), None),
            "triage": p.get("triage"),
        }
        for p in ordered if p.get("role") == "assume"
    ]
    summary = _summary(ordered, requirements)
    results = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_fingerprint,
        "generated_at": _now(),
        "design": {
            "name": (contract.get("design") or {}).get("name"),
            "top": (contract.get("design") or {}).get("top"),
            "clock": (contract.get("design") or {}).get("clock"),
            "reset": (contract.get("design") or {}).get("reset"),
            "parameters": (contract.get("design") or {}).get("parameters"),
        },
        "scope": contract.get("scope"),
        "contract": {
            "path": (harness.get("contract") or {}).get("path"),
            "sha256": (harness.get("contract") or {}).get("sha256"),
        },
        "backend": backend_info,
        "summary": summary,
        "properties": ordered,
        "requirements": requirements,
        "features": features,
        "assumptions": assumptions,
        "limitations": _limitations(harness, backend),
        "elapsed_s": round(time.time() - started, 3),
        "artifacts": {
            "harness": rel_to(root, Path(harness["_path"])),
            "sva_checker": (harness.get("files") or {}).get("sva_checker"),
            "results": rel_to(root, Path(manifest["_paths"]["results"])),
            "report_md": rel_to(root, Path(manifest["_paths"]["reports"]) / "report.md"),
            "report_json": rel_to(root, Path(manifest["_paths"]["reports"]) / "report.json"),
            "junit": rel_to(root, Path(manifest["_paths"]["reports"]) / "junit.xml"),
        },
    }
    # The review is part of every completed run: verification results alone do not
    # show whether the properties and constraints themselves are trustworthy.
    if with_review:
        from fv_review import review as review_results, write_review

        review_doc = review_results(manifest, results)
        artifacts = write_review(manifest, review_doc)
        results["review"] = {
            "overall": review_doc["overall"],
            "summary": review_doc["summary"],
            "artifacts": artifacts,
        }
        results["review_findings"] = review_doc.get("findings") or []
        results["artifacts"].update(artifacts)

    # Planning/signoff artifacts: derived from contract/bundle/results.
    try:
        from fv_plan import build as build_plan, write as write_plan
        from fv_ledger import build as build_ledger, write as write_ledger
        plan = build_plan(manifest, contract, bundle, results)
        results["verification_plan"] = {"summary": {
            "requirements": len(plan.get("requirements") or []),
            "exclusions": len(plan.get("exclusions") or []),
        }, "artifacts": write_plan(manifest, plan)}
        results["artifacts"].update(results["verification_plan"]["artifacts"])
        ledger = build_ledger(manifest, bundle)
        results["completeness_ledger"] = {"summary": ledger.get("summary"), "artifacts": write_ledger(manifest, ledger)}
        results["artifacts"].update(results["completeness_ledger"]["artifacts"])
    except Exception as exc:
        results.setdefault("warnings", []).append(f"plan/ledger generation failed: {exc}")

    provenance = {
        "generated_by": "formal-sva/fv_run.py",
        "run_id": run_fingerprint,
        "model": {"id": "unknown", "provider": "unknown", "note": "populated by agent when available"},
        "prompt_template_versions": {},
        "input_hashes": {
            "contract_sha256": (harness.get("contract") or {}).get("sha256"),
            "bundle_sha256": (harness.get("bundle") or {}).get("sha256"),
            "harness_sha256": sha256_file(Path(harness["_path"])) if harness.get("_path") else "",
            "tools_sha256": "",
        },
        "tool_versions": {
            "skill_version": __import__("fv_common").SKILL_VERSION,
            "backend": backend_info,
        },
        "validation_feedback": list(results.get("warnings") or []),
        "repair_history": [],
        "trust_boundary": {
            "spec_and_rtl_are_data": True,
            "tool_logs_are_data": True,
            "untrusted_inputs_must_not_override_policy": True,
        },
    }
    try:
        from fv_tools import tools_hash
        provenance["input_hashes"]["tools_sha256"] = tools_hash(manifest)
    except Exception:
        pass
    try:
        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        if prompts_dir.is_dir():
            provenance["prompt_template_versions"] = {
                p.name: sha256_file(p) for p in sorted(prompts_dir.glob("*.md"))
            }
    except Exception:
        pass
    results["provenance"] = provenance
    results["signoff"] = compute_signoff(results, manifest)

    # Persist artifacts.
    write_artifacts(manifest, results)
    if not quiet:
        results_path = Path(manifest["_paths"]["results"])
        reports = Path(manifest["_paths"]["reports"])
        print(f"results: {rel_to(root, results_path)}")
        print(f"report:  {rel_to(root, reports / 'report.md')}")
        print(f"junit:   {rel_to(root, reports / 'junit.xml')}")
        print(f"overall: {summary['overall']}  "
              + "  ".join(f"{k}={v}" for k, v in sorted(summary['statuses'].items())))
    return results


def check_results_binding(results: dict[str, Any], manifest: dict[str, Any] | None = None,
                          contract_path: Path | None = None) -> list[str]:
    """Return binding/staleness errors for a results document."""
    errors: list[str] = []
    if not isinstance(results, dict):
        return ["results must be an object"]
    if not results.get("run_id"):
        errors.append("results missing run_id; rerun verification")
    if not isinstance(results.get("properties"), list):
        errors.append("results missing properties list")
    contract = results.get("contract") or {}
    if not contract.get("sha256"):
        errors.append("results missing contract.sha256")
    if manifest is not None:
        cpath = contract_path or Path(manifest["_paths"]["contract"])
        if Path(cpath).is_file():
            current = sha256_file(Path(cpath))
            recorded = contract.get("sha256")
            if recorded and recorded != current:
                errors.append(
                    f"results are stale / contract does not match: contract sha256 {recorded} != current {current}; rerun verification")
        hpath = Path(manifest["_paths"]["sva"]) / "generated" / "fv_manifest.json"
        if hpath.is_file():
            try:
                harness = json.loads(hpath.read_text(encoding="utf-8"))
                hsha = (harness.get("contract") or {}).get("sha256")
                recorded = contract.get("sha256")
                if hsha and recorded and hsha != recorded:
                    errors.append("results do not match rendered harness contract; rerender/rerun")
            except Exception:
                pass
    seen: set[str] = set()
    for i, p in enumerate(results.get("properties") or []):
        if not isinstance(p, dict):
            errors.append(f"properties[{i}] is not an object")
            continue
        pid = str(p.get("id") or "")
        if not pid:
            errors.append(f"properties[{i}] missing id")
        elif pid in seen:
            errors.append(f"duplicate property id in results: {pid}")
        seen.add(pid)
        if not p.get("status"):
            errors.append(f"property {pid or i} missing status")
        if not p.get("config"):
            errors.append(f"property {pid or i} missing config binding")
        if not p.get("artifact_hashes"):
            errors.append(f"property {pid or i} missing artifact_hashes")
    return errors


def _merge_waveform_artifacts(manifest: dict[str, Any], results: dict[str, Any], wave_index: dict[str, Any], root: Path) -> None:
    by_id = {str(a.get("property")): a for a in (wave_index.get("artifacts") or [])}
    for p in results.get("properties") or []:
        a = by_id.get(str(p.get("id")))
        if not a:
            continue
        for k in ("vcd", "svg", "gtkw", "signals_json", "vcd_kind", "signals_of_interest", "counterexample_summary"):
            if a.get(k):
                target = k
                if k in ("vcd", "svg", "gtkw"):
                    target = "waveform_" + k
                p[target] = a[k]
        if a.get("vcd_source"):
            p["trace_vcd"] = a["vcd_source"]
        if not p.get("trace_vcd"):
            for row in p.get("counterexample") or []:
                m = re.search(r"trace_vcd:\s*(.+)$", str(row).strip())
                if m:
                    p["trace_vcd"] = m.group(1).strip()
                    break
    arts = results.setdefault("artifacts", {})
    arts["waveforms_dir"] = rel_to(root, Path(manifest["_paths"]["reports"]) / "waveforms")
    arts["waveforms_index"] = rel_to(root, Path(manifest["_paths"]["reports"]) / "waveforms" / "index.json")


def write_artifacts(manifest: dict[str, Any], results: dict[str, Any],
                    allow_review_errors: bool = False) -> None:
    """Write results.json, report.md, report.json, junit.xml, catalog, and waveforms."""
    root = project_root(manifest)
    results_path = Path(manifest["_paths"]["results"])
    reports = Path(manifest["_paths"]["reports"])
    reports.mkdir(parents=True, exist_ok=True)
    try:
        from fv_waveform import generate_for_results
        wave_index = generate_for_results(manifest, results)
        _merge_waveform_artifacts(manifest, results, wave_index, root)
    except Exception as exc:
        results.setdefault("warnings", []).append(f"waveform generation failed: {exc}")
    try:
        from fv_catalog import write_catalog
        results.setdefault("artifacts", {}).update(write_catalog(manifest, results))
    except Exception as exc:
        results.setdefault("warnings", []).append(f"catalog generation failed: {exc}")
    results["signoff"] = compute_signoff(results, manifest, allow_review_errors=allow_review_errors)
    write_json(results_path, results)
    write_text(reports / "report.md", _markdown(results))
    write_json(reports / "report.json", results)
    write_text(reports / "junit.xml", _junit(results))


def _load_contract_bundle(manifest: dict[str, Any], contract_path: Path | None,
                          harness: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    root = project_root(manifest)
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    cpath = contract_path
    if cpath is None:
        recorded = (harness.get("contract") or {}).get("path")
        cpath = (root / recorded) if recorded else paths["contract"]
    if not Path(cpath).is_file():
        raise FvError(f"contract not found: {cpath}")
    contract = read_json(Path(cpath))
    bundle = read_json(paths["bundle"])
    return contract, bundle


if __name__ == "__main__":
    import argparse
    from fv_common import load_manifest

    ap = argparse.ArgumentParser(description="run all rendered formal properties")
    ap.add_argument("--design", required=True)
    ap.add_argument("--contract")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--jobs", type=int)
    ap.add_argument("--no-induction", action="store_true")
    ap.add_argument("--property", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    m = load_manifest(args.design)
    res = run_all(m, Path(args.contract) if args.contract else None,
                  args.depth, args.timeout, args.jobs,
                  False if args.no_induction else None,
                  args.property or None, args.quiet)
    if args.json:
        print(json.dumps({k: res[k] for k in ("summary", "properties", "artifacts")}, indent=2))
    raise SystemExit(0 if res["summary"]["overall"] == "PASS" else 1)
