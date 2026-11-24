"""Compare formal results against a committed baseline and report regressions.

The baseline is intentionally small: property/requirement/feature statuses and
the contract hash.  It is *not* a copy of the full report, so it is reviewable
in a pull request and stable across report-format changes.

A regression is a weaker status than the baseline (see ``fv_common.STATUS_ORDER``).
``PROVEN -> BMC_PASS`` is a regression.  ``COVERED -> UNREACHABLE`` is a
regression too, but it only blocks the run when the property severity is
``error``; an informational cover is reported as a warning.

A baseline is never updated from a failing verification run unless ``--force``
is passed, so a broken build cannot silently become the reference.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fv_common import (
    BAD_STATUSES,
    FvError,
    load_manifest,
    project_root,
    read_json,
    rel_to,
    sha256_file,
    status_rank,
    write_json,
    write_text,
)
from fv_run import run_all, triage, write_artifacts

BASELINE_SCHEMA_VERSION = "1.0"
REGRESSION_SCHEMA_VERSION = "1.0"

WARN_STATUSES = {"UNREACHABLE", "VACUOUS", "ASSUMED_UNCHECKED"}

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_SEMANTIC_KEYS = (
    "role", "kind", "severity", "expr", "trigger", "response", "signal",
    "when", "min_delay", "max_delay", "cycles", "disable_reset",
    "obligation", "owner", "depth", "reset_cycles",
)


def _semantic_signature(x: dict[str, Any]) -> dict[str, Any]:
    sig: dict[str, Any] = {}
    for key in _SEMANTIC_KEYS:
        if x.get(key) is not None:
            sig[key] = x.get(key)
    # Configuration and assumption set are part of verification meaning.
    for key in ("assumptions", "configuration", "configurations", "bound_source",
                "proof_method"):
        if x.get(key) is not None:
            sig[key] = x.get(key)
    return sig


def _status_table(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for x in items:
        semantic = x.get("semantic") if isinstance(x.get("semantic"), dict) else _semantic_signature(x)
        entry: dict[str, Any] = {
            "status": x.get("status"),
            "req": x.get("req"),
            "semantic": semantic,
        }
        for key in ("severity", "kind", "role", "obligation", "owner", "semantic_sha256", "run_id"):
            if x.get(key) is not None:
                entry[key] = x[key]
        if isinstance(x.get("artifact_hashes"), dict):
            entry["assumption_set_sha256"] = x["artifact_hashes"].get("assumption_set_sha256")
        out[str(x.get("id"))] = entry
    return out


def make_baseline(results: dict[str, Any]) -> dict[str, Any]:
    """Reduce a full results document to the committed baseline shape."""
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": _now(),
        "design": results.get("design") or {},
        "contract": results.get("contract") or {},
        "summary": {
            "overall": (results.get("summary") or {}).get("overall"),
            "statuses": (results.get("summary") or {}).get("statuses") or {},
        },
        "properties": _status_table(results.get("properties") or []),
        "requirements": _status_table(results.get("requirements") or []),
        "features": _status_table(results.get("features") or []),
    }


def baseline_path_for(manifest: dict[str, Any], explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit) if Path(explicit).is_absolute() else project_root(manifest) / explicit
    return project_root(manifest) / "formal" / "regression" / "baseline.json"


def results_path_for(manifest: dict[str, Any], explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit) if Path(explicit).is_absolute() else project_root(manifest) / explicit
    return Path(manifest["_paths"]["results"])


_FALSEY_EXPR = {"0", "1'b0", "1'h0", "1'd0", "false"}


def _norm_expr(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _semantic_weakening(base: dict[str, Any], current: dict[str, Any]) -> str | None:
    """Return a reason if current semantics are weaker/dangerous vs baseline."""
    bsem = base.get("semantic") if isinstance(base.get("semantic"), dict) else {}
    csem = current.get("semantic") if isinstance(current.get("semantic"), dict) else {}

    def g(d: dict[str, Any], k: str, fallback: Any = None) -> Any:
        return d.get(k, base.get(k, fallback) if d is bsem else current.get(k, fallback))

    brole, crole = str(g(bsem, "role", "")), str(g(csem, "role", ""))
    bobl, cobl = str(g(bsem, "obligation", "")), str(g(csem, "obligation", ""))
    bkind, ckind = str(g(bsem, "kind", "")), str(g(csem, "kind", ""))

    if bobl and not cobl:
        return "obligation removed"
    if bobl and cobl and bobl != cobl:
        return f"obligation changed {bobl}->{cobl}"
    if brole and crole and brole != crole:
        return f"role changed {brole}->{crole}"
    if bkind and ckind and bkind != ckind:
        return f"kind changed {bkind}->{ckind}"

    btrig = _norm_expr(g(bsem, "trigger"))
    ctrig = _norm_expr(g(csem, "trigger"))
    if btrig and ctrig in _FALSEY_EXPR:
        return "trigger became false"
    if btrig and not ctrig and str(g(csem, "kind")) in {"next", "bounded_response", "stability", "change"}:
        return "trigger removed"

    bresp = _norm_expr(g(bsem, "response") or g(bsem, "expr"))
    cresp = _norm_expr(g(csem, "response") or g(csem, "expr"))
    if bresp and cresp and bresp != cresp:
        if cresp in {"1", "1'b1", "1'h1", "1'd1", "true"} and bresp not in {"1", "1'b1", "1'h1", "1'd1", "true"}:
            return "consequent weakened to tautology"

    if crole == "assume" and brole == "assume":
        bwhen = _norm_expr(g(bsem, "when"))
        cwhen = _norm_expr(g(csem, "when"))
        if bresp != cresp or bwhen != cwhen:
            return "assumption semantics changed"

    if str(g(bsem, "kind")) == "bounded_response" and str(g(csem, "kind")) == "bounded_response":
        try:
            bmin, bmax = int(g(bsem, "min_delay", 0) or 0), int(g(bsem, "max_delay", 0) or 0)
            cmin, cmax = int(g(csem, "min_delay", 0) or 0), int(g(csem, "max_delay", 0) or 0)
            if cmin < bmin or cmax > bmax:
                return "response window widened"
        except Exception:
            pass
    return None


def _change(base: dict[str, Any] | str | None,
            current: dict[str, Any] | str | None) -> str:
    # Backward compatibility: accept raw status strings.
    if isinstance(base, str) or base is None and isinstance(current, str):
        base_d = {"status": base} if isinstance(base, str) else None
        cur_d = {"status": current} if isinstance(current, str) else None
        return _change(base_d, cur_d)
    if isinstance(current, str):
        current = {"status": current}
    if base is None:
        return "NEW"
    if current is None:
        return "REMOVED"
    bstatus = base.get("status")
    cstatus = current.get("status")
    bsem = base.get("semantic")
    csem = current.get("semantic")
    legacy = "semantic" not in base
    if not isinstance(bsem, dict):
        bsem = {}
    if not isinstance(csem, dict):
        csem = {}
    semantic_changed = (bsem != csem) if not legacy else False
    bsha = base.get("semantic_sha256")
    csha = current.get("semantic_sha256")
    if bsha and csha and bsha != csha:
        semantic_changed = True
    bash = base.get("assumption_set_sha256")
    cash = current.get("assumption_set_sha256")
    if bash and cash and bash != cash:
        semantic_changed = True

    weakening = None if legacy else _semantic_weakening(base, current)
    if weakening:
        return "REGRESSION"

    rb, rc = status_rank(bstatus), status_rank(cstatus)
    if rc > rb:
        return "REGRESSION"
    if rc < rb:
        return "CHANGED" if semantic_changed else "IMPROVEMENT"
    if semantic_changed:
        return "CHANGED"
    return "UNCHANGED"


def _is_blocking(change: str, current: str | None, severity: str) -> bool:
    if current is None:
        # A property/requirement that disappeared from the run is a real loss.
        return severity == "error"
    if current in BAD_STATUSES:
        return True
    if current in WARN_STATUSES and severity == "error":
        return True
    if change == "CHANGED" and severity == "error":
        return True
    return change == "REGRESSION" and severity == "error"


def _compare_table(label: str, current_items: list[dict[str, Any]],
                   baseline_items: dict[str, dict[str, Any]],
                   severity_default: str = "error") -> list[dict[str, Any]]:
    current = {str(x.get("id")): x for x in current_items}
    order = list(baseline_items)
    order += [k for k in current if k not in baseline_items]
    rows: list[dict[str, Any]] = []
    for key in order:
        b = baseline_items.get(key) or {}
        c = current.get(key) or {}
        bstatus = b.get("status")
        cstatus = c.get("status")
        c_entry = dict(c)
        c_entry["semantic"] = c.get("semantic") or _semantic_signature(c)
        change = _change(b, c_entry)
        severity = (c.get("severity") or b.get("severity") or severity_default)
        row = {
            "id": key,
            "scope": label,
            "title": c.get("title") or b.get("title"),
            "req": c.get("req") or b.get("req"),
            "baseline": bstatus,
            "current": cstatus,
            "change": change,
            "severity": severity,
            "blocking": _is_blocking(change, cstatus, severity),
        }
        if b.get("semantic") != c_entry.get("semantic"):
            row["semantic_changed"] = True
        for extra in ("kind", "role"):
            if c.get(extra) or b.get(extra):
                row[extra] = c.get(extra) or b.get(extra)
        if label == "property" and c:
            row["evidence"] = c.get("evidence")
            row["triage"] = c.get("triage") or triage(c)
        rows.append(row)
    return rows


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {
        "total": len(rows),
        "unchanged": 0,
        "regressions": 0,
        "blocking_regressions": 0,
        "improvements": 0,
        "changed": 0,
        "new": 0,
        "removed": 0,
    }
    for row in rows:
        key = {
            "UNCHANGED": "unchanged",
            "REGRESSION": "regressions",
            "IMPROVEMENT": "improvements",
            "CHANGED": "changed",
            "NEW": "new",
            "REMOVED": "removed",
        }[row["change"]]
        out[key] += 1
        if row["blocking"]:
            out["blocking_regressions"] += 1
    return out


def compare(results: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    props = _compare_table("property", results.get("properties") or [],
                           baseline.get("properties") or {})
    reqs = _compare_table("requirement", results.get("requirements") or [],
                          baseline.get("requirements") or {})
    feats = _compare_table("feature", results.get("features") or [],
                           baseline.get("features") or {}, severity_default="warning")
    current_summary = results.get("summary") or {}
    verification_failed = (
        current_summary.get("overall") != "PASS"
        or bool(set((current_summary.get("statuses") or {})) & BAD_STATUSES)
    )
    contract_now = (results.get("contract") or {}).get("sha256")
    contract_then = (baseline.get("contract") or {}).get("sha256")
    contract_changed = bool(contract_now and contract_then and contract_now != contract_then)
    property_counts = _counts(props)
    blocking = property_counts["blocking_regressions"] > 0
    overall = "FAIL" if verification_failed or blocking else "PASS"
    if not verification_failed and not blocking and (
            property_counts["regressions"] or _counts(reqs)["regressions"]
            or _counts(feats)["regressions"]):
        overall = "WARN"
    return {
        "schema_version": REGRESSION_SCHEMA_VERSION,
        "generated_at": _now(),
        "design": results.get("design") or baseline.get("design") or {},
        "contract": results.get("contract") or baseline.get("contract") or {},
        "contract_changed": contract_changed,
        "verification_failed": verification_failed,
        "overall": overall,
        "summary": {
            "properties": property_counts,
            "requirements": _counts(reqs),
            "features": _counts(feats),
            "verification": current_summary,
        },
        "properties": props,
        "requirements": reqs,
        "features": feats,
        "baseline": {
            "generated_at": baseline.get("generated_at"),
            "overall": (baseline.get("summary") or {}).get("overall"),
            "contract_sha256": contract_then,
        },
    }


def _md_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _change_cell(row: dict[str, Any]) -> str:
    change = row["change"]
    if change == "REGRESSION":
        return "**REGRESSION**"
    if row["blocking"]:
        return "**" + change + "**"
    return change


def regression_markdown(report: dict[str, Any]) -> str:
    design = report.get("design") or {}
    summary = report.get("summary") or {}
    lines = [
        f"# Formal regression report: {design.get('name') or design.get('top')}",
        "",
        f"- **Top:** `{design.get('top')}`",
        f"- **Overall:** **{report.get('overall')}**",
        f"- **Verification run:** {(summary.get('verification') or {}).get('overall')}",
        f"- **Baseline:** {report.get('baseline', {}).get('generated_at') or '(new)'}",
        f"- **Contract changed since baseline:** "
        f"{'yes' if report.get('contract_changed') else 'no'}",
        "",
        "## Summary",
        "",
        "| Scope | Total | Regressions | Blocking | Improvements | New | Removed |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for scope in ("properties", "requirements", "features"):
        c = summary.get(scope) or {}
        lines.append(f"| {scope.title()} | {c.get('total', 0)} | {c.get('regressions', 0)} | "
                     f"{c.get('blocking_regressions', 0)} | {c.get('improvements', 0)} | "
                     f"{c.get('new', 0)} | {c.get('removed', 0)} |")
    lines.append("")
    changed = [r for r in report.get("properties") or [] if r["change"] != "UNCHANGED"]
    if changed:
        lines.append("## Property changes")
        lines.append("")
        lines.append("| Property | Req | Baseline | Current | Change | Severity | Blocking |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in changed:
            lines.append(f"| `{r['id']}` | `{r.get('req') or ''}` | "
                         f"{_md_escape(r.get('baseline'))} | {_md_escape(r.get('current'))} | "
                         f"{_change_cell(r)} | {_md_escape(r.get('severity'))} | "
                         f"{'yes' if r.get('blocking') else 'no'} |")
        lines.append("")
        for r in changed:
            if r["change"] == "REGRESSION" and r.get("triage"):
                lines.append(f"### `{r['id']}` -- regression")
                lines.append("")
                lines.append(f"- **Requirement:** `{r.get('req')}`")
                lines.append(f"- **Baseline:** {r.get('baseline')}")
                lines.append(f"- **Current:** {r.get('current')}")
                lines.append(f"- **Triage:** {r.get('triage')}")
                lines.append("")
    if report.get("contract_changed"):
        lines.append("## Contract changed")
        lines.append("")
        lines.append("The contract hash differs from the baseline. Confirm that property "
                     "changes are intentional, then update the baseline.")
        lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```sh")
    lines.append("python3 scripts/fv.py regress --run --design <formal/design.json>")
    lines.append("python3 scripts/fv.py regress --update --design <formal/design.json>")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def regression_junit(report: dict[str, Any]) -> str:
    design = report.get("design") or {}
    suite = ET.Element("testsuite", {
        "name": f"formal-regression.{design.get('name') or design.get('top')}",
        "tests": str(len(report.get("properties") or [])),
    })
    failures = errors = skipped = 0
    for row in report.get("properties") or []:
        case = ET.SubElement(suite, "testcase", {
            "classname": "formal-regression",
            "name": str(row.get("id")),
        })
        current = row.get("current")
        if current in ("ERROR", "TIMEOUT"):
            errors += 1
            node = ET.SubElement(case, "error", {"message": str(current)})
            node.text = row.get("triage") or ""
        elif row.get("blocking"):
            failures += 1
            node = ET.SubElement(case, "failure", {"message": row.get("change") or ""})
            node.text = (f"baseline={row.get('baseline')} current={current}\n"
                         + (row.get("triage") or ""))
        elif row["change"] == "REGRESSION":
            skipped += 1
            node = ET.SubElement(case, "skipped", {"message": "non-blocking regression"})
            node.text = f"baseline={row.get('baseline')} current={current}"
        elif row["change"] in ("IMPROVEMENT", "NEW", "REMOVED"):
            out = ET.SubElement(case, "system-out")
            out.text = f"{row['change']}: baseline={row.get('baseline')} current={current}"
    suite.set("failures", str(failures))
    suite.set("errors", str(errors))
    suite.set("skipped", str(skipped))
    return ET.tostring(suite, encoding="unicode") + "\n"


def write_regression_artifacts(manifest: dict[str, Any], report: dict[str, Any]) -> dict[str, str]:
    root = project_root(manifest)
    reports = Path(manifest["_paths"]["reports"])
    paths = {
        "regression_json": reports / "regression.json",
        "regression_md": reports / "regression.md",
        "regression_junit": reports / "regression-junit.xml",
    }
    report["artifacts"] = {k: rel_to(root, v) for k, v in paths.items()}
    write_json(paths["regression_json"], report)
    write_text(paths["regression_md"], regression_markdown(report))
    write_text(paths["regression_junit"], regression_junit(report))
    return report["artifacts"]


def _load_results(manifest: dict[str, Any], results_path: str | Path | None) -> dict[str, Any]:
    path = results_path_for(manifest, results_path)
    if not path.is_file():
        raise FvError(f"results not found: {path}\nrun `fv.py run --design ...` first")
    data = read_json(path)
    if not isinstance(data, dict) or "properties" not in data:
        raise FvError(f"{path}: not a formal results document")
    return data


def _contract_info(manifest: dict[str, Any], contract_path: str | Path | None,
                   results: dict[str, Any]) -> dict[str, Any]:
    if results.get("contract"):
        return results["contract"]
    path = Path(contract_path) if contract_path else Path(manifest["_paths"]["contract"])
    if not path.is_absolute():
        path = project_root(manifest) / path
    return {"path": rel_to(project_root(manifest), path),
            "sha256": sha256_file(path) if path.is_file() else ""}


def regress_design(manifest: dict[str, Any],
                   baseline_path: str | Path | None = None,
                   results_path: str | Path | None = None,
                   contract_path: str | Path | None = None,
                   update: bool = False, force: bool = False,
                   allow_review_errors: bool = False, run: bool = False,
                   depth: int | None = None,
                   timeout_s: int | None = None, jobs: int | None = None,
                   quiet: bool = False) -> dict[str, Any]:
    if run:
        run_all(manifest, Path(contract_path) if contract_path else None,
                depth, timeout_s, jobs, quiet=quiet)
    results = _load_results(manifest, results_path)
    from fv_run import check_results_binding
    binding_errors = check_results_binding(results, manifest,
                                           Path(contract_path) if contract_path else None)
    if binding_errors:
        raise FvError("stale or incomplete results:\n" + "\n".join(f"  {e}" for e in binding_errors))
    if not run and contract_path:
        # Without a run there is nothing to verify, so a different contract
        # would silently relabel results that came from another file.
        explicit = _contract_info(manifest, contract_path, {})
        recorded = results.get("contract") or {}
        if recorded.get("sha256") and explicit.get("sha256") != recorded.get("sha256"):
            raise FvError(
                "--contract does not match the contract recorded in the results; "
                "pass --run to verify with it")
    results_contract = _contract_info(manifest, contract_path, results)
    results = {**results, "contract": results_contract}

    bpath = baseline_path_for(manifest, baseline_path)
    baseline_exists = bpath.is_file()
    baseline = read_json(bpath) if baseline_exists else None
    if baseline is not None and not isinstance(baseline, dict):
        raise FvError(f"{bpath}: baseline must be a JSON object")

    current_summary = results.get("summary") or {}
    failed = (current_summary.get("overall") != "PASS"
              or bool(set(current_summary.get("statuses") or {}) & BAD_STATUSES))

    report: dict[str, Any] | None = None
    if baseline_exists:
        report = compare(results, baseline)  # type: ignore[arg-type]
        write_regression_artifacts(manifest, report)

    if update:
        if failed and not force:
            raise FvError(
                "refusing to update the baseline from a failing verification run; "
                "fix the failure or pass --force")
        review_blocked = (
            (results.get("review") or {}).get("overall") == "ACTION_REQUIRED"
            and not allow_review_errors
        )
        from fv_run import compute_signoff
        signoff = compute_signoff(results, manifest, allow_review_errors=allow_review_errors)
        signoff_blocked = (not signoff.get("strict_signoff_ready", True)
                           and not force)
        if review_blocked:
            if report is None and baseline_exists:
                report = compare(results, baseline)  # type: ignore[arg-type]
                write_regression_artifacts(manifest, report)
            doc = report or {}
            doc.update({
                "overall": "FAIL",
                "baseline_updated": False,
                "update_refused": (
                    "SVA/constraint review requires action; pass "
                    "--allow-review-errors for an explicit exception"),
            })
            if not quiet:
                print(f"baseline: not updated ({doc['update_refused']})")
            return doc
        if signoff_blocked:
            reasons = "; ".join(signoff.get("blocking_reasons") or ["signoff not ready"])
            if report is None and baseline_exists:
                report = compare(results, baseline)  # type: ignore[arg-type]
                write_regression_artifacts(manifest, report)
            doc = report or {}
            doc.update({
                "overall": "FAIL",
                "baseline_updated": False,
                "update_refused": f"strict signoff not ready: {reasons}",
                "signoff": signoff,
            })
            if not quiet:
                print(f"baseline: not updated ({doc['update_refused']})")
            return doc
        write_json(bpath, make_baseline(results))
        if not quiet:
            print(f"baseline: {rel_to(project_root(manifest), bpath)}")
        if report is None:
            return {
                "schema_version": REGRESSION_SCHEMA_VERSION,
                "generated_at": _now(),
                "design": results.get("design") or {},
                "contract": results_contract,
                "contract_changed": False,
                "verification_failed": failed,
                "overall": "FAIL" if failed else "PASS",
                "summary": {
                    "properties": _counts([]),
                    "requirements": _counts([]),
                    "features": _counts([]),
                    "verification": current_summary,
                },
                "properties": [],
                "requirements": [],
                "features": [],
                "baseline": {"generated_at": _now(), "overall": "CREATED",
                             "contract_sha256": results_contract.get("sha256")},
                "baseline_created": True,
            }
        report["baseline"]["updated"] = True
        write_regression_artifacts(manifest, report)
        return report

    if report is None:
        raise FvError(
            f"baseline not found: {bpath}\n"
            "create it after a passing run with `fv.py regress --update --design ...`")
    if not quiet:
        print(f"regression: {report['overall']}  "
              + "  ".join(f"{k}={v}" for k, v in
                          (report["summary"]["properties"] or {}).items()))
    return report


# -- suite mode ---------------------------------------------------------------------------------

def _suite_entries(suite: dict[str, Any], suite_path: Path) -> list[dict[str, Any]]:
    raw = suite.get("designs") or []
    if not isinstance(raw, list) or not raw:
        raise FvError(f"{suite_path}: suite has no designs")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            item = {"design": item}
        if not isinstance(item, dict) or not item.get("design"):
            raise FvError(f"{suite_path}: designs[{i}] must be a path or an object with 'design'")
        design = Path(item["design"])
        if not design.is_absolute():
            design = suite_path.parent / design
        entry = {"design": str(design.resolve()), "name": item.get("name") or design.parent.name}
        if item.get("baseline"):
            b = Path(item["baseline"])
            entry["baseline"] = str(b if b.is_absolute() else (suite_path.parent / b).resolve())
        out.append(entry)
    return out


def regress_suite(suite_path: str | Path, update: bool = False, force: bool = False,
                  run: bool = False, allow_review_errors: bool = False,
                  quiet: bool = False, out_dir: str | Path | None = None) -> dict[str, Any]:
    suite_path = Path(suite_path).expanduser().resolve()
    suite = read_json(suite_path)
    entries = _suite_entries(suite, suite_path)
    started = time.time()
    designs: list[dict[str, Any]] = []
    for entry in entries:
        name = entry["name"]
        try:
            manifest = load_manifest(entry["design"])
            rep = regress_design(manifest, entry.get("baseline"), update=update,
                                 force=force, run=run,
                                 allow_review_errors=allow_review_errors, quiet=quiet)
            designs.append({
                "name": name,
                "design": rep.get("design") or {},
                "manifest": entry["design"],
                "overall": rep.get("overall"),
                "verification_failed": rep.get("verification_failed"),
                "contract_changed": rep.get("contract_changed"),
                "summary": rep.get("summary"),
                "artifacts": {
                    "regression_md": (rep.get("artifacts") or {}).get("regression_md"),
                    "regression_json": (rep.get("artifacts") or {}).get("regression_json"),
                },
            })
        except FvError as exc:
            designs.append({
                "name": name,
                "manifest": entry["design"],
                "overall": "ERROR",
                "error": str(exc),
            })
    failed = [d for d in designs if d.get("overall") not in ("PASS", "WARN")]
    warnings = [d for d in designs if d.get("overall") == "WARN"]
    overall = "FAIL" if failed else ("WARN" if warnings else "PASS")
    result = {
        "schema_version": REGRESSION_SCHEMA_VERSION,
        "generated_at": _now(),
        "suite": str(suite_path),
        "overall": overall,
        "designs": designs,
        "summary": {
            "designs": len(designs),
            "passed": len([d for d in designs if d.get("overall") == "PASS"]),
            "warnings": len(warnings),
            "failed": len(failed),
        },
        "elapsed_s": round(time.time() - started, 3),
    }
    target = Path(out_dir).expanduser().resolve() if out_dir else suite_path.parent
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "regression-suite.json", result)
    write_text(target / "regression-suite.md", suite_markdown(result))
    result["artifacts"] = {
        "regression_suite_json": str(target / "regression-suite.json"),
        "regression_suite_md": str(target / "regression-suite.md"),
    }
    if not quiet:
        print(f"suite: {overall}  " + "  ".join(f"{k}={v}" for k, v in result["summary"].items()))
        for d in designs:
            print(f"  {d['name']:<24} {d.get('overall')}"
                  + (f"  ({d.get('error')})" if d.get("error") else ""))
    return result


def suite_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Formal regression suite",
        "",
        f"- **Suite:** `{result.get('suite')}`",
        f"- **Overall:** **{result.get('overall')}**",
        f"- **Generated:** {result.get('generated_at')}",
        "",
        "## Designs",
        "",
        "| Design | Overall | Verification | Contract changed | Blocking |",
        "|---|---|---|---|---|",
    ]
    for d in result.get("designs") or []:
        props = ((d.get("summary") or {}).get("properties") or {})
        lines.append(f"| {_md_escape(d.get('name'))} | **{d.get('overall')}** | "
                     f"{'failed' if d.get('verification_failed') else 'ok'} | "
                     f"{'yes' if d.get('contract_changed') else 'no'} | "
                     f"{props.get('blocking_regressions', 0)} |")
    lines.append("")
    for d in result.get("designs") or []:
        if d.get("error"):
            lines.append(f"## {d.get('name')} -- ERROR")
            lines.append("")
            lines.append(f"- **Manifest:** `{d.get('manifest')}`")
            lines.append(f"- **Error:** {d.get('error')}")
            lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```sh")
    lines.append("python3 scripts/fv.py regress --suite <suite.json> --run")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="formal regression against a committed baseline")
    ap.add_argument("--design")
    ap.add_argument("--suite")
    ap.add_argument("--baseline")
    ap.add_argument("--results")
    ap.add_argument("--contract")
    ap.add_argument("--out")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--allow-review-errors", action="store_true",
                    help="record a baseline despite a review requiring action")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--jobs", type=int)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.suite:
        res = regress_suite(args.suite, args.update, args.force, args.run,
                            args.allow_review_errors, args.quiet, args.out)
    elif args.design:
        m = load_manifest(args.design)
        res = regress_design(m, args.baseline, args.results, args.contract,
                             args.update, args.force, args.allow_review_errors,
                             args.run, args.depth, args.timeout, args.jobs, args.quiet)
    else:
        ap.error("pass --design or --suite")
    raise SystemExit(0 if res.get("overall") in ("PASS", "WARN") else 1)
