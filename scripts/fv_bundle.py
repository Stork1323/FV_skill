"""Build the deterministic extraction bundle (Q3TUI spec artifacts -> one JSON).

The bundle is the *only* input the contract-writing LLM stage needs besides the
spec Markdown itself.  It contains the requirement list, feature matrix,
resolved parameters, interface ports, source hashes and section headings.
Nothing here is inferred from the RTL except the interface (names, widths).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fv_common import (
    DEFAULT_FORMAL,
    FvError,
    eprint,
    load_manifest,
    project_root,
    read_json,
    rel_to,
    resolve_path,
    sha256_file,
    sha256_paths,
    source_files,
    write_json,
)
from fv_design import const_eval, extract_interface, interface_text


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if value is None:
        return None
    return const_eval(str(value), {})


def _requirements(structured: dict[str, Any]) -> list[dict[str, Any]]:
    raw = structured.get("requirements") or []
    out: list[dict[str, Any]] = []
    for i, req in enumerate(raw):
        if not isinstance(req, dict):
            continue
        rid = req.get("req_id") or req.get("id") or f"REQ-{i + 1:03d}"
        out.append({
            "id": str(rid),
            "text": str(req.get("text") or req.get("description") or "").strip(),
            "category": str(req.get("category") or ""),
            "feature": str(req.get("feature_id") or req.get("feature") or ""),
            "sva_hint": req.get("sva_hint"),
            "source_section": str(req.get("source_section") or ""),
            "optional": bool(req.get("optional", False)),
            "modules": list(req.get("rtl_modules") or []),
            "ambiguity_score": req.get("ambiguity_score"),
        })
    return out


FEATURE_ROW_RE = re.compile(r"^\|\s*(F\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|")


def _features_from_markdown(markdown: str) -> dict[str, dict[str, Any]]:
    """Feature table rows (`| F01 | text | Mandatory |`), section-aware when possible."""
    lines = markdown.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if re.match(r"^#{2,4}\s*(?:\d+\.?\s*)?Key Features\b", line.strip(), re.I):
            start = i + 1
            break
    found: dict[str, dict[str, Any]] = {}
    for line in lines[start:] if start else lines:
        m = FEATURE_ROW_RE.match(line.strip())
        if m:
            fid, text, klass = m.group(1), m.group(2).strip(), m.group(3).strip()
            text = re.sub(r"`", "", text)
            klass = re.sub(r"[*_]", "", klass).strip()
            found[fid] = {"id": fid, "text": text, "class": klass, "requirements": []}
    return found


def _features(config: dict[str, Any], requirements: list[dict[str, Any]],
              markdown: str) -> list[dict[str, Any]]:
    md = _features_from_markdown(markdown)
    by_id: dict[str, dict[str, Any]] = {}

    def ensure(fid: str) -> dict[str, Any]:
        if fid not in by_id:
            by_id[fid] = {
                "id": fid,
                "text": md.get(fid, {}).get("text", ""),
                "class": md.get(fid, {}).get("class", ""),
                "requirements": [],
            }
        return by_id[fid]

    for entry in config.get("feature_matrix") or []:
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("id") or entry.get("feature_id") or "").strip()
        if not fid:
            continue
        f = ensure(fid)
        if entry.get("name"):
            f["text"] = str(entry["name"])
        if entry.get("class"):
            f["class"] = str(entry["class"])
        for rid in entry.get("requirements") or []:
            if str(rid) not in f["requirements"]:
                f["requirements"].append(str(rid))
    for fid, f in md.items():
        ensure(fid)
    for req in requirements:
        fid = req.get("feature") or ""
        if not fid:
            continue
        f = ensure(fid)
        if req["id"] not in f["requirements"]:
            f["requirements"].append(req["id"])
    return list(by_id.values())


def _parameters(config: dict[str, Any], interface: dict[str, Any]) -> dict[str, dict[str, Any]]:
    detailed: dict[str, dict[str, Any]] = {}
    for name, entry in (config.get("parameters") or {}).items():
        if isinstance(entry, dict):
            value = entry.get("value", entry.get("default"))
            default = entry.get("default")
            ptype = entry.get("type", "")
            changed = entry.get("changed_from_default")
        else:
            value, default, ptype, changed = entry, entry, "", None
        v = _as_int(value)
        d = _as_int(default)
        detailed[str(name)] = {
            "value": v if v is not None else value,
            "default": d if d is not None else default,
            "type": str(ptype or ""),
            "changed_from_default": changed,
            "source": "final_config",
        }
    for name, value in (interface.get("parameters") or {}).items():
        if name not in detailed:
            detailed[name] = {
                "value": value,
                "default": value,
                "type": "int",
                "changed_from_default": None,
                "source": "rtl_default",
            }
        else:
            detailed[name]["value"] = value
            if detailed[name].get("default") is None:
                detailed[name]["default"] = value
    return detailed


def _constraints(config: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for cid, entry in (config.get("constraints") or {}).items():
        if isinstance(entry, dict):
            out.append({
                "id": str(cid),
                "rule": str(entry.get("rule") or ""),
                "pass": entry.get("pass"),
                "detail": str(entry.get("detail") or ""),
            })
        else:
            out.append({"id": str(cid), "rule": str(entry), "pass": None, "detail": ""})
    return out


def _spec_headings(markdown: str) -> list[dict[str, Any]]:
    out = []
    for line in markdown.splitlines():
        m = re.match(r"^(#{1,5})\s+(.*)$", line.strip())
        if m:
            out.append({"level": len(m.group(1)), "text": m.group(2).strip()})
    return out


def prepare(manifest: dict[str, Any], out_path: Path | None = None,
            quiet: bool = False) -> dict[str, Any]:
    root = project_root(manifest)
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    spec = manifest.get("spec") or {}
    structured_path = resolve_path(root, spec["structured"]) if spec.get("structured") else None
    config_path = resolve_path(root, spec["config"]) if spec.get("config") else None
    markdown_path = resolve_path(root, spec.get("markdown") or spec.get("markdown_path")) if (
        spec.get("markdown") or spec.get("markdown_path")) else None

    if not markdown_path or not markdown_path.is_file():
        raise FvError(
            "spec Markdown not found; set spec.markdown in formal/design.json. "
            "Evidence quotes are checked against this file."
        )
    markdown = markdown_path.read_text(encoding="utf-8")

    from fv_rules import ensure as ensure_rules
    from fv_tools import ensure_tools

    ensure_rules(manifest)
    ensure_tools(manifest)

    # The extraction Markdown is the reviewable requirement source.  For a
    # Q3TUI project it is generated from structured_spec.json; for a
    # spec-only project it is authored from prompts/extract.md.
    from fv_extract import extract as extract_spec

    extraction = extract_spec(manifest, quiet=True)
    if extraction.get("errors"):
        raise FvError(
            "requirement extraction has errors; fix formal/spec/requirements.md:\n"
            + "\n".join(f"  {e}" for e in extraction["errors"])
        )
    if not extraction.get("requirements"):
        raise FvError(
            f"{paths['requirements']} has no requirements. Read prompts/extract.md, "
            "fill the requirement sections, then re-run `fv.py extract`."
        )
    requirements = []
    hint_by_proposed = {
        "always": "property", "next": "property", "response": "property",
        "stability": "property", "reset_value": "property",
        "cover": "cover", "assume": "assume", "static": "static",
        "unknown": "", "": "",
    }
    for req in extraction["requirements"]:
        requirements.append({
            "id": str(req["id"]),
            "text": str(req.get("text") or "").strip(),
            "category": str(req.get("category") or ""),
            "feature": str(req.get("feature") or ""),
            "sva_hint": hint_by_proposed.get(str(req.get("proposed") or ""), ""),
            "source_section": str(req.get("source") or ""),
            "optional": False,
            "modules": [],
            "ambiguity_score": None,
            "clarity": str(req.get("clarity") or "clear"),
            "question": str(req.get("question") or ""),
            "owner": str(req.get("owner") or "spec_agent"),
            "blocking": bool(req.get("blocking")),
            "evidence": {"section": str(req.get("source") or ""),
                         "quote": str(req.get("evidence") or "")},
        })
    features = extraction.get("features") or []
    interface = extract_interface(manifest)
    structured = (read_json(structured_path)
                  if structured_path and structured_path.is_file() else {})
    config = read_json(config_path) if config_path and config_path.is_file() else {}
    params = _parameters(config, interface)
    src = source_files(manifest)
    if src["missing"]:
        raise FvError("missing RTL source(s): " + ", ".join(src["missing"]))

    design_meta = structured.get("metadata") or {}
    from fv_rules import rules_hash
    from fv_pipeline import record

    bundle = {
        "schema_version": "1.0",
        "generated_by": "formal-sva/fv_bundle.py",
        "design": {
            "name": manifest.get("name") or design_meta.get("ip_name") or interface["top"],
            "top": interface["top"],
            "clock": manifest.get("clock") or {},
            "reset": manifest.get("reset") or {},
            "parameters": interface.get("parameters") or {},
            "formal": manifest.get("_formal") or {},
            "spec_revision": design_meta.get("spec_revision"),
            "description": design_meta.get("description"),
        },
        "interface": {
            "top": interface["top"],
            "ports": interface["ports"],
            "parameters": interface.get("parameters") or {},
            "localparams": interface.get("localparams") or {},
            "unresolved_constants": interface.get("unresolved_constants") or [],
        },
        "features": features,
        "requirements": requirements,
        "parameters": params,
        "constraints": _constraints(config),
        "spec": {
            "markdown": rel_to(root, markdown_path),
            "headings": _spec_headings(markdown),
            "structured": rel_to(root, structured_path) if structured_path else None,
            "config": rel_to(root, config_path) if config_path else None,
            "extraction": rel_to(root, paths["requirements"]),
            "extraction_json": rel_to(root, paths["extraction"]),
            "spec_sva_json": rel_to(root, paths["extraction"].parent / "spec_sva.json") if (paths["extraction"].parent / "spec_sva.json").is_file() else None,
        },
        "spec_sva": extraction.get("spec_sva") or [],
        "rules_sha256": rules_hash(manifest),
        "sources": {
            "structured_spec": ({"path": rel_to(root, structured_path),
                                 "sha256": sha256_file(structured_path)}
                                if structured_path and structured_path.is_file() else None),
            "final_config": ({"path": rel_to(root, config_path), "sha256": sha256_file(config_path)}
                             if config_path and config_path.is_file() else None),
            "spec_markdown": {"path": rel_to(root, markdown_path), "sha256": sha256_file(markdown_path)},
            "requirements": ({"path": rel_to(root, paths["requirements"]),
                              "sha256": sha256_file(paths["requirements"])}
                             if paths["requirements"].is_file() else None),
            "extraction": ({"path": rel_to(root, paths["extraction"]),
                            "sha256": sha256_file(paths["extraction"])}
                           if paths["extraction"].is_file() else None),
            "design": {"path": rel_to(root, Path(manifest["_path"])), "sha256": sha256_file(Path(manifest["_path"]))},
            "rtl": [{"path": rel_to(root, p), "sha256": sha256_file(p)} for p in src["sources"]],
            "rtl_set_sha256": sha256_paths(src["sources"]),
        },
    }
    out = out_path or paths["bundle"]
    write_json(out, bundle)
    # Persist the interface separately too: the renderer and validator can read it
    # without re-elaborating the RTL.
    write_json(paths["interface"], interface)
    record(manifest, "extract")
    if not quiet:
        print(f"bundle:    {out}")
        print(f"interface: {paths['interface']}")
        print(f"  {len(requirements)} requirements, {len(features)} features, "
              f"{len(interface['ports'])} ports")
        if extraction.get("stats", {}).get("unclear"):
            print(f"  {extraction['stats']['unclear']} requirement(s) have open "
                  "clarification questions; see formal/reports/questions.md")
        print(interface_text(interface))
    return bundle


def init_design(project: Path, top: str | None = None, clock: str | None = None,
                reset: str | None = None, force: bool = False,
                spec: str | Path | None = None,
                rtl: str | Path | None = None,
                sources: list[str] | None = None) -> Path:
    project = Path(project).expanduser().resolve()
    manifest_path = project / "formal" / "design.json"
    if manifest_path.exists() and not force:
        raise FvError(f"{manifest_path} already exists (use --force to replace it)")

    def first(*candidates: str) -> str | None:
        for c in candidates:
            if (project / c).is_file():
                return c
        return None

    def rel_or_abs(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(project))
        except ValueError:
            return str(path.resolve())

    structured = first("schemas/structured_spec.json", "formal/schemas/structured_spec.json")
    config = first("schemas/final_config.json", "formal/schemas/final_config.json")

    # A spec may be a file or a directory supplied from outside the project.
    markdown: str | None = None
    if spec:
        spec_path = Path(spec).expanduser()
        if not spec_path.is_absolute():
            spec_path = (Path.cwd() / spec_path)
        if spec_path.is_dir():
            for name in ("spec.md", "specification.md"):
                if (spec_path / name).is_file():
                    spec_path = spec_path / name
                    break
            else:
                raise FvError(f"{spec_path}: no spec.md or specification.md")
        if not spec_path.is_file():
            raise FvError(f"spec Markdown not found: {spec_path}")
        markdown = rel_or_abs(spec_path)
    else:
        markdown = first("spec/spec.md", "docs/spec/spec.md", "spec.md")

    # RTL: a file list, a source file, a directory, or the usual project layout.
    filelist: str | None = None
    rtl_sources: list[str] = []
    if rtl:
        rtl_path = Path(rtl).expanduser()
        if not rtl_path.is_absolute():
            rtl_path = (Path.cwd() / rtl_path)
        if rtl_path.is_dir():
            hits = sorted(rtl_path.glob("filelist.f")) or sorted(rtl_path.glob("*.f"))
            if hits:
                filelist = rel_or_abs(hits[0])
            else:
                rtl_sources = [rel_or_abs(p) for p in sorted(rtl_path.glob("*.sv"))]
        elif rtl_path.suffix in (".f", ".lst"):
            filelist = rel_or_abs(rtl_path)
        elif rtl_path.is_file():
            rtl_sources = [rel_or_abs(rtl_path)]
        else:
            raise FvError(f"RTL path not found: {rtl_path}")
    if not filelist and not rtl_sources:
        filelist = first("src/rtl/filelist.f", "rtl/filelist.f", "src/filelist.f")
    if not filelist and not rtl_sources and not sources:
        for pattern in ("src/rtl/*.sv", "rtl/*.sv", "src/*.sv"):
            hits = sorted(str(p.relative_to(project)) for p in project.glob(pattern))
            if hits:
                rtl_sources = hits
                break
    if sources:
        rtl_sources = list(dict.fromkeys(list(sources) + rtl_sources))
    if not markdown:
        raise FvError(f"{project}: no spec/spec.md; pass --spec /path/to/spec.md")
    if not top:
        if structured:
            meta = read_json(project / structured).get("metadata") or {}
            top = meta.get("top_module")
        if not top:
            raise FvError("no top module could be inferred; pass --top")

    provisional = {
        "schema_version": "1.0",
        "name": project.name,
        "top": top,
        "spec": {
            "structured": structured,
            "config": config,
            "markdown": markdown,
        },
        "clock": {"signal": clock or "i_clk", "edge": "posedge"},
        "reset": {"signal": reset or "i_rst_n", "active": "low", "kind": "async"},
        "formal": dict(),
    }
    if filelist:
        provisional["rtl"] = {"filelist": filelist}
    elif rtl_sources:
        provisional["rtl"] = {"sources": rtl_sources}
    provisional["spec"] = {k: v for k, v in provisional["spec"].items() if v is not None}

    # Infer clock/reset from the elaborated interface when not supplied.
    if provisional.get("rtl"):
        try:
            iface = extract_interface({
                **provisional,
                "_root": str(project),
                "_path": str(manifest_path),
                "_paths": {"interface": str(project / "formal" / "schemas" / "design_interface.json")},
                "_formal": dict(),
            })
            names = [p["name"] for p in iface["ports"] if p.get("direction") == "input"]
            if not clock:
                for cand in ("i_clk", "clk", "clk_i", "clock", "i_clock", "aclk", "aclk_i"):
                    if cand in names:
                        provisional["clock"] = {"signal": cand, "edge": "posedge", "inferred": True}
                        break
                else:
                    hits = [n for n in names if re.search(r"(^|_)clk(_|$)", n, re.I)]
                    if len(hits) == 1:
                        provisional["clock"] = {"signal": hits[0], "edge": "posedge", "inferred": True}
            if not reset:
                hits = [n for n in names if re.search(r"(^|_)(rst|reset|clr|clear)(_|n$|ni$|b$|$)", n, re.I)]
                if len(hits) == 1:
                    sig = hits[0]
                    active = "low" if re.search(r"(n|ni|b|_n|_ni|_b)$", sig, re.I) else "high"
                    provisional["reset"] = {"signal": sig, "active": active, "kind": "unknown", "inferred": True}
                else:
                    provisional["reset"] = {"signal": reset or "i_rst_n", "active": "low",
                                            "kind": "unknown", "inferred": True}
        except FvError as exc:
            eprint(f"warning: could not infer clock/reset: {exc}")
            provisional["clock"] = {"signal": clock or "i_clk", "edge": "posedge", "inferred": True}
            provisional["reset"] = {"signal": reset or "i_rst_n", "active": "low", "kind": "unknown", "inferred": True}

    provisional["formal"] = dict(DEFAULT_FORMAL)
    provisional["formal"]["reset_cycles"] = (provisional.get("reset") or {}).get("cycles", 2)
    write_json(manifest_path, provisional)
    manifest = load_manifest(manifest_path)

    from fv_rules import generate as generate_rules
    from fv_tools import ensure_tools
    from fv_extract import extract as extract_spec

    ensure_tools(manifest)
    generate_rules(manifest)
    extract_spec(manifest, quiet=True)

    print(f"wrote {manifest_path}")
    print(f"  top:   {top}")
    print(f"  clock: {(provisional.get('clock') or {}).get('signal')} "
          f"({(provisional.get('clock') or {}).get('edge')})")
    print(f"  reset: {(provisional.get('reset') or {}).get('signal')} "
          f"({(provisional.get('reset') or {}).get('active')}-active, "
          f"{(provisional.get('reset') or {}).get('kind')})")
    if (provisional.get("clock") or {}).get("inferred") or (provisional.get("reset") or {}).get("inferred"):
        print("  WARNING: clock/reset were inferred; confirm them against the spec.")
    if not provisional.get("rtl"):
        print("  NOTE: no RTL was found. Extraction can run, but formal verification needs `rtl` in design.json.")
    print("next: python3 scripts/fv.py extract --design " + str(manifest_path))
    return manifest_path


if __name__ == "__main__":
    import argparse
    from fv_common import load_manifest

    ap = argparse.ArgumentParser(description="build the formal extraction bundle")
    ap.add_argument("--design", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    m = load_manifest(args.design)
    prepare(m, Path(args.out) if args.out else None)
