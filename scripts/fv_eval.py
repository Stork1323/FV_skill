"""Independent AI behavior evaluation harness.

This evaluates LLM/agent outputs against a labeled corpus.  It does not call an
LLM; it scores artifacts produced by the skill or an agent.  Metrics are
intentionally narrow and evidence-based.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fv_common import normalize_text


def load_corpus(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("corpus must be an object with a 'cases' list")
    return data


def _contains(text: str, needle: str) -> bool:
    return normalize_text(needle) in normalize_text(text)


def evaluate_case(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {"id": case.get("id"), "checks": [], "score": 0, "total": 0}
    # Extraction recall: expected requirement IDs/text fragments present.
    reqs = actual.get("requirements") or []
    req_blob = " ".join(str(r.get("text") or r.get("id") or "") for r in reqs)
    for frag in case.get("expected_requirement_fragments") or []:
        metrics["total"] += 1
        ok = _contains(req_blob, frag)
        metrics["checks"].append({"name": "extraction_recall", "fragment": frag, "ok": ok})
        metrics["score"] += int(ok)
    # Invented requirements/properties: forbidden fragments must not appear.
    prop_blob = " ".join(str(p.get("title") or p.get("expr") or "") for p in (actual.get("properties") or []))
    for frag in case.get("forbidden_fragments") or []:
        metrics["total"] += 1
        ok = not (_contains(req_blob, frag) or _contains(prop_blob, frag))
        metrics["checks"].append({"name": "no_invented", "fragment": frag, "ok": ok})
        metrics["score"] += int(ok)
    # Temporal translation: expected kind/role.
    for exp in case.get("expected_properties") or []:
        metrics["total"] += 1
        ok = any(str(p.get("kind")) == exp.get("kind") and str(p.get("role")) == exp.get("role")
                 for p in (actual.get("properties") or []))
        metrics["checks"].append({"name": "temporal_translation", "expected": exp, "ok": ok})
        metrics["score"] += int(ok)
    # Assumption leakage: no DUT-output assumption expression.
    for p in actual.get("properties") or []:
        if p.get("role") == "assume":
            metrics["total"] += 1
            ok = str(p.get("owner") or "environment") == "environment"
            metrics["checks"].append({"name": "assumption_ownership", "property": p.get("id"), "ok": ok})
            metrics["score"] += int(ok)
    metrics["pass"] = metrics["score"] == metrics["total"]
    return metrics


def evaluate(corpus_path: str | Path, actual_path: str | Path) -> dict[str, Any]:
    corpus = load_corpus(corpus_path)
    actual = json.loads(Path(actual_path).read_text())
    cases = []
    for case in corpus.get("cases") or []:
        # actual may be a map keyed by case id, or a single object for single-case corpus.
        item = actual.get(case.get("id")) if isinstance(actual, dict) and case.get("id") in actual else actual
        cases.append(evaluate_case(case, item if isinstance(item, dict) else {}))
    total = sum(c["total"] for c in cases)
    score = sum(c["score"] for c in cases)
    return {
        "schema_version": "1.0",
        "cases": cases,
        "summary": {"cases": len(cases), "score": score, "total": total, "pct": round(100 * score / max(1, total), 1)},
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Evaluate formal-sva AI outputs against a corpus")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--actual", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = evaluate(args.corpus, args.actual)
    print(json.dumps(res, indent=2))
