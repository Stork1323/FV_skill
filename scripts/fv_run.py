"""Run all rendered properties, write results/reports/JUnit, and triage.

The run is only as trustworthy as its reporting: every property carries its
requirement, evidence, BMC/induction outcome, log path and, on failure, the SAT
counterexample rows.  ``BMC_PASS`` is deliberately reported separately from
``PROVEN``.
"""

from __future__ import annotations

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

RUN_SCHEMA_VERSION = "1.0"

BAD_ERROR = {"FAIL", "ERROR", "TIMEOUT"}
WARN_ERROR = {"UNREACHABLE", "VACUOUS"}


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


def triage(prop: dict[str, Any]) -> str:
    status = prop.get("status")
    mode = prop.get("mode")
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
    if status == "UNREACHABLE":
        return ("The cover condition was not reachable within the depth. Confirm the "
                "scenario is legal, add environment assumptions/stimulus, or increase "
                "the depth.")
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
    return ""


def _property_aggregate(props: list[dict[str, Any]]) -> str:
    if not props:
        return "NOT_FORMALIZED"
    statuses = [p.get("status") for p in props]
    for bad in ("ERROR", "TIMEOUT", "FAIL"):
        if bad in statuses:
            return bad
    if any(p.get("status") == "UNREACHABLE" and p.get("severity") == "error" for p in props):
        return "FAIL"
    if "PROVEN" in statuses:
        return "PROVEN"
    if "COVERED" in statuses:
        return "COVERED"
    if "BMC_PASS" in statuses:
        return "BMC_PASS"
    if "ASSUMED" in statuses:
        return "ASSUMED"
    if "VACUOUS" in statuses:
        return "VACUOUS"
    if "UNREACHABLE" in statuses:
        return "UNREACHABLE"
    return "UNKNOWN"


def _overall(properties: list[dict[str, Any]]) -> str:
    for p in properties:
        status = p.get("status")
        if status in BAD_ERROR:
            return "FAIL"
        if status in WARN_ERROR and p.get("severity") == "error":
            return "FAIL"
    return "PASS"


def _summary(properties: list[dict[str, Any]], requirements: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for p in properties:
        counts[p.get("status", "UNKNOWN")] = counts.get(p.get("status", "UNKNOWN"), 0) + 1
    return {
        "overall": _overall(properties),
        "properties": len(properties),
        "requirements": len(requirements),
        "proven": counts.get("PROVEN", 0),
        "bmc_pass": counts.get("BMC_PASS", 0),
        "covered": counts.get("COVERED", 0),
        "unreachable": counts.get("UNREACHABLE", 0),
        "assumed": counts.get("ASSUMED", 0),
        "vacuous": counts.get("VACUOUS", 0),
        "partial": sum(1 for r in requirements if r.get("disposition") == "partially_formalized"),
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
        status = (_property_aggregate(plist)
                  if disposition in ("formalized", "partially_formalized", "assumption")
                  else disposition.upper())
        sev_rank = {"info": 0, "warning": 1, "error": 2}
        severity = "info"
        if plist:
            severity = max((p.get("severity") or "error" for p in plist),
                           key=lambda x: sev_rank.get(x, 2))
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


def _markdown(results: dict[str, Any]) -> str:
    design = results.get("design") or {}
    summary = results.get("summary") or {}
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
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---:|")
    labels = {
        "properties": "Properties", "requirements": "Requirements",
        "proven": "Proven", "bmc_pass": "BMC pass (not proven)",
        "covered": "Covered", "unreachable": "Unreachable covers",
        "assumed": "Assumptions satisfiable", "vacuous": "Vacuous assumptions",
        "partial": "Partially formalized requirements",
        "fail": "Failed", "error": "Errors", "timeout": "Timeouts",
    }
    for key, label in labels.items():
        lines.append(f"| {label} | {summary.get(key, 0)} |")
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
    lines.append("## Property results")
    lines.append("")
    lines.append("| Property | Req | Kind | Role | Depth | BMC | Induction | Status | Time (s) |")
    lines.append("|---|---|---|---|---:|---|---|---|---:|")
    for p in results.get("properties") or []:
        bmc = _phase_display(p, "bmc")
        ind = _phase_display(p, "induction")
        lines.append(f"| `{p['id']}` | `{p.get('req')}` | {p.get('kind')} | {p.get('role')} | "
                     f"{p.get('depth')} | {bmc} | {ind} | **{p.get('status')}** | "
                     f"{p.get('elapsed_s', '')} |")
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
            lines.append(f"- **Triage:** {triage(p)}")
            if p.get("error"):
                lines.append(f"- **Error:** {_md_escape(p['error'])}")
            for item in ("bmc", "induction"):
                sub = p.get(item) or {}
                if sub.get("log"):
                    lines.append(f"- **{item.upper()} log:** `{sub['log']}`")
                    if sub.get("line"):
                        lines.append(f"- **{item.upper()} result:** `{_md_escape(sub['line'])}`")
            cx = p.get("counterexample") or []
            if cx:
                lines.append("")
                lines.append("Counterexample (Yosys model rows):")
                lines.append("")
                lines.append("```")
                lines.extend(cx[:40])
                if len(cx) > 40:
                    lines.append(f"... {len(cx) - 40} more rows in the results JSON")
                lines.append("```")
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
            fail.text = (p.get("error") or "") + "\n" + "\n".join((p.get("counterexample") or [])[:20])
        elif status in ("ERROR", "TIMEOUT"):
            errors += 1
            err = ET.SubElement(case, "error", {"message": str(status)})
            err.text = p.get("error") or ""
        elif status == "BMC_PASS":
            # Not a failure, but make the limitation visible in CI.
            out = ET.SubElement(case, "system-out")
            out.text = "BMC_PASS is bounded, not a proof. " + (triage(p) or "")
        elif status == "COVERED":
            out = ET.SubElement(case, "system-out")
            out.text = ("Reachable within depth " + str(p.get("depth")) + ". "
                        "Reachability is bounded, not exhaustive.")
        elif status == "ASSUMED":
            out = ET.SubElement(case, "system-out")
            out.text = "Assumption is satisfiable and active within depth."
        elif status in ("UNREACHABLE", "VACUOUS") and p.get("severity") != "error":
            skipped += 1
            sk = ET.SubElement(case, "skipped", {"message": str(status)})
            sk.text = triage(p)
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

    ordered: list[dict[str, Any]] = []
    for prop in props:
        run = results_by_id[str(prop["id"])]
        merged = {
            **run,
            "req": prop.get("req"),
            "feature": prop.get("feature"),
            "title": prop.get("title"),
            "severity": prop.get("severity") or "error",
            "evidence": prop.get("evidence"),
            "tags": prop.get("tags") or [],
            "triage": triage({**run, **prop}),
        }
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


def write_artifacts(manifest: dict[str, Any], results: dict[str, Any]) -> None:
    """Write results.json, report.md, report.json and junit.xml deterministically."""
    results_path = Path(manifest["_paths"]["results"])
    reports = Path(manifest["_paths"]["reports"])
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
