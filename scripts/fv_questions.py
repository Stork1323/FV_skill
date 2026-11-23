"""Clarification questions for spec, architecture and RTL owners.

Extraction records a clarity value per requirement.  Anything that is not
``clear`` becomes a question here, with an owner, a severity and the evidence
that makes it unclear.  Contract dispositions that admit missing information
also produce questions.  Answers are read from ``formal/spec/answers.json`` so a
resolved question disappears from the open list without editing generated
files.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    project_root,
    read_json,
    rel_to,
    sha256_file,
    write_json,
    write_text,
)

QUESTIONS_SCHEMA_VERSION = "1.0"
UNCERTAIN_WORDS = ("ambig", "unclear", "not specified", "undefined", "tbd",
                   "missing", "unknown", "conflict", "incomplete", "assume",
                   "not documented", "open issue")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _answers_path(manifest: dict[str, Any]) -> Path:
    return project_root(manifest) / "formal" / "spec" / "answers.json"


def _load_answers(manifest: dict[str, Any]) -> dict[str, Any]:
    path = _answers_path(manifest)
    if not path.is_file():
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        raise FvError(f"{path}: answers must be a JSON object")
    answers = data.get("answers") if isinstance(data.get("answers"), dict) else data
    return answers if isinstance(answers, dict) else {}


def collect(manifest: dict[str, Any], bundle: dict[str, Any] | None = None,
            contract: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    if bundle is None:
        if not paths["bundle"].is_file():
            from fv_bundle import prepare
            bundle = prepare(manifest, quiet=True)
        else:
            bundle = read_json(paths["bundle"])
    if contract is None:
        if paths["contract"].is_file():
            contract = read_json(paths["contract"])
        else:
            contract = {}
    answers = _load_answers(manifest)
    features = {str(f.get("id")): f for f in bundle.get("features") or []}
    contract_reqs = {str(r.get("id")): r for r in contract.get("requirements") or []}
    questions: list[dict[str, Any]] = []

    def add(rid: str, feature: str, clarity: str, question: str, owner: str,
            blocking: bool, evidence: str, suggested: str = "") -> None:
        index = 1 + sum(1 for q in questions if q["requirement"] == rid)
        qid = f"Q-{rid}-{index:02d}"
        answer = answers.get(qid) or {}
        status = "answered" if answer.get("status", "answered" if answer.get("answer") else "open") == "answered" else "open"
        questions.append({
            "id": qid,
            "requirement": rid,
            "feature": feature,
            "severity": "blocking" if blocking else "advisory",
            "category": clarity,
            "owner": owner or "spec_agent",
            "question": question,
            "why": f"requirement clarity is {clarity}",
            "evidence": evidence,
            "suggested_answer": suggested,
            "status": status,
            "answer": answer.get("answer") if isinstance(answer, dict) else None,
            "answered_at": answer.get("answered_at") if isinstance(answer, dict) else None,
        })

    spec_sha = ((bundle.get("sources") or {}).get("spec_markdown") or {}).get("sha256") or ""
    for req in bundle.get("requirements") or []:
        rid = str(req.get("id"))
        clarity = str(req.get("clarity") or "clear")
        if clarity == "clear":
            continue
        feature = str(req.get("feature") or "")
        klass = str((features.get(feature) or {}).get("class") or "")
        blocking = bool(req.get("blocking")) or klass.lower().startswith("mand")
        question = str(req.get("question") or "").strip()
        if not question:
            question = (f"Requirement {rid} is marked {clarity}; confirm the intended "
                        "behavior and the formalizable statement.")
        evidence = req.get("evidence")
        if isinstance(evidence, dict):
            evidence = evidence.get("quote") or evidence.get("section") or ""
        add(rid, feature, clarity, question,
            str(req.get("owner") or "spec_agent"), blocking,
            str(evidence or req.get("text") or ""),
            str(req.get("suggested_answer") or ""))
    # Invalidate answers tied to an old spec revision.
    for q in questions:
        ans = answers.get(q["id"]) or {}
        if isinstance(ans, dict) and ans.get("spec_sha256") and spec_sha and ans.get("spec_sha256") != spec_sha:
            q["status"] = "open"
            q["answer_stale"] = True
            q["why"] = q.get("why", "") + "; answer is tied to an old spec revision"

    for rid, req in contract_reqs.items():
        disp = str(req.get("disposition") or "")
        if disp not in ("partially_formalized", "not_formalizable", "out_of_scope"):
            continue
        reason = str(req.get("reason") or "")
        if not any(word in reason.lower() for word in UNCERTAIN_WORDS):
            continue
        if any(q["requirement"] == rid for q in questions):
            continue
        add(rid, str(req.get("feature") or ""), "incomplete",
            f"Contract disposition for {rid} is {disp}: {reason}",
            "spec_agent", disp == "partially_formalized", reason,
            "Clarify the missing behavior or confirm the documented limitation.")

    counts = {
        "open": sum(1 for q in questions if q["status"] == "open"),
        "answered": sum(1 for q in questions if q["status"] == "answered"),
        "blocking": sum(1 for q in questions if q["status"] == "open"
                        and q["severity"] == "blocking"),
        "by_owner": {},
    }
    for q in questions:
        if q["status"] == "open":
            counts["by_owner"][q["owner"]] = counts["by_owner"].get(q["owner"], 0) + 1
    doc = {
        "schema_version": QUESTIONS_SCHEMA_VERSION,
        "generated_at": _now(),
        "design": bundle.get("design") or {},
        "overall": "OPEN" if counts["open"] else "CLEAR",
        "summary": counts,
        "questions": questions,
        "answers_file": rel_to(project_root(manifest), _answers_path(manifest)),
        "inputs": {
            "requirements": rel_to(project_root(manifest), paths["requirements"]),
            "requirements_sha256": (sha256_file(paths["requirements"])
                                    if paths["requirements"].is_file() else ""),
            "contract_sha256": (sha256_file(paths["contract"])
                                if paths["contract"].is_file() else ""),
            "spec_sha256": ((bundle.get("sources") or {}).get("spec_markdown") or {}).get("sha256") or "",
        },
    }
    return doc


def markdown(doc: dict[str, Any]) -> str:
    design = doc.get("design") or {}
    summary = doc.get("summary") or {}
    lines = [
        f"# Formal clarification questions: {design.get('name') or design.get('top')}",
        "",
        f"- **Overall:** **{doc.get('overall')}**",
        f"- **Open:** {summary.get('open', 0)} "
        f"(blocking: {summary.get('blocking', 0)})",
        f"- **Answered:** {summary.get('answered', 0)}",
        f"- **Answers file:** `{doc.get('answers_file')}`",
        "",
    ]
    open_qs = [q for q in doc.get("questions") or [] if q["status"] == "open"]
    if open_qs:
        owners = summary.get("by_owner") or {}
        if owners:
            lines.append("Open by owner: "
                         + ", ".join(f"`{k}`={v}" for k, v in sorted(owners.items())))
            lines.append("")
        lines.append("## Open questions")
        lines.append("")
        lines.append("| ID | Severity | Owner | Requirement | Category | Question |")
        lines.append("|---|---|---|---|---|---|")
        for q in open_qs:
            lines.append(f"| `{q['id']}` | **{q['severity']}** | `{q['owner']}` | "
                         f"`{q['requirement']}` | {q['category']} | {_md(q['question'])} |")
        lines.append("")
        for q in open_qs:
            lines.append(f"### `{q['id']}` -- {q['requirement']}")
            lines.append("")
            lines.append(f"- **Owner:** {q['owner']}")
            lines.append(f"- **Why it is open:** {q['why']}")
            lines.append(f"- **Evidence:** {_md(q['evidence'])}")
            if q.get("suggested_answer"):
                lines.append(f"- **Suggested answer:** {_md(q['suggested_answer'])}")
            lines.append("")
    else:
        lines.append("No open questions.")
        lines.append("")
    answered = [q for q in doc.get("questions") or [] if q["status"] == "answered"]
    if answered:
        lines.append("## Answered questions")
        lines.append("")
        lines.append("| ID | Requirement | Answer |")
        lines.append("|---|---|---|")
        for q in answered:
            lines.append(f"| `{q['id']}` | `{q['requirement']}` | {_md(q.get('answer'))} |")
        lines.append("")
    lines.append("## How to answer")
    lines.append("")
    lines.append("Write `formal/spec/answers.json`:")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({
        "schema_version": "1.0",
        "answers": {
            "Q-REQ-002-01": {
                "status": "answered",
                "answer": "The counter is free-running; no pause input exists.",
                "owner": "spec_agent",
                "spec_sha256": "<sha256 from questions.json inputs.spec_sha256>",
                "answered_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("Answers tied to an old `spec_sha256` are treated as stale when the spec changes.")
    lines.append("")
    return "\n".join(lines)


def write_questions(manifest: dict[str, Any], doc: dict[str, Any]) -> dict[str, str]:
    root = project_root(manifest)
    reports = Path(manifest["_paths"]["reports"])
    paths = {
        "questions_json": reports / "questions.json",
        "questions_md": reports / "questions.md",
    }
    doc["artifacts"] = {k: rel_to(root, v) for k, v in paths.items()}
    write_json(paths["questions_json"], doc)
    write_text(paths["questions_md"], markdown(doc))
    return doc["artifacts"]
