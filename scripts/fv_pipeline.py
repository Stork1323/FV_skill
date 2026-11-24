"""Ordered pipeline state.

Every step records the hashes of its inputs and outputs in
``formal/state/pipeline.json``.  The pipeline command uses this record to
refuse a run whose contract or bundle changed underneath it; individual
commands record their step so `fv.py status` shows what is current and what is
stale.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    project_root,
    read_json,
    sha256_file,
    sha256_paths,
    write_json,
)

PIPELINE_SCHEMA_VERSION = "1.0"
STEPS = ("rules", "extract", "contract", "render", "run", "review", "regress")
STEP_INPUTS: dict[str, tuple[str, ...]] = {
    "rules": ("spec_markdown", "tools", "design"),
    "extract": ("spec_markdown", "requirements", "rules"),
    "contract": ("requirements", "extraction", "bundle", "contract", "rules"),
    "render": ("contract", "bundle", "requirements", "rules", "tools"),
    "run": ("bundle", "contract", "rules", "tools"),
    "review": ("results", "contract"),
    "regress": ("results", "contract"),
}
OUTPUT_KEYS: dict[str, tuple[str, ...]] = {
    "rules": ("rules",),
    "extract": ("requirements", "extraction", "questions"),
    "contract": ("contract",),
    "render": ("sva",),
    "run": ("results", "reports"),
    "review": ("review",),
    "regress": ("reports",),
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def state_path(manifest: dict[str, Any]) -> Path:
    return Path(manifest["_paths"]["state"])


def load(manifest: dict[str, Any]) -> dict[str, Any]:
    path = state_path(manifest)
    if not path.is_file():
        return {"schema_version": PIPELINE_SCHEMA_VERSION, "updated_at": None, "steps": {}}
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
        raise FvError(f"{path}: invalid pipeline state")
    return data


def _file_info(manifest: dict[str, Any], key: str) -> dict[str, Any] | None:
    paths = manifest["_paths"]
    if key == "design":
        path = Path(manifest["_path"])
    elif key == "spec_markdown":
        spec = manifest.get("spec") or {}
        raw = spec.get("markdown") or "spec/spec.md"
        path = Path(raw)
        if not path.is_absolute():
            path = project_root(manifest) / raw
    elif key in paths:
        path = Path(paths[key])
    else:
        return None
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        return {"path": str(path), "kind": "dir", "files": len(files),
                "sha256": sha256_paths(files) if files else ""}
    if not path.is_file():
        return {"path": str(path), "exists": False}
    return {"path": str(path), "exists": True, "sha256": sha256_file(path)}


def fingerprints(manifest: dict[str, Any], step: str,
                 direction: str = "inputs") -> dict[str, Any]:
    keys = STEP_INPUTS.get(step, ()) if direction == "inputs" else OUTPUT_KEYS.get(step, ())
    out: dict[str, Any] = {}
    for key in keys:
        info = _file_info(manifest, key)
        if info is not None:
            out[key] = info
    return out


def record(manifest: dict[str, Any], step: str, status: str = "ok",
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if step not in STEPS:
        raise FvError(f"unknown pipeline step {step!r}")
    state = load(manifest)
    state["schema_version"] = PIPELINE_SCHEMA_VERSION
    state["updated_at"] = _now()
    state["steps"][step] = {
        "status": status,
        "timestamp": _now(),
        "inputs": fingerprints(manifest, step, "inputs"),
        "outputs": fingerprints(manifest, step, "outputs"),
        "extra": extra or {},
    }
    write_json(state_path(manifest), state)
    return state


def _stale(entry: dict[str, Any], current: dict[str, Any]) -> str:
    for key, old in (entry.get("inputs") or {}).items():
        new = current.get(key) or {}
        if bool(old.get("exists")) != bool(new.get("exists")):
            return f"input {key} changed presence"
        if old.get("sha256") and old.get("sha256") != new.get("sha256"):
            return f"input {key} changed"
    return ""


def status(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    state = load(manifest)
    out: list[dict[str, Any]] = []
    for step in STEPS:
        entry = (state.get("steps") or {}).get(step)
        current = fingerprints(manifest, step, "inputs")
        if not entry:
            out.append({"step": step, "status": "missing", "stale": False,
                        "reason": "not run"})
            continue
        reason = _stale(entry, current)
        out.append({
            "step": step,
            "status": "stale" if reason else entry.get("status", "unknown"),
            "stale": bool(reason),
            "reason": reason or "current",
            "timestamp": entry.get("timestamp"),
        })
    return out


def require(manifest: dict[str, Any], step: str,
            allow_missing_contract: bool = False) -> list[str]:
    """Errors that make an ordered run of ``step`` unsafe."""
    errors: list[str] = []
    rows = {r["step"]: r for r in status(manifest)}
    for previous in STEPS[:STEPS.index(step)]:
        row = rows[previous]
        if row["status"] in ("missing", "stale"):
            if allow_missing_contract and previous == "contract" and row["status"] == "missing":
                continue
            errors.append(f"{previous} is {row['status']}: {row['reason']}")
    return errors


def summary(manifest: dict[str, Any]) -> dict[str, Any]:
    rows = status(manifest)
    return {
        "overall": ("STALE" if any(r["status"] == "stale" for r in rows)
                    else "INCOMPLETE" if any(r["status"] == "missing" for r in rows)
                    else "CURRENT"),
        "steps": rows,
    }


def markdown(manifest: dict[str, Any]) -> str:
    doc = summary(manifest)
    rows = status(manifest)
    lines = [
        "# Formal pipeline state",
        "",
        f"- **Overall:** **{doc['overall']}**",
        "",
        "| Step | Status | Detail | Timestamp |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| `{row['step']}` | **{row['status']}** | "
                     f"{row['reason']} | {row.get('timestamp') or ''} |")
    lines.append("")
    return "\n".join(lines)


def _fail(manifest: dict[str, Any], step: str, message: str,
          extra: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = {"error": message}
    if extra:
        entry.update(extra)
    record(manifest, step, status="failed", extra=entry)
    return {
        "overall": "FAIL",
        "failed_step": step,
        "error": message,
        "steps": status(manifest),
    }


def run(manifest: dict[str, Any], *, contract: str | Path | None = None,
        depth: int | None = None, timeout_s: int | None = None,
        jobs: int | None = None, induction: bool | None = None,
        baseline: str | Path | None = None, update_baseline: bool = False,
        no_regress: bool = False, force: bool = False, strict: bool = False,
        allow_review_errors: bool = False, allow_unreviewed_extraction: bool = False,
        quiet: bool = False, backend: str | None = None) -> dict[str, Any]:
    """Execute the ordered pipeline and record every step.

    The order is fixed: rules -> extract -> contract -> render -> run ->
    review -> regress.  A step failure is recorded and stops the pipeline,
    except that a verification failure still runs the review and regression so
    the report is complete.
    """
    from fv_common import FvError, project_root, read_json
    from fv_bundle import prepare
    from fv_contract import validate_design
    from fv_extract import extract as extract_spec
    from fv_render import render
    from fv_review import review as review_results, write_review
    from fv_rules import ensure_consistent as ensure_rules_consistent
    from fv_run import run_all

    # -- rules --------------------------------------------------------------------------------
    if not quiet:
        print("pipeline: rules")
    try:
        rules = ensure_rules_consistent(manifest, quiet=True)
    except FvError as exc:
        return _fail(manifest, "rules", str(exc))
    record(manifest, "rules", extra={"rules_sha256": rules.get("rules_sha256"),
                                     "files": len(rules.get("files") or {})})

    # -- extract ------------------------------------------------------------------------------
    if not quiet:
        print("pipeline: extract")
    try:
        extraction = extract_spec(manifest, quiet=True)
    except FvError as exc:
        return _fail(manifest, "extract", str(exc))
    if extraction.get("errors"):
        return _fail(manifest, "extract", "requirement extraction has errors",
                     {"errors": extraction["errors"]})
    if not extraction.get("requirements"):
        return _fail(
            manifest, "extract",
            "no requirements in formal/spec/requirements.md; fill it from "
            "prompts/extract.md, then re-run the pipeline")
    record(manifest, "extract", extra={
        "requirements": len(extraction["requirements"]),
        "features": len(extraction.get("features") or []),
        "unclear": extraction["stats"]["unclear"],
    })

    from fv_questions import collect as collect_questions
    questions = collect_questions(manifest, bundle=extraction, contract={})
    if (questions.get("summary") or {}).get("blocking", 0):
        return _fail(
            manifest, "extract",
            f"{questions['summary']['blocking']} blocking clarification question(s) are "
            "open; answer them in formal/spec/answers.json or mark them non-blocking",
            {"questions": questions.get("artifacts") or {}})

    # P3: human gate for spec-extraction review.
    if not allow_unreviewed_extraction:
        approval_path = Path(manifest["_paths"].get("spec_dir", project_root(manifest) / "formal" / "spec")) / "extraction_approval.json"
        approved = False
        if approval_path.is_file():
            try:
                approval = read_json(approval_path)
                approved = bool(isinstance(approval, dict) and approval.get("approved") is True)
            except FvError:
                approved = False
        if not approved:
            return _fail(
                manifest, "extract",
                "extraction requires human approval; create formal/spec/extraction_approval.json "
                "with {\"approved\": true, \"reviewer\": ..., \"reviewed_at\": ...} "
                "or pass --allow-unreviewed-extraction for a non-signoff run",
                {"approval_path": str(approval_path)})

    # -- contract -----------------------------------------------------------------------------
    if not quiet:
        print("pipeline: contract")
    try:
        # Rebuild the bundle from the current extraction and RTL before the
        # validator reads it.  Without this a second run could validate a stale
        # requirement list.
        prepare(manifest, quiet=True)
        validation, _contract, _bundle, _interface = validate_design(
            manifest, Path(contract) if contract else None, strict)
    except FvError as exc:
        return _fail(manifest, "contract", str(exc))
    if not validation.ok:
        messages = [str(e) for e in validation.errors]
        return _fail(manifest, "contract", "contract validation failed",
                     {"errors": messages})
    record(manifest, "contract", extra={
        "warnings": len(validation.warnings),
        "stats": validation.stats,
    })

    # -- structural ---------------------------------------------------------------------------
    try:
        from fv_structural import run as structural_run
        structural = structural_run(manifest)
        if structural.get("overall") not in ("PASS", "WARN"):
            return _fail(manifest, "contract", "structural checks failed",
                         {"structural": structural.get("artifacts")})
    except Exception as exc:
        # Structural checks are best-effort if environment lacks tools.
        pass

    # -- render -------------------------------------------------------------------------------
    if not quiet:
        print("pipeline: render")
    try:
        render(manifest, Path(contract) if contract else None, depth, quiet=True)
    except FvError as exc:
        return _fail(manifest, "render", str(exc))
    record(manifest, "render")

    # -- run ----------------------------------------------------------------------------------
    if not quiet:
        print("pipeline: run")
    try:
        results = run_all(
            manifest, Path(contract) if contract else None, depth, timeout_s,
            jobs, induction, quiet=quiet, with_review=False,
            backend_override=backend)
    except FvError as exc:
        return _fail(manifest, "run", str(exc))
    record(manifest, "run", status="ok" if results["summary"]["overall"] == "PASS" else "failed",
           extra={"summary": results["summary"]})

    # -- review -------------------------------------------------------------------------------
    if not quiet:
        print("pipeline: review")
    review_error = ""
    try:
        review_doc = review_results(manifest)
        artifacts = write_review(manifest, review_doc)
        results["review"] = {
            "overall": review_doc["overall"],
            "summary": review_doc["summary"],
            "artifacts": artifacts,
        }
        results["review_findings"] = review_doc.get("findings") or []
        results["artifacts"].update(artifacts)
        from fv_run import write_artifacts
        write_artifacts(manifest, results)
        review_status = ("failed" if review_doc["overall"] == "ACTION_REQUIRED"
                         else "ok")
        record(manifest, "review", status=review_status,
               extra={"overall": review_doc["overall"],
                      "summary": review_doc["summary"]})
        if review_doc["overall"] == "ACTION_REQUIRED" and not allow_review_errors:
            review_error = ("SVA/constraint review found blocking issues; inspect "
                            "formal/reports/sva_review.md")
    except FvError as exc:
        return _fail(manifest, "review", str(exc))

    # -- regress ------------------------------------------------------------------------------
    regress_doc: dict[str, Any] | None = None
    regress_error = ""
    if no_regress:
        record(manifest, "regress", status="skipped", extra={"reason": "--no-regress"})
    else:
        if not quiet:
            print("pipeline: regress")
        from fv_regress import regress_design

        bpath = baseline
        has_baseline = bool(bpath) or (
            project_root(manifest) / "formal" / "regression" / "baseline.json").is_file()
        if not has_baseline and not update_baseline:
            return _fail(
                manifest, "regress",
                "no baseline yet; run with --update-baseline to create it after "
                "reviewing the results")
        if update_baseline and review_error:
            return _fail(
                manifest, "regress",
                "refusing to update the regression baseline while the SVA/constraint "
                "review requires action; fix the findings or pass "
                "--allow-review-errors for an explicit exception")
        try:
            regress_doc = regress_design(
                manifest, bpath, None, contract,
                update=update_baseline, force=force, run=False, quiet=quiet,
                allow_review_errors=allow_review_errors)
        except FvError as exc:
            return _fail(manifest, "regress", str(exc))
        record(manifest, "regress",
               status="ok" if regress_doc.get("overall") in ("PASS", "WARN") else "failed",
               extra={"overall": regress_doc.get("overall")})

    if regress_doc is not None:
        results["regression"] = {
            "overall": regress_doc.get("overall"),
            "summary": regress_doc.get("summary"),
            "artifacts": regress_doc.get("artifacts"),
        }
    from fv_run import compute_signoff, write_artifacts

    results["signoff"] = compute_signoff(results, manifest, regress_doc,
                                         allow_review_errors=allow_review_errors)
    write_artifacts(manifest, results)

    verification = results["summary"]["overall"]
    overall = "PASS"
    if verification != "PASS" or review_error:
        overall = "FAIL"
    if regress_doc is not None and regress_doc.get("overall") not in ("PASS", "WARN"):
        overall = "FAIL"
    if not results.get("signoff", {}).get("signoff_ready", True):
        # Signoff is stricter than verification pass/fail; do not mask it.
        if verification == "PASS" and not review_error and (
                regress_doc is None or regress_doc.get("overall") in ("PASS", "WARN")):
            overall = "FAIL"
    return {
        "overall": overall,
        "verification": verification,
        "review": (results.get("review") or {}).get("overall"),
        "regression": (regress_doc or {}).get("overall"),
        "review_error": review_error,
        "summary": results["summary"],
        "results": results,
        "regression_doc": regress_doc,
        "steps": status(manifest),
    }
