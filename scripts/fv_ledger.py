"""Normative completeness ledger.

Maps normative specification statements to extracted requirements or justified
exclusions.  This is a deterministic heuristic guardrail: it catches obvious
omissions, but it does not replace human review of the spec.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fv_common import normalize_text, project_root, read_json, rel_to, resolve_path, write_json, write_text

NORMATIVE_RE = re.compile(r"\b(shall|must|required|only|never|always|exactly|within|no more than|at least)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build(manifest: dict[str, Any], bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    bundle = bundle or read_json(paths["bundle"])
    root = project_root(manifest)
    spec_path = resolve_path(root, (bundle.get("spec") or {}).get("markdown") or (manifest.get("spec") or {}).get("markdown") or "spec/spec.md")
    spec_text = spec_path.read_text(encoding="utf-8") if spec_path.is_file() else ""
    lines = spec_text.splitlines()
    reqs = bundle.get("requirements") or []
    req_texts = [normalize_text(str(r.get("text") or "") + " " + str((r.get("evidence") or {}).get("quote") or r.get("evidence") or "")) for r in reqs]

    entries = []
    clause_idx = 0
    for i, line in enumerate(lines, 1):
        raw = line.strip()
        if not raw or raw.startswith("#") or raw.startswith("|---"):
            continue
        # split into independent normative clauses
        parts = [p.strip(" -\t") for p in re.split(r"[;](?=\s+[A-Z(])|(?<=\.)\s+(?=[A-Z(])", raw) if p.strip()]
        if not parts:
            parts = [raw]
        for part in parts:
            if not NORMATIVE_RE.search(part):
                continue
            clause_idx += 1
            norm = normalize_text(part)
            matched = []
            for r, rt in zip(reqs, req_texts):
                ev = r.get("evidence")
                quote = normalize_text((ev or {}).get("quote") if isinstance(ev, dict) else str(ev or ""))
                if quote and (quote in norm or norm in quote):
                    matched.append(str(r.get("id")))
                    continue
                rnorm = normalize_text(str(r.get("text") or ""))
                if rnorm and (rnorm in norm or norm in rnorm):
                    matched.append(str(r.get("id")))
            matched = sorted(set(matched))
            entries.append({
                "id": f"CLS-{clause_idx:03d}",
                "line": i,
                "text": part,
                "status": "mapped" if matched else "unmapped",
                "requirements": matched,
            })

    unmapped = [e for e in entries if e["status"] == "unmapped"]
    doc = {
        "schema_version": "1.0",
        "generated_at": _now(),
        "spec": rel_to(root, spec_path) if spec_path.exists() else str(spec_path),
        "summary": {
            "normative_statements": len(entries),
            "mapped": len(entries) - len(unmapped),
            "unmapped": len(unmapped),
            "coverage_pct": round(100 * (len(entries) - len(unmapped)) / max(1, len(entries)), 1),
        },
        "entries": entries,
    }
    return doc


def markdown(doc: dict[str, Any]) -> str:
    s = doc.get("summary") or {}
    lines = [
        "# Normative completeness ledger",
        "",
        f"- **Spec:** `{doc.get('spec')}`",
        f"- **Normative statements:** {s.get('normative_statements', 0)}",
        f"- **Mapped:** {s.get('mapped', 0)}",
        f"- **Unmapped:** {s.get('unmapped', 0)}",
        f"- **Coverage:** {s.get('coverage_pct', 0)}%",
        "",
        "## Unmapped statements",
        "",
    ]
    unmapped = [e for e in doc.get("entries") or [] if e.get("status") == "unmapped"]
    if not unmapped:
        lines.append("- (none detected by heuristic)")
    else:
        for e in unmapped:
            lines.append(f"- L{e['line']}: {e['text']}")
    lines.append("")
    return "\n".join(lines)


def write(manifest: dict[str, Any], doc: dict[str, Any]) -> dict[str, str]:
    root = project_root(manifest)
    reports = Path(manifest["_paths"]["reports"])
    paths = {"ledger_json": reports / "completeness_ledger.json", "ledger_md": reports / "completeness_ledger.md"}
    doc["artifacts"] = {k: rel_to(root, v) for k, v in paths.items()}
    write_json(paths["ledger_json"], doc)
    write_text(paths["ledger_md"], markdown(doc))
    return doc["artifacts"]
