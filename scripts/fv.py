#!/usr/bin/env python3
"""Command-line entry point of the formal-sva skill.

Only the extraction and contract-writing steps need an LLM.  Everything else --
parsing, validation, rendering, proving, review, reporting and regression -- is
deterministic and available here.

Ordered flow:

  fv.py all        extract -> contract -> render -> run -> review -> regress

Building blocks (each records its pipeline step):

  fv.py init       create formal/design.json for a project
  fv.py extract    spec.md -> requirements.md -> requirements.json
  fv.py prepare    build the contract-writing bundle
  fv.py validate   validate formal_contract.json and its traceability
  fv.py render     render standard SVA and backend monitors
  fv.py run        run every property, write results and reports
  fv.py review     review SVA and constraints for vacuity/coverage issues
  fv.py report     rebuild report.md/report.json/JUnit from existing results
  fv.py regress    compare against the committed baseline
  fv.py questions  rebuild the clarification report
  fv.py rules      generate the project rule files
  fv.py tools      show or validate the tool configuration
  fv.py status     show the recorded pipeline state
  fv.py doctor     check the local toolchain and backend availability

Exit codes: 0 success, 1 verification failure or regression, 2 usage/setup error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Make sibling modules importable when invoked as a script or as `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fv_common import (  # noqa: E402
    FvError,
    SKILL_VERSION,
    eprint,
    load_manifest,
    project_root,
    read_json,
    rel_to,
    which_yosys,
    yosys_version,
)


def _cmd_init(args: argparse.Namespace) -> int:
    from fv_bundle import init_design

    path = init_design(Path(args.project), args.top, args.clock, args.reset,
                       args.force, spec=args.spec, rtl=args.rtl,
                       sources=args.source or None)
    if args.no_prepare:
        return 0
    manifest = load_manifest(path)
    extraction_path = Path(manifest["_paths"]["extraction"])
    extraction = read_json(extraction_path) if extraction_path.is_file() else {}
    if not extraction.get("requirements"):
        print("prepare skipped: formal/spec/requirements.md has no requirements yet.")
        print("next: read prompts/extract.md, fill the requirement sections, then run")
        print(f"  python3 scripts/fv.py prepare --design {path}")
        return 0
    return _prepare_path(path, args.out, quiet=False)


def _prepare_path(design: str | Path, out: str | None = None, quiet: bool = False) -> int:
    from fv_bundle import prepare

    manifest = load_manifest(design)
    prepare(manifest, Path(out) if out else None, quiet=quiet)
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    return _prepare_path(args.design, args.out, args.quiet)


def _cmd_extract(args: argparse.Namespace) -> int:
    from fv_extract import extract, update_spec_hash, write_extraction_artifacts
    from fv_pipeline import record
    from fv_questions import collect

    manifest = load_manifest(args.design)
    if args.update_spec_hash:
        refresh = update_spec_hash(manifest)
        if not args.quiet:
            state = "unchanged" if not refresh["changed"] else "updated"
            print(f"spec_sha256: {state} ({refresh['spec_sha256']})")
    doc = extract(manifest, force=args.force, quiet=args.quiet)
    write_extraction_artifacts(manifest, doc)
    questions = collect(manifest, bundle=doc, contract={})
    record(manifest, "extract", status="ok" if doc["stats"]["ready"] else "failed",
           extra={"stats": doc["stats"],
                  "blocking_questions": questions["summary"]["blocking"]})
    if not doc["stats"]["ready"]:
        return 1
    return 0 if questions["summary"]["blocking"] == 0 else 1


def _cmd_validate(args: argparse.Namespace) -> int:
    from fv_contract import validate_design
    from fv_pipeline import record

    manifest = load_manifest(args.design)
    validation, _contract, _bundle, _interface = validate_design(
        manifest, Path(args.contract) if args.contract else None, args.strict)
    record(manifest, "contract", status="ok" if validation.ok else "failed",
           extra={"errors": len(validation.errors),
                  "warnings": len(validation.warnings),
                  "stats": validation.stats})
    if args.json:
        print(json.dumps({
            "ok": validation.ok,
            "errors": [str(e) for e in validation.errors],
            "warnings": [str(w) for w in validation.warnings],
            "stats": validation.stats,
        }, indent=2))
    else:
        for error in validation.errors:
            print(f"ERROR {error}")
        for warning in validation.warnings:
            print(f"WARN  {warning}")
        print(json.dumps(validation.stats, indent=2))
    return 0 if validation.ok else 1


def _cmd_render(args: argparse.Namespace) -> int:
    from fv_pipeline import record
    from fv_render import render

    manifest = load_manifest(args.design)
    render(manifest, Path(args.contract) if args.contract else None, args.depth,
           quiet=args.quiet)
    record(manifest, "render")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from fv_pipeline import record
    from fv_run import run_all

    manifest = load_manifest(args.design)
    results = run_all(
        manifest,
        Path(args.contract) if args.contract else None,
        args.depth,
        args.timeout,
        args.jobs,
        False if args.no_induction else None,
        args.property or None,
        quiet=args.quiet,
        render_if_stale=not args.no_render,
        with_review=not args.no_review,
        backend_override=args.backend,
    )
    record(manifest, "run",
           status="ok" if results["summary"]["overall"] == "PASS" else "failed",
           extra={"summary": results["summary"]})
    if results.get("review"):
        record(manifest, "review",
               status="ok" if results["review"]["overall"] != "ACTION_REQUIRED" else "failed",
               extra={"overall": results["review"]["overall"]})
    return 0 if results["summary"]["overall"] == "PASS" else 1



def _cmd_structural(args: argparse.Namespace) -> int:
    from fv_structural import run as structural_run
    manifest = load_manifest(args.design)
    res = structural_run(manifest)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"structural: {res.get('overall')}")
        for c in res.get('checks') or []:
            print(f"  [{c.get('status')}] {c.get('id')}: {c.get('message')}")
    return 0 if res.get('overall') == 'PASS' else 1
def _cmd_review(args: argparse.Namespace) -> int:
    from fv_pipeline import record
    from fv_review import review, write_review

    manifest = load_manifest(args.design)
    results = read_json(Path(args.results)) if args.results else None
    doc = review(manifest, results,
                 Path(args.contract) if args.contract else None)
    write_review(manifest, doc)
    record(manifest, "review",
           status="ok" if doc["overall"] != "ACTION_REQUIRED" else "failed",
           extra={"overall": doc["overall"], "summary": doc["summary"]})
    root = project_root(manifest)
    print(f"review: {rel_to(root, Path(manifest['_paths']['reports']) / 'sva_review.md')}")
    print(f"overall: {doc['overall']}  "
          + "  ".join(f"{k}={v}" for k, v in sorted((doc.get('summary') or {}).items())
                      if isinstance(v, int)))
    return 0 if doc["overall"] != "ACTION_REQUIRED" else 1




def _cmd_matrix(args: argparse.Namespace) -> int:
    from fv_matrix import report
    manifest = load_manifest(args.design)
    res = report(manifest)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"config_matrix: {res.get('overall')}  declared={res.get('declared_configurations')}")
    return 0 if res.get('overall') in ('PASS','NO_CONFIGS') else 1
def _cmd_replay(args: argparse.Namespace) -> int:
    from fv_replay import replay_property

    manifest = load_manifest(args.design)
    res = replay_property(manifest, args.property, timeout_s=args.timeout)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"replay: {res.get('status')}  property={res.get('id')}")
        if res.get('reason'):
            print(f"reason: {res['reason']}")
        if res.get('output'):
            print(res['output'][-1000:])
    return 0 if res.get('status') in ('REPLAYED_FAIL', 'SKIPPED') else 1
def _cmd_report(args: argparse.Namespace) -> int:
    from fv_run import check_results_binding, write_artifacts

    manifest = load_manifest(args.design)
    path = Path(args.results) if args.results else Path(manifest["_paths"]["results"])
    if not path.is_file():
        raise FvError(f"results not found: {path}\nrun `fv.py run --design ...` first")
    results = read_json(path)
    binding_errors = check_results_binding(results, manifest)
    if binding_errors and not args.allow_stale:
        raise FvError("stale or incomplete results:\n" + "\n".join(f"  {e}" for e in binding_errors))
    if binding_errors:
        eprint("warning: stale or incomplete results:\n" + "\n".join(f"  {e}" for e in binding_errors))
    write_artifacts(manifest, results)
    root = project_root(manifest)
    reports = Path(manifest["_paths"]["reports"])
    print(f"report: {rel_to(root, reports / 'report.md')}")
    print(f"junit:  {rel_to(root, reports / 'junit.xml')}")
    print(f"overall: {(results.get('summary') or {}).get('overall')}")
    return 0 if (results.get("summary") or {}).get("overall") == "PASS" else 1


def _cmd_regress(args: argparse.Namespace) -> int:
    from fv_pipeline import record
    from fv_regress import regress_design, regress_suite

    if args.suite:
        result = regress_suite(args.suite, args.update, args.force, args.run,
                               args.allow_review_errors, args.quiet, args.out)
        return 0 if result.get("overall") in ("PASS", "WARN") else 1
    if not args.design:
        raise FvError("pass --design or --suite")
    manifest = load_manifest(args.design)
    result = regress_design(
        manifest,
        args.baseline,
        args.results,
        args.contract,
        args.update,
        args.force,
        args.allow_review_errors,
        args.run,
        args.depth,
        args.timeout,
        args.jobs,
        quiet=args.quiet,
    )
    record(manifest, "regress",
           status="ok" if result.get("overall") in ("PASS", "WARN") else "failed",
           extra={"overall": result.get("overall")})
    return 0 if result.get("overall") in ("PASS", "WARN") else 1


def _cmd_questions(args: argparse.Namespace) -> int:
    from fv_questions import collect, write_questions

    manifest = load_manifest(args.design)
    doc = collect(manifest)
    write_questions(manifest, doc)
    root = project_root(manifest)
    print(f"questions: {rel_to(root, Path(manifest['_paths']['reports']) / 'questions.md')}")
    print(f"open: {(doc.get('summary') or {}).get('open', 0)}  "
          f"blocking: {(doc.get('summary') or {}).get('blocking', 0)}")
    return 0 if doc.get("summary", {}).get("blocking", 0) == 0 else 1


def _cmd_rules(args: argparse.Namespace) -> int:
    from fv_pipeline import record
    from fv_rules import check, generate

    manifest = load_manifest(args.design)
    if args.check:
        errors = check(manifest)
        for error in errors:
            print(f"ERROR {error}")
        return 1 if errors else 0
    doc = generate(manifest, force=args.force, quiet=args.quiet)
    record(manifest, "rules", extra={"rules_sha256": doc.get("rules_sha256"),
                                     "files": len(doc.get("files") or {})})
    root = project_root(manifest)
    print(f"rules: {rel_to(root, Path(manifest['_paths']['rules']))}")
    print(f"rules_sha256: {doc.get('rules_sha256')}")
    return 0


def _cmd_tools(args: argparse.Namespace) -> int:
    from fv_tools import (
        backend_config,
        backend_name,
        describe,
        ensure_tools,
        load_tools,
        tools_path,
        validate_tools,
    )

    manifest = load_manifest(args.design)
    ensure_tools(manifest, force=args.reset)
    tools = load_tools(manifest, create=False)
    errors = validate_tools(tools, tools_path(manifest))
    if args.list or args.json:
        rows = [describe(manifest, tools, name) for name in tools.get("backends") or {}]
        if args.json:
            print(json.dumps({"default": tools.get("default"),
                              "errors": errors, "backends": rows}, indent=2))
        else:
            print(f"default: {tools.get('default')}")
            for row in rows:
                state = "available" if row["available"] else "unavailable"
                print(f"  {row['name']:<12} {state:<12} {row.get('description') or ''}")
        return 0 if not errors else 1
    name = backend_name(manifest, tools, args.backend)
    cfg = backend_config(tools, name)
    print(json.dumps({"name": name, "config": cfg, "errors": errors}, indent=2))
    return 0 if not errors else 1


def _cmd_status(args: argparse.Namespace) -> int:
    from fv_pipeline import markdown, summary

    manifest = load_manifest(args.design)
    doc = summary(manifest)
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print(markdown(manifest))
    return 0 if doc["overall"] == "CURRENT" else 1


def _cmd_all(args: argparse.Namespace) -> int:
    from fv_pipeline import run as run_pipeline

    manifest = load_manifest(args.design)
    result = run_pipeline(
        manifest,
        contract=args.contract,
        depth=args.depth,
        timeout_s=args.timeout,
        jobs=args.jobs,
        induction=False if args.no_induction else None,
        baseline=args.baseline,
        update_baseline=args.update_baseline,
        no_regress=args.no_regress,
        force=args.force,
        strict=args.strict,
        allow_review_errors=args.allow_review_errors,
        allow_unreviewed_extraction=getattr(args, "allow_unreviewed_extraction", False),
        quiet=args.quiet,
        backend=args.backend,
    )
    if result["overall"] == "FAIL":
        if result.get("error"):
            eprint(f"error: {result['error']}")
        if result.get("review_error"):
            eprint(f"error: {result['review_error']}")
        return 1
    summary_doc = result.get("summary") or {}
    print(f"pipeline: PASS  verification={result.get('verification')} "
          f"review={result.get('review')} regression={result.get('regression')}")
    return 0


def _cmd_pipeline(args: argparse.Namespace) -> int:
    return _cmd_all(args)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from fv_tools import describe, load_tools, tools_path

    ok = True
    print(f"formal-sva {SKILL_VERSION}")
    print(f"python      {sys.version.split()[0]}")
    try:
        yosys = which_yosys()
        print(f"yosys       {yosys}")
        print(f"            {yosys_version()}")
    except FvError as exc:
        ok = False
        print(f"yosys       MISSING ({exc})")
    import shutil
    verilator = shutil.which("verilator")
    print(f"verilator   {verilator or 'not found (optional, lint only)'}")
    print(f"sby         {shutil.which('sby') or 'not found (default backend needs it)'}")
    if args.design:
        try:
            manifest = load_manifest(args.design)
            print(f"design      {manifest['_path']}")
            print(f"root        {manifest['_root']}")
            print(f"contract    {manifest['_paths']['contract']}")
            print(f"results     {manifest['_paths']['results']}")
            tools = load_tools(manifest)
            print(f"tools       {tools_path(manifest)}")
            for name in tools.get("backends") or {}:
                row = describe(manifest, tools, name)
                state = "available" if row["available"] else "unavailable"
                print(f"  backend   {name:<12} {state:<12} {row.get('description') or ''}")
            from fv_pipeline import summary

            state = summary(manifest)
            print(f"pipeline    {state['overall']}")
            for step in state["steps"]:
                print(f"  {step['step']:<9} {step['status']:<8} {step['reason']}")
        except FvError as exc:
            ok = False
            print(f"design      ERROR ({exc})")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fv.py",
        description="spec-traceable formal SVA generation and verification",
    )
    ap.add_argument("--version", action="version", version=f"formal-sva {SKILL_VERSION}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create formal/design.json for a project")
    p.add_argument("--project", required=True)
    p.add_argument("--top")
    p.add_argument("--clock")
    p.add_argument("--reset")
    p.add_argument("--spec", help="spec.md or the directory that contains it")
    p.add_argument("--rtl", help="RTL file, file list or directory")
    p.add_argument("--source", action="append", default=[], help="extra RTL source")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-prepare", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("extract", help="parse spec.md into requirement extraction")
    p.add_argument("--design", required=True)
    p.add_argument("--force", action="store_true",
                   help="regenerate requirements.md from structured_spec or scaffold")
    p.add_argument("--update-spec-hash", action="store_true",
                   help="after reviewing the extraction against a changed spec, "
                        "record the current spec hash")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_extract)

    p = sub.add_parser("prepare", help="build the contract-writing bundle")
    p.add_argument("--design", required=True)
    p.add_argument("--out")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_prepare)

    p = sub.add_parser("validate", help="validate the contract and its traceability")
    p.add_argument("--design", required=True)
    p.add_argument("--contract")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("render", help="render standard SVA and backend monitors")
    p.add_argument("--design", required=True)
    p.add_argument("--contract")
    p.add_argument("--depth", type=int)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_render)

    p = sub.add_parser("run", help="run all properties with the configured backend")
    p.add_argument("--design", required=True)
    p.add_argument("--contract")
    p.add_argument("--depth", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--jobs", type=int)
    p.add_argument("--property", action="append", default=[])
    p.add_argument("--backend", help="override formal/tools.json and design.json")
    p.add_argument("--no-induction", action="store_true")
    p.add_argument("--no-render", action="store_true")
    p.add_argument("--no-review", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_run)


    p = sub.add_parser("structural", help="run structural/elaboration/connectivity checks")
    p.add_argument("--design", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_structural)
    p = sub.add_parser("review", help="review SVA and constraints after a run")
    p.add_argument("--design", required=True)
    p.add_argument("--contract")
    p.add_argument("--results")
    p.set_defaults(func=_cmd_review)

    p = sub.add_parser("questions", help="rebuild the clarification report")
    p.add_argument("--design", required=True)
    p.set_defaults(func=_cmd_questions)

    p = sub.add_parser("rules", help="generate or check the project rule files")
    p.add_argument("--design", required=True)
    p.add_argument("--check", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_rules)

    p = sub.add_parser("tools", help="show or validate the tool configuration")
    p.add_argument("--design", required=True)
    p.add_argument("--backend")
    p.add_argument("--list", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reset", action="store_true", help="rewrite formal/tools.json defaults")
    p.set_defaults(func=_cmd_tools)

    p = sub.add_parser("status", help="show the recorded pipeline state")
    p.add_argument("--design", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status)



    p = sub.add_parser("matrix", help="report configuration matrix coverage")
    p.add_argument("--design", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_matrix)
    p = sub.add_parser("replay", help="replay a failing counterexample with iverilog (best effort)")
    p.add_argument("--design", required=True)
    p.add_argument("--property", required=True)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_replay)
    p = sub.add_parser("report", help="rebuild reports from existing results")
    p.add_argument("--design", required=True)
    p.add_argument("--results")
    p.add_argument("--allow-stale", action="store_true",
                   help="allow rebuilding reports from stale/incomplete results")
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("regress", help="compare with the committed baseline")
    p.add_argument("--design")
    p.add_argument("--suite")
    p.add_argument("--baseline")
    p.add_argument("--results")
    p.add_argument("--contract")
    p.add_argument("--out")
    p.add_argument("--run", action="store_true", help="run verification first")
    p.add_argument("--update", action="store_true", help="write current results as baseline")
    p.add_argument("--force", action="store_true", help="allow a failing baseline update")
    p.add_argument("--allow-review-errors", action="store_true",
                   help="record a baseline despite a review requiring action")
    p.add_argument("--allow-unreviewed-extraction", action="store_true",
                   help="allow pipeline to proceed without human extraction approval (non-signoff)")
    p.add_argument("--depth", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--jobs", type=int)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_regress)

    p = sub.add_parser("all", help="run the ordered pipeline through regression")
    p.add_argument("--design", required=True)
    p.add_argument("--contract")
    p.add_argument("--baseline")
    p.add_argument("--depth", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--jobs", type=int)
    p.add_argument("--backend", help="override formal/tools.json and design.json")
    p.add_argument("--no-induction", action="store_true")
    p.add_argument("--no-regress", action="store_true")
    p.add_argument("--update-baseline", action="store_true")
    p.add_argument("--allow-review-errors", action="store_true",
                   help="let review errors be reported without failing the pipeline")
    p.add_argument("--force", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_all)

    p = sub.add_parser("pipeline", help="alias for the ordered `all` flow")
    p.add_argument("--design", required=True)
    p.add_argument("--contract")
    p.add_argument("--baseline")
    p.add_argument("--depth", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--jobs", type=int)
    p.add_argument("--backend", help="override formal/tools.json and design.json")
    p.add_argument("--no-induction", action="store_true")
    p.add_argument("--no-regress", action="store_true")
    p.add_argument("--update-baseline", action="store_true")
    p.add_argument("--allow-review-errors", action="store_true")
    p.add_argument("--allow-unreviewed-extraction", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_pipeline)

    p = sub.add_parser("doctor", help="check the local toolchain and backends")
    p.add_argument("--design")
    p.set_defaults(func=_cmd_doctor)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except FvError as exc:
        eprint(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("interrupted")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
