"""Human-readable SVA catalog for review."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fv_common import project_root, read_json, rel_to, write_json, write_text


def _expr_signals(prop: dict[str, Any]) -> list[str]:
    try:
        from fv_contract import expr_identifiers
    except Exception:
        expr_identifiers = None
    names: list[str] = []
    for key in ("expr", "trigger", "response", "when", "signal"):
        val = prop.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        if expr_identifiers:
            try:
                names.extend(expr_identifiers(val))
            except Exception:
                pass
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _check_text(prop: dict[str, Any]) -> str:
    kind = str(prop.get("kind") or "")
    role = str(prop.get("role") or "")
    if kind == "reset_value":
        return f"During reset, `{prop.get('expr','')}` must hold."
    if kind == "always":
        expr = prop.get("expr") or ""
        when = prop.get("when")
        if when:
            return f"When `{when}`, always `{expr}`."
        return f"Always `{expr}`."
    if kind == "next":
        trig = prop.get("trigger") or "trigger"
        expr = prop.get("expr") or ""
        when = prop.get("when")
        base = f"After `{trig}`, next cycle `{expr}`."
        return f"When `{when}`, {base}" if when else base
    if kind == "response":
        return (f"After `{prop.get('trigger','')}`, expect `{prop.get('response','')}` "
                f"within [{prop.get('min_delay')},{prop.get('max_delay')}] cycles.")
    if kind == "stability":
        return f"When `{prop.get('trigger','')}`, signal `{prop.get('signal','')}` stays stable for {prop.get('cycles','?')} cycles."
    if kind == "cover":
        return f"Cover reachable scenario: `{prop.get('expr','')}`."
    return f"{role} {kind}: `{prop.get('expr') or prop.get('trigger') or ''}`"


def build_catalog(manifest: dict[str, Any], results: dict[str, Any] | None = None) -> dict[str, Any]:
    root = project_root(manifest)
    cpath = Path(manifest["_paths"]["contract"])
    rpath = Path(manifest["_paths"]["results"])
    contract = read_json(cpath) if cpath.is_file() else {}
    if results is None and rpath.is_file():
        results = read_json(rpath)
    results = results or {}
    res_by_id = {str(p.get("id")): p for p in results.get("properties") or []}
    req_by_id = {str(r.get("id")): r for r in (contract.get("requirements") or [])}
    items = []
    for p in contract.get("properties") or []:
        pid = str(p.get("id"))
        rr = res_by_id.get(pid, {})
        req = req_by_id.get(str(p.get("req")), {})
        ev = p.get("evidence") or req.get("evidence") or {}
        item = {
            "id": pid,
            "req": p.get("req"),
            "feature": p.get("feature"),
            "title": p.get("title") or rr.get("title") or "",
            "kind": p.get("kind"),
            "role": p.get("role"),
            "clock": p.get("clock"),
            "reset": p.get("reset"),
            "domain": p.get("domain"),
            "disable_reset": p.get("disable_reset"),
            "expression": p.get("expr"),
            "trigger": p.get("trigger"),
            "response": p.get("response"),
            "when": p.get("when"),
            "min_delay": p.get("min_delay"),
            "max_delay": p.get("max_delay"),
            "signal": p.get("signal"),
            "cycles": p.get("cycles"),
            "signals_referenced": _expr_signals(p),
            "checks": _check_text(p),
            "intent": p.get("intent") or {},
            "evidence": {"section": ev.get("section"), "quote": ev.get("quote")},
            "notes": p.get("notes"),
            "status": rr.get("status"),
            "status_reason": rr.get("status_reason"),
            "waveform_vcd": rr.get("waveform_vcd") or rr.get("vcd"),
            "waveform_svg": rr.get("waveform_svg") or rr.get("svg"),
            "waveform_gtkw": rr.get("waveform_gtkw") or rr.get("gtkw"),
            "bmc_log": ((rr.get("bmc") or {}).get("log")),
            "induction_log": ((rr.get("induction") or {}).get("log")),
        }
        items.append(item)
    return {
        "schema_version": "1.0",
        "design": contract.get("design") or results.get("design") or {},
        "summary": {
            "properties": len(items),
            "by_status": {},
        },
        "properties": items,
    }


def render_markdown(catalog: dict[str, Any]) -> str:
    design = catalog.get("design") or {}
    props = catalog.get("properties") or []
    by_status: dict[str, int] = {}
    for p in props:
        s = str(p.get("status") or "UNKNOWN")
        by_status[s] = by_status.get(s, 0) + 1
    lines = [f"# SVA Catalog: {design.get('name') or design.get('top') or 'design'}", ""]
    lines.append("This report lists every SVA/property in the contract, its purpose, what it checks, and current status.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Properties: **{len(props)}**")
    for k, v in sorted(by_status.items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Properties")
    lines.append("")
    for p in props:
        lines.append(f"### `{p['id']}` — {p.get('title') or '(no title)'}")
        lines.append("")
        lines.append(f"- **Requirement:** `{p.get('req')}`  **Feature:** `{p.get('feature')}`")
        lines.append(f"- **Kind/Role:** `{p.get('kind')}` / `{p.get('role')}`  **Status:** **{p.get('status') or 'N/A'}**")
        dom = []
        if p.get("clock"): dom.append(f"clock=`{p['clock']}`")
        if p.get("reset"): dom.append(f"reset=`{p['reset']}`")
        if p.get("domain"): dom.append(f"domain=`{p['domain']}`")
        if dom:
            lines.append(f"- **Domain:** {', '.join(dom)}")
        lines.append(f"- **Purpose / checks:** {p.get('checks')}")
        for field in ("expression", "trigger", "response", "when", "signal"):
            val = p.get(field)
            if val not in (None, "", []):
                lines.append(f"- **{field.capitalize()}:** `{val}`")
        if p.get("min_delay") is not None or p.get("max_delay") is not None:
            lines.append(f"- **Window:** [{p.get('min_delay')}, {p.get('max_delay')}] cycles={p.get('cycles')}")
        if p.get("signals_referenced"):
            lines.append(f"- **Signals referenced:** {', '.join('`'+s+'`' for s in p['signals_referenced'])}")
        ev = p.get("evidence") or {}
        if ev.get("quote"):
            lines.append(f"- **Evidence ({ev.get('section','')}):** {ev.get('quote')}")
        if p.get("status_reason"):
            lines.append(f"- **Status reason:** {p.get('status_reason')}")
        links = []
        for k, label in (("waveform_vcd","VCD"), ("waveform_svg","SVG"), ("waveform_gtkw","GTKWave"), ("bmc_log","BMC log"), ("induction_log","Induction log")):
            if p.get(k):
                links.append(f"{label}: `{p[k]}`")
        if links:
            lines.append(f"- **Artifacts:** {' | '.join(links)}")
        if p.get("notes"):
            lines.append(f"- **Notes:** {p.get('notes')}")
        lines.append("")
    return "\n".join(lines)


def write_catalog(manifest: dict[str, Any], results: dict[str, Any] | None = None) -> dict[str, str]:
    catalog = build_catalog(manifest, results)
    reports = Path(manifest["_paths"]["reports"])
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / "sva_catalog.json"
    md_path = reports / "sva_catalog.md"
    write_json(json_path, catalog)
    write_text(md_path, render_markdown(catalog))
    root = project_root(manifest)
    return {"catalog_json": rel_to(root, json_path), "catalog_md": rel_to(root, md_path)}
