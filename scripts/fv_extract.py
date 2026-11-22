"""Specification extraction: spec.md -> requirements.md -> bundle.

The extraction Markdown is the reviewable middle layer between the
specification and the formal contract.  It lists features and requirements with
source lines, a verbatim evidence quote, a clarity decision and a proposed
property shape.  A requirement that is not clear carries the question, owner
and blocking flag that the clarification report needs.

For Q3TUI projects with ``schemas/structured_spec.json`` the file is generated
deterministically from that artifact.  For a project that only has ``spec.md``
the command writes a stable scaffold and the extraction prompt guides the agent
to fill it; the parser then refuses TODO markers and unknown evidence.
"""

from __future__ import annotations

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
    resolve_path,
    sha256_file,
    words,
    write_json,
    write_text,
)
from fv_design import extract_interface

EXTRACTION_SCHEMA_VERSION = "1.0"
CLARITY = {"clear", "ambiguous", "incomplete", "conflicting", "missing_info", "untestable"}
OWNERS = {"spec_agent", "spec_engineer", "architecture_engineer",
          "rtl_agent", "verification_engineer"}
BOOLEAN = {"yes", "no", "true", "false", "1", "0"}
PROPOSED = {"always", "next", "response", "stability", "reset_value", "cover",
            "assume", "static", "unknown", ""}
HINTS = {
    "always": "property",
    "next": "property",
    "response": "property",
    "stability": "property",
    "reset_value": "property",
    "cover": "cover",
    "assume": "assume",
    "static": "static",
    "unknown": "",
    "": "",
}

HEADING_RE = re.compile(r"^###\s+`?([A-Za-z][A-Za-z0-9_]*[-_][A-Za-z0-9_.-]+)`?\s*(?:--|—|-)?\s*(.*)$")
BULLET_RE = re.compile(r"^\s*[-*]\s+\*\*([^*]+?)\s*:?\*\*\s*:?\s*(.*)$")
FEATURE_ROW_RE = re.compile(r"^\|\s*`?([^|`]+?)`?\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|")
SOURCE_RE = re.compile(r"(?:L)?(\d+)(?:\s*[-–]\s*(?:L)?(\d+))?", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1]
    return text.strip()


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in ("yes", "true", "1", "blocking")


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if m:
            meta[m.group(1).lower()] = _strip_quotes(m.group(2))
    raise FvError("extraction Markdown: front matter is not closed with ---")


def _feature_section(text: str) -> tuple[list[dict[str, Any]], str]:
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^##\s+Features\b", line.strip(), re.I):
            start = i + 1
            continue
        if start is not None and re.match(r"^##\s+", line.strip()):
            end = i
            break
    if start is None:
        return [], text
    features: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines[start:end]:
        if not line.strip().startswith("|"):
            continue
        m = FEATURE_ROW_RE.match(line.strip())
        if not m:
            continue
        fid = _strip_quotes(m.group(1)).strip()
        if not fid or fid.lower() in ("id", "feature") or set(fid) <= {"-", ":"}:
            continue
        klass = _strip_quotes(m.group(2)).strip()
        statement = _strip_quotes(m.group(3)).strip()
        source = _strip_quotes(m.group(4)).strip()
        if not klass or not statement:
            continue
        if fid in seen:
            raise FvError(f"extraction Markdown: duplicate feature {fid}")
        seen.add(fid)
        features.append({"id": fid, "class": klass, "text": statement,
                         "source": source, "requirements": []})
    return features, text


def _requirement_sections(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last_key: str | None = None
    for line in lines:
        m = HEADING_RE.match(line.strip())
        if m:
            if current:
                items.append(current)
            current = {"id": m.group(1).strip(), "title": m.group(2).strip(), "fields": {}}
            last_key = None
            continue
        if re.match(r"^#{1,3}\s+", line.strip()):
            if current:
                items.append(current)
                current = None
            last_key = None
            continue
        if current is None:
            continue
        b = BULLET_RE.match(line)
        if b:
            key = re.sub(r"\s+", " ", b.group(1).strip()).lower()
            current["fields"][key] = b.group(2).strip()
            last_key = key
        elif last_key and line.strip():
            current["fields"][last_key] += " " + line.strip()
    if current:
        items.append(current)
    return items


def _field(fields: dict[str, str], *names: str) -> str:
    for name in names:
        for key, value in fields.items():
            if key == name:
                return value.strip()
    return ""


def _source_lines(source: str) -> tuple[int | None, int | None]:
    m = SOURCE_RE.search(source or "")
    if not m:
        return None, None
    start = int(m.group(1))
    end = int(m.group(2) or m.group(1))
    if end < start:
        start, end = end, start
    return start, end


def _quote_check(spec_text: str, quote: str, source: str) -> bool:
    needle = normalize_text(quote)
    if not needle:
        return False
    start, end = _source_lines(source)
    if start and end:
        lines = spec_text.splitlines()
        if start <= len(lines):
            selected = "\n".join(lines[start - 1:min(end, len(lines))])
            if needle in normalize_text(selected):
                return True
    return needle in normalize_text(spec_text)


def _find_quote(spec_text: str, requirement: str) -> tuple[str, int | None, int | None, bool]:
    """Return (quote, start line, end line, exact) for a structured requirement."""
    req_norm = normalize_text(requirement)
    lines = spec_text.splitlines()
    if not req_norm:
        return requirement, None, None, False
    for i, line in enumerate(lines, 1):
        if req_norm and req_norm in normalize_text(line):
            return line.strip(), i, i, True
    # The requirement text may be a paragraph spread over lines.
    for i in range(len(lines)):
        for j in range(i + 1, min(i + 8, len(lines)) + 1):
            chunk = "\n".join(lines[i:j])
            if req_norm and req_norm in normalize_text(chunk):
                return " ".join(lines[i:j]).strip(), i + 1, j, True
    # Fall back to the line with the best word overlap; mark it as inexact.
    req_words = {w.lower() for w in words(requirement) if len(w) > 3}
    best: tuple[int, int, str] | None = None
    for i, line in enumerate(lines, 1):
        line_words = {w.lower() for w in words(line) if len(w) > 3}
        if not line_words:
            continue
        score = len(req_words & line_words)
        if score and (best is None or score > best[0]):
            best = (score, i, line.strip())
    if best:
        return best[2], best[1], best[1], False
    return requirement, None, None, False


def _features_from_structured(structured: dict[str, Any], config: dict[str, Any],
                              markdown: str) -> list[dict[str, Any]]:
    from fv_bundle import _features, _requirements

    reqs = _requirements(structured)
    feats = _features(config, reqs, markdown)
    for feat in feats:
        if not feat.get("text"):
            feat["text"] = str(feat.get("id"))
        if not feat.get("class"):
            feat["class"] = "Unclassified"
        feat["source"] = ""
    return feats


def _feature_class(features: list[dict[str, Any]], fid: str) -> str:
    for feature in features:
        if str(feature.get("id")) == fid:
            return str(feature.get("class") or "")
    return ""


def _structured_requirements(structured: dict[str, Any], config: dict[str, Any],
                             features: list[dict[str, Any]],
                             spec_text: str) -> list[dict[str, Any]]:
    from fv_bundle import _requirements

    out: list[dict[str, Any]] = []
    for req in _requirements(structured):
        text = str(req.get("text") or "").strip()
        if not text:
            continue
        quote, start, end, exact = _find_quote(spec_text, text)
        source = f"L{start}" if start and start == end else (
            f"L{start}-L{end}" if start and end else "")
        ambiguity = req.get("ambiguity_score")
        try:
            ambiguity_value = float(ambiguity) if ambiguity is not None else 0.0
        except (TypeError, ValueError):
            ambiguity_value = 0.0
        clarity = "clear"
        question = ""
        owner = "spec_agent"
        blocking = False
        if not exact:
            clarity = "incomplete"
            question = ("The structured requirement is not quoted verbatim in spec.md; "
                        "confirm the source passage or rewrite the requirement to match "
                        "the specification.")
            blocking = str(_feature_class(features, str(req.get("feature") or ""))).lower().startswith("mand")
        elif ambiguity_value >= 0.5:
            clarity = "ambiguous"
            question = (f"The structured spec marks this requirement with ambiguity score "
                        f"{ambiguity_value}; confirm the intended behavior and the "
                        "formalizable boundary.")
            blocking = str(_feature_class(features, str(req.get("feature") or ""))).lower().startswith("mand")
        hint = str(req.get("sva_hint") or "")
        proposed = {"static": "static", "assume": "assume", "cover": "cover",
                    "property": "always"}.get(hint, "unknown")
        out.append({
            "id": str(req["id"]),
            "title": text[:80],
            "feature": str(req.get("feature") or ""),
            "category": str(req.get("category") or ""),
            "text": text,
            "source": source,
            "evidence": quote,
            "clarity": clarity,
            "question": question,
            "owner": owner,
            "blocking": blocking,
            "proposed": proposed,
            "notes": "",
        })
    return out


def _markdown(manifest: dict[str, Any], features: list[dict[str, Any]],
              requirements: list[dict[str, Any]], spec_rel: str,
              spec_hash: str) -> str:
    design = manifest.get("name") or manifest.get("top") or "design"
    lines = [
        "---",
        f"schema_version: {EXTRACTION_SCHEMA_VERSION}",
        f"design: {design}",
        f"top: {manifest.get('top')}",
        f"spec: {spec_rel}",
        f"spec_sha256: {spec_hash}",
        "generated_by: formal-sva/fv_extract.py",
        "---",
        "",
        f"# Formal requirement extraction: {design}",
        "",
        "This file is the reviewable bridge between the specification and the",
        "formal contract. Each requirement has a source, a verbatim evidence",
        "quote, a clarity decision and a proposed property shape.",
        "",
        "## Features",
        "",
        "| ID | Class | Statement | Source |",
        "|---|---|---|---|",
    ]
    for feat in features:
        statement = str(feat.get("text") or "").replace("|", "\\|")
        lines.append(f"| `{feat.get('id')}` | {feat.get('class')} | "
                     f"{statement} | {feat.get('source') or ''} |")
    lines.extend([
        "",
        "## Requirements",
        "",
    ])
    if not requirements:
        lines.extend([
            "<!-- No requirements were extracted yet. Add one section per requirement:",
            "",
            "### REQ-001 -- Short title",
            "",
            "- **Feature:** F01",
            "- **Category:** behavior",
            "- **Statement:** The design shall ...",
            "- **Source:** L10-L12",
            "- **Evidence:** \"verbatim sentence from spec.md\"",
            "- **Clarity:** clear",
            "- **Proposed property:** always",
            "- **Owner:** spec_agent",
            "- **Blocking:** no",
            "-->",
            "",
        ])
    for req in requirements:
        lines.extend([
            f"### {req['id']} -- {req.get('title') or 'Requirement'}",
            "",
            f"- **Feature:** {req.get('feature') or ''}",
            f"- **Category:** {req.get('category') or 'behavior'}",
            f"- **Statement:** {req.get('text') or ''}",
            f"- **Source:** {req.get('source') or ''}",
            f"- **Evidence:** \"{req.get('evidence') or ''}\"",
            f"- **Clarity:** {req.get('clarity') or 'clear'}",
            f"- **Proposed property:** {req.get('proposed') or 'unknown'}",
            f"- **Owner:** {req.get('owner') or 'spec_agent'}",
            f"- **Blocking:** {'yes' if req.get('blocking') else 'no'}",
        ])
        if req.get("question"):
            lines.append(f"- **Question:** {req['question']}")
        if req.get("notes"):
            lines.append(f"- **Notes:** {req['notes']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _spec_only_scaffold(markdown: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    from fv_bundle import _features_from_markdown

    found = _features_from_markdown(markdown)
    features = [{"id": k, "class": v.get("class") or "Unclassified",
                 "text": v.get("text") or k, "source": ""}
                for k, v in found.items()]
    candidates: list[str] = []
    for i, line in enumerate(markdown.splitlines(), 1):
        stripped = line.strip()
        body = re.sub(r"^[-*]\s+", "", stripped)
        if len(body) < 20:
            continue
        if re.search(r"\b(shall|must|should|will)\b", body, re.I) or re.search(r"\bREQ[-_]\w+", body):
            candidates.append(f"<!-- candidate L{i}: {body} -->")
    if candidates:
        note = "\n".join(candidates)
    else:
        note = ("<!-- No requirement-like lines were detected. Read spec.md and add "
                "one section per requirement. -->")
    return features, [], note


def parse(manifest: dict[str, Any], text: str | None = None,
          path: Path | None = None) -> dict[str, Any]:
    root = project_root(manifest)
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    file_path = path or paths["requirements"]
    if text is None:
        if not file_path.is_file():
            raise FvError(
                f"extraction Markdown not found: {file_path}\n"
                "run `fv.py extract --design ...` to create it"
            )
        text = file_path.read_text()
    meta, body = _front_matter(text)
    # HTML comments hold the scaffold instructions, including example
    # requirement headings.  They must not become requirements.
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    spec = manifest.get("spec") or {}
    spec_path = resolve_path(root, meta.get("spec") or spec.get("markdown") or "spec/spec.md")
    if not spec_path.is_file():
        raise FvError(f"spec Markdown not found: {spec_path}")
    spec_text = spec_path.read_text()
    current_spec_hash = sha256_file(spec_path)
    recorded_spec_hash = str(meta.get("spec_sha256") or "").strip()
    features, _ = _feature_section(body)
    sections = _requirement_sections(body)
    requirements: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    if not recorded_spec_hash:
        warnings.append(
            "extraction has no spec_sha256; regenerate it with `fv.py extract` "
            "so a changed spec cannot leave stale requirements")
    elif recorded_spec_hash != current_spec_hash:
        errors.append(
            "spec.md changed since requirements.md was generated; review the "
            "extraction against the new spec, update it, then run "
            "`fv.py extract --update-spec-hash`")
    seen: set[str] = set()
    for item in sections:
        rid = item["id"]
        if rid in seen:
            errors.append(f"duplicate requirement {rid}")
        seen.add(rid)
        fields = item["fields"]
        feature = _field(fields, "feature")
        statement = _field(fields, "statement", "requirement", "text")
        source = _field(fields, "source", "source lines")
        evidence = _strip_quotes(_field(fields, "evidence", "quote"))
        clarity = _field(fields, "clarity").lower() or "clear"
        proposed = _field(fields, "proposed property", "proposed formalization",
                          "proposed").lower()
        question = _field(fields, "question")
        owner_field = _field(fields, "owner")
        owner = owner_field or "spec_agent"
        blocking_field = _field(fields, "blocking").strip().lower()
        blocking = _bool(blocking_field)
        category = _field(fields, "category") or ""
        notes = _field(fields, "notes")
        if not feature:
            errors.append(f"{rid}: missing Feature")
        elif features and feature not in {f["id"] for f in features}:
            errors.append(f"{rid}: Feature {feature!r} is not in the feature table")
        if not statement:
            errors.append(f"{rid}: missing Statement")
        if not source:
            errors.append(f"{rid}: missing Source")
        elif not SOURCE_RE.fullmatch(source.strip()):
            errors.append(f"{rid}: Source {source!r} is not L<n> or L<n>-L<m>")
        elif _source_lines(source)[0] > len(spec_text.splitlines()):
            errors.append(f"{rid}: Source {source!r} is past the end of spec.md")
        if not evidence:
            errors.append(f"{rid}: missing Evidence")
        if clarity not in CLARITY:
            errors.append(f"{rid}: Clarity {clarity!r} is not one of {sorted(CLARITY)}")
        if proposed not in PROPOSED:
            errors.append(f"{rid}: Proposed property {proposed!r} is not one of {sorted(PROPOSED)}")
        if owner_field and owner_field not in OWNERS:
            errors.append(f"{rid}: Owner {owner_field!r} is not one of {sorted(OWNERS)}")
        if clarity != "clear" and not owner_field:
            errors.append(
                f"{rid}: Clarity {clarity!r} requires an explicit Owner "
                f"({', '.join(sorted(OWNERS))})")
        if blocking_field not in BOOLEAN:
            errors.append(
                f"{rid}: Blocking {blocking_field!r} must be yes or no")
        if clarity != "clear" and not question:
            errors.append(f"{rid}: Clarity {clarity!r} requires a Question")
        if clarity != "clear" and not owner:
            errors.append(f"{rid}: Clarity {clarity!r} requires an Owner")
        if evidence:
            if not _quote_check(spec_text, evidence, source):
                message = f"{rid}: evidence quote is not verbatim in the spec or its source range"
                (warnings if clarity != "clear" else errors).append(message)
        if "TODO" in statement or "TODO" in evidence:
            errors.append(f"{rid}: contains a TODO marker")
        requirements.append({
            "id": rid,
            "title": item.get("title") or "",
            "feature": feature,
            "category": category,
            "text": statement,
            "source": source,
            "evidence": evidence,
            "clarity": clarity,
            "question": question,
            "owner": owner,
            "blocking": blocking,
            "proposed": proposed,
            "notes": notes,
        })
    # Attach requirements to features.
    by_feature: dict[str, list[str]] = {}
    for req in requirements:
        by_feature.setdefault(req["feature"], []).append(req["id"])
    for feat in features:
        feat["requirements"] = by_feature.get(str(feat["id"]), [])
    for fid in by_feature:
        if features and fid not in {f["id"] for f in features}:
            errors.append(f"feature {fid!r} is cited but not in the feature table")
    stats = {
        "features": len(features),
        "requirements": len(requirements),
        "clear": sum(1 for r in requirements if r["clarity"] == "clear"),
        "unclear": sum(1 for r in requirements if r["clarity"] != "clear"),
        "blocking": sum(1 for r in requirements if r["clarity"] != "clear" and r["blocking"]),
        "errors": len(errors),
        "warnings": len(warnings),
        "ready": bool(requirements) and not errors,
    }
    doc = {
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "generated_at": _now(),
        "generated_by": "formal-sva/fv_extract.py",
        "design": {
            "name": meta.get("design") or manifest.get("name"),
            "top": meta.get("top") or manifest.get("top"),
            "spec": rel_to(root, spec_path),
            "spec_sha256": sha256_file(spec_path),
            "extraction": rel_to(root, file_path),
            "extraction_sha256": sha256_file(file_path) if file_path.is_file() else "",
        },
        "features": features,
        "requirements": requirements,
        "stats": stats,
        "errors": errors,
        "warnings": warnings,
    }
    return doc


def _write_questions_from_extraction(manifest: dict[str, Any], doc: dict[str, Any]) -> None:
    from fv_questions import collect, write_questions

    bundle = {
        "design": doc.get("design") or {},
        "features": doc.get("features") or [],
        "requirements": doc.get("requirements") or [],
    }
    questions = collect(manifest, bundle=bundle, contract={})
    write_questions(manifest, questions)


def write_extraction_artifacts(manifest: dict[str, Any], doc: dict[str, Any]) -> dict[str, str]:
    """Persist the normalized extraction and rebuild the clarification report."""
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    write_json(paths["extraction"], doc)
    _write_questions_from_extraction(manifest, doc)
    root = project_root(manifest)
    return {
        "extraction": rel_to(root, paths["extraction"]),
        "questions": rel_to(root, Path(manifest["_paths"]["reports"]) / "questions.md"),
    }


def update_spec_hash(manifest: dict[str, Any]) -> dict[str, Any]:
    """Record the current spec hash after the extraction was reviewed.

    A changed spec is an error, not a silent refresh.  This function is the
    deliberate, explicit action a reviewer takes after updating the extraction
    to match the new spec; the following parse still checks every evidence
    quote against the new text.
    """
    root = project_root(manifest)
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    md_path = paths["requirements"]
    if not md_path.is_file():
        raise FvError(
            f"extraction Markdown not found: {md_path}\n"
            "run `fv.py extract --design ...` to create the scaffold first"
        )
    text = md_path.read_text()
    meta, _ = _front_matter(text)
    spec = manifest.get("spec") or {}
    spec_path = resolve_path(root, meta.get("spec") or spec.get("markdown") or "spec/spec.md")
    if not spec_path.is_file():
        raise FvError(f"spec Markdown not found: {spec_path}")
    current = sha256_file(spec_path)
    previous = str(meta.get("spec_sha256") or "").strip()
    if previous == current:
        return {"changed": False, "spec_sha256": current,
                "path": rel_to(root, md_path)}

    if not meta:
        header = [
            "---",
            f"schema_version: {EXTRACTION_SCHEMA_VERSION}",
            f"design: {manifest.get('name') or manifest.get('top') or 'design'}",
            f"top: {manifest.get('top')}",
            f"spec: {rel_to(root, spec_path)}",
            f"spec_sha256: {current}",
            "generated_by: formal-sva/fv_extract.py",
            "---",
            "",
        ]
        text = "\n".join(header) + text.lstrip("\n")
    else:
        lines = text.splitlines(keepends=True)
        replaced = False
        for i, line in enumerate(lines):
            if i == 0:
                continue
            if line.strip() == "---":
                if not replaced:
                    ending = "\r\n" if line.endswith("\r\n") else (
                        "\n" if line.endswith("\n") else "")
                    lines.insert(i, f"spec_sha256: {current}{ending}")
                break
            if re.match(r"^spec_sha256\s*:", line):
                ending = "\r\n" if line.endswith("\r\n") else (
                    "\n" if line.endswith("\n") else "")
                lines[i] = f"spec_sha256: {current}{ending}"
                replaced = True
        text = "".join(lines)
    write_text(md_path, text)
    return {"changed": True, "spec_sha256": current,
            "previous": previous, "path": rel_to(root, md_path)}


def extract(manifest: dict[str, Any], force: bool = False,
            quiet: bool = False) -> dict[str, Any]:
    root = project_root(manifest)
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    md_path = paths["requirements"]
    spec = manifest.get("spec") or {}
    markdown_path = resolve_path(root, spec.get("markdown") or "spec/spec.md")
    if not markdown_path.is_file():
        raise FvError(
            f"spec Markdown not found: {markdown_path}\n"
            "point manifest spec.markdown at the generated spec.md"
        )
    if force or not md_path.is_file():
        structured_path = resolve_path(root, spec["structured"]) if spec.get("structured") else None
        if structured_path and structured_path.is_file():
            structured = read_json(structured_path)
            config_path = resolve_path(root, spec["config"]) if spec.get("config") else None
            config = read_json(config_path) if config_path and config_path.is_file() else {}
            spec_text = markdown_path.read_text()
            features = _features_from_structured(structured, config, spec_text)
            requirements = _structured_requirements(structured, config, features, spec_text)
            text = _markdown(manifest, features, requirements,
                             rel_to(root, markdown_path), sha256_file(markdown_path))
        else:
            features, requirements, note = _spec_only_scaffold(markdown_path.read_text())
            text = _markdown(manifest, features, requirements,
                             rel_to(root, markdown_path), sha256_file(markdown_path))
            marker = "## Requirements\n\n"
            if note and marker in text:
                text = text.replace(marker, marker + note + "\n\n", 1)
        write_text(md_path, text)
        if not quiet:
            print(f"extraction: {rel_to(root, md_path)}")
            print(f"  {len(features)} features, {len(requirements)} requirements")
            if not requirements:
                print("  next: read prompts/extract.md and fill the requirement sections")
    doc = parse(manifest)
    doc["design"]["design"] = rel_to(root, Path(manifest["_path"]))
    doc["design"]["rules_sha256"] = ""
    write_extraction_artifacts(manifest, doc)
    if not quiet:
        stats = doc["stats"]
        print(f"parsed:     {rel_to(root, paths['extraction'])}")
        print(f"  {stats['clear']} clear, {stats['unclear']} unclear, "
              f"{stats['errors']} error(s), {stats['warnings']} warning(s)")
        for error in doc["errors"]:
            print(f"  ERROR {error}")
        for warning in doc["warnings"]:
            print(f"  WARN  {warning}")
    return doc


def scaffold_manifest(manifest_path: Path, quiet: bool = False) -> None:
    """Create the extraction file from a manifest that was just written."""
    from fv_common import load_manifest

    manifest = load_manifest(manifest_path)
    extract(manifest, quiet=quiet)
