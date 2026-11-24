"""Independent AI behavior evaluation harness.

Evaluates agent/LLM outputs against a labeled corpus. It does not call an LLM;
it scores artifacts produced by the skill or an agent. Metrics are intentionally
narrow and evidence-based.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fv_common import normalize_text


def load_corpus(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("corpus must be an object with a 'cases' list")
    return data


def _contains(text: str, needle: str) -> bool:
    return normalize_text(needle) in normalize_text(text)


def _norm_expr(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _prop_index(actual: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for p in actual.get("properties") or []:
        if isinstance(p, dict) and p.get("id"):
            out[str(p["id"])] = p
    return out


def _req_index(actual: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for r in actual.get("requirements") or []:
        if isinstance(r, dict) and r.get("id"):
            out[str(r["id"])] = r
    return out


def evaluate_case(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {"id": case.get("id"), "checks": [], "score": 0, "total": 0}
    props = actual.get("properties") or []
    reqs = actual.get("requirements") or []
    pidx = _prop_index(actual)
    ridx = _req_index(actual)
    req_blob = " ".join(str(r.get("text") or r.get("id") or "") for r in reqs)
    prop_blob = " ".join(str(p.get("title") or p.get("expr") or p.get("trigger") or p.get("response") or "") for p in props)

    def check(name: str, ok: bool, **extra: Any) -> None:
        metrics["total"] += 1
        metrics["score"] += int(bool(ok))
        item = {"name": name, "ok": bool(ok)}
        item.update(extra)
        metrics["checks"].append(item)

    # Extraction recall: expected requirement IDs/text fragments present.
    for rid in case.get("expected_requirement_ids") or []:
        check("requirement_id", rid in ridx, requirement=rid)
    for frag in case.get("expected_requirement_fragments") or []:
        check("extraction_recall", _contains(req_blob, frag), fragment=frag)
    # Invented requirements/properties: forbidden fragments must not appear.
    for frag in case.get("forbidden_fragments") or []:
        ok = not (_contains(req_blob, frag) or _contains(prop_blob, frag))
        check("no_invented", ok, fragment=frag)
    # Requirement semantics.
    for exp in case.get("expected_requirements") or []:
        rid = str(exp.get("id") or "")
        got = ridx.get(rid)
        ok = bool(got)
        if ok and exp.get("disposition"):
            ok = ok and str(got.get("disposition")) == str(exp["disposition"])
        if ok and exp.get("owner"):
            ok = ok and str(got.get("owner")) == str(exp["owner"])
        check("requirement_semantics", ok, expected=exp, actual={k: got.get(k) for k in ("id", "disposition", "owner")} if got else None)
    # Property semantics.
    for exp in case.get("expected_properties") or []:
        pid = str(exp.get("id") or "")
        if pid:
            got = pidx.get(pid)
            ok = bool(got)
        else:
            got = next((p for p in props if str(p.get("kind")) == str(exp.get("kind")) and str(p.get("role")) == str(exp.get("role"))), None)
            ok = bool(got)
        if ok and exp.get("kind"):
            ok = ok and str(got.get("kind")) == str(exp.get("kind"))
        if ok and exp.get("role"):
            ok = ok and str(got.get("role")) == str(exp.get("role"))
        if ok and exp.get("req"):
            ok = ok and str(got.get("req")) == str(exp.get("req"))
        if ok and exp.get("obligation"):
            ok = ok and str(got.get("obligation")) == str(exp.get("obligation"))
        for field in ("expr", "trigger", "response", "when", "signal"):
            if ok and field in exp:
                ok = ok and _norm_expr(got.get(field)) == _norm_expr(exp.get(field))
        for field in ("min_delay", "max_delay", "cycles"):
            if ok and field in exp:
                ok = ok and int(got.get(field) or -1) == int(exp.get(field))
        check("property_semantics", ok, expected=exp, actual={k: got.get(k) for k in ("id", "req", "kind", "role", "expr", "trigger", "response", "when", "obligation")} if got else None)

    # Assumption leakage: no DUT-output assumption expression.
    for p in props:
        if p.get("role") == "assume":
            owner = str(p.get("owner") or "environment")
            check("assumption_ownership", owner == "environment", property=p.get("id"), owner=owner)

    # Required negative checks: these cases should fail if an artifact claims success incorrectly.
    for frag in case.get("required_error_fragments") or []:
        blob = json.dumps(actual, sort_keys=True)
        check("required_error", _contains(blob, frag), fragment=frag)

    metrics["pass"] = metrics["score"] == metrics["total"]
    return metrics


def evaluate(corpus_path: str | Path, actual_path: str | Path) -> dict[str, Any]:
    corpus = load_corpus(corpus_path)
    actual = json.loads(Path(actual_path).read_text(encoding="utf-8"))
    cases = []
    for case in corpus.get("cases") or []:
        # actual may be a map keyed by case id, or a single object for single-case corpus.
        item = actual.get(case.get("id")) if isinstance(actual, dict) and case.get("id") in actual else actual
        cases.append(evaluate_case(case, item if isinstance(item, dict) else {}))
    total = sum(c["total"] for c in cases)
    score = sum(c["score"] for c in cases)
    return {
        "schema_version": "1.1",
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "passed_cases": sum(1 for c in cases if c.get("pass")),
            "failed_cases": sum(1 for c in cases if not c.get("pass")),
            "score": score,
            "total": total,
            "pct": round(100 * score / max(1, total), 1),
            "overall": "PASS" if all(c.get("pass") for c in cases) else "FAIL",
        },
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Evaluate formal-sva AI outputs against a corpus")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--actual", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = evaluate(args.corpus, args.actual)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        s = res["summary"]
        print(f"eval: {s['overall']}  cases={s['cases']} score={s['score']}/{s['total']} ({s['pct']}%)")
        for case in res.get("cases") or []:
            if not case.get("pass"):
                print(f"  FAIL {case.get('id')}")
                for chk in case.get("checks") or []:
                    if not chk.get("ok"):
                        print(f"    - {chk.get('name')}: {chk}")
    raise SystemExit(0 if res["summary"]["overall"] == "PASS" else 1)
