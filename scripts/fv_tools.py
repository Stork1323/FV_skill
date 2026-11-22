"""Tool configuration and backend selection.

The skill does not hard-code a prover.  ``formal/tools.json`` names the default
backend and holds one configuration object per backend.  The checked-in default
is SymbiYosys; ``yosys`` remains available as a dependency-free fallback and a
generic ``command`` backend connects commercial tools such as VC Formal.

Configuration is data, not code.  A commercial backend is described by the
command to run, the files it consumes and regular expressions that classify its
output.  This keeps proprietary tool names and licence wrappers out of the
skill scripts.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    project_root,
    read_json,
    rel_to,
    run_cmd,
    sha256_file,
    write_json,
)

TOOLS_SCHEMA_VERSION = "1.0"
DEFAULT_BACKEND = "symbiyosys"

# The command backend is intentionally generic.  The VC Formal entry is a
# template: fill `command` with the site's invocation and `result_pattern` with
# the line its log prints for PASS/FAIL.  It is disabled until `command` is set.
DEFAULT_TOOLS: dict[str, Any] = {
    "schema_version": TOOLS_SCHEMA_VERSION,
    "default": DEFAULT_BACKEND,
    "backends": {
        "symbiyosys": {
            "kind": "symbiyosys",
            "description": "SymbiYosys (sby) with the smtbmc engine",
            "executable": "sby",
            "engine": "smtbmc",
            "timeout_s": 300,
        },
        "yosys": {
            "kind": "yosys",
            "description": "Direct Yosys SAT, shipped as the dependency-free fallback",
            "executable": "yosys",
            "timeout_s": 300,
        },
        "vc_formal": {
            "kind": "command",
            "description": "VC Formal or any commercial prover, via a command template",
            "enabled": False,
            "executable": "vc_fv",
            "command": [],
            "script_template": "",
            "script_extension": ".tcl",
            "result_pattern": r"^(?:RESULT|STATUS)\s*[:=]\s*(?P<status>PROVEN|PASS|FAIL|ERROR|TIMEOUT)\b",
            "status_map": {
                "PROVEN": "PROVEN",
                "PASS": "BMC_PASS",
                "FAIL": "FAIL",
                "ERROR": "ERROR",
                "TIMEOUT": "TIMEOUT",
            },
            "timeout_s": 300,
        },
    },
}


def tools_path(manifest: dict[str, Any]) -> Path:
    return Path(manifest["_paths"]["tools"])


def ensure_tools(manifest: dict[str, Any], force: bool = False) -> Path:
    """Create formal/tools.json with the SymbiYosys default if it is missing."""
    path = tools_path(manifest)
    if path.is_file() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, DEFAULT_TOOLS)
    return path


def load_tools(manifest: dict[str, Any], create: bool = True) -> dict[str, Any]:
    path = tools_path(manifest)
    if not path.is_file():
        if not create:
            raise FvError(
                f"tool configuration not found: {path}\n"
                "run `fv.py init` (or `fv.py tools --design ...`) to create it"
            )
        ensure_tools(manifest)
    tools = read_json(path)
    if not isinstance(tools, dict):
        raise FvError(f"{path}: tool configuration must be a JSON object")
    if tools.get("schema_version") != TOOLS_SCHEMA_VERSION:
        raise FvError(
            f"{path}: unsupported tools schema {tools.get('schema_version')!r}; "
            f"expected {TOOLS_SCHEMA_VERSION!r}"
        )
    backends = tools.get("backends")
    if not isinstance(backends, dict) or not backends:
        raise FvError(f"{path}: `backends` must be a non-empty object")
    if not tools.get("default"):
        tools["default"] = DEFAULT_BACKEND
    if tools["default"] not in backends:
        raise FvError(
            f"{path}: default backend {tools['default']!r} is not in `backends` "
            f"({', '.join(sorted(backends))})"
        )
    return tools


def backend_name(manifest: dict[str, Any], tools: dict[str, Any],
                 override: str | None = None) -> str:
    """CLI override > manifest formal.backend > tools.json default."""
    if override and override != "auto":
        name = override
    else:
        manifest_backend = (manifest.get("_formal") or {}).get("backend") or "auto"
        name = tools.get("default") if manifest_backend == "auto" else manifest_backend
    if name not in (tools.get("backends") or {}):
        raise FvError(
            f"unknown formal backend {name!r}; configured backends: "
            + ", ".join(sorted(tools.get("backends") or {}))
        )
    return str(name)


def backend_config(tools: dict[str, Any], name: str) -> dict[str, Any]:
    cfg = (tools.get("backends") or {}).get(name)
    if not isinstance(cfg, dict):
        raise FvError(f"backend {name!r} has no configuration object")
    cfg = dict(cfg)
    cfg.setdefault("kind", name)
    cfg["name"] = name
    return cfg


def executable(cfg: dict[str, Any]) -> str | None:
    exe = cfg.get("executable")
    if not exe:
        return None
    return shutil.which(str(exe))


def available(cfg: dict[str, Any]) -> bool:
    if cfg.get("kind") == "command" and cfg.get("enabled") is False:
        return False
    if cfg.get("kind") == "command" and not cfg.get("command"):
        return False
    return executable(cfg) is not None


def describe(manifest: dict[str, Any], tools: dict[str, Any],
             override: str | None = None) -> dict[str, Any]:
    name = backend_name(manifest, tools, override)
    cfg = backend_config(tools, name)
    exe = executable(cfg)
    return {
        "name": name,
        "kind": cfg.get("kind"),
        "description": cfg.get("description"),
        "executable": cfg.get("executable"),
        "path": exe,
        "available": available(cfg),
        "config": cfg,
    }


def tools_hash(manifest: dict[str, Any]) -> str:
    path = tools_path(manifest)
    return sha256_file(path) if path.is_file() else ""


def backend_version(cfg: dict[str, Any]) -> str:
    """Best-effort version string; never fails a run."""
    exe = executable(cfg)
    if not exe:
        return ""
    kind = cfg.get("kind")
    flags = ["--version"]
    if kind == "yosys":
        flags = ["-V"]
    try:
        res = run_cmd([exe, *flags], cwd=Path(exe).parent, timeout_s=15)
        text = " ".join((res.get("output") or "").split())
        return text[:200]
    except Exception:
        return ""


def ensure_runnable(manifest: dict[str, Any], tools: dict[str, Any],
                    name: str) -> dict[str, Any]:
    cfg = backend_config(tools, name)
    kind = cfg.get("kind")
    if kind == "command" and cfg.get("enabled") is False:
        raise FvError(
            f"backend {name!r} is disabled in {rel_to(project_root(manifest), tools_path(manifest))}; "
            "set enabled=true and fill its command template"
        )
    if not available(cfg):
        hint = ""
        if kind == "symbiyosys":
            hint = (" Install OSS CAD Suite (which provides sby) or select the "
                    "'yosys' backend in formal/tools.json.")
        elif kind == "command":
            hint = " Fill the backend's `command` template in formal/tools.json."
        raise FvError(
            f"backend {name!r} is not available"
            + (f" (executable {cfg.get('executable')!r} not found)" if cfg.get("executable") else "")
            + "." + hint
        )
    return cfg


def validate_tools(tools: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    for name, cfg in (tools.get("backends") or {}).items():
        if not isinstance(cfg, dict):
            errors.append(f"{path}: backend {name!r} must be an object")
            continue
        kind = cfg.get("kind")
        if kind not in ("yosys", "symbiyosys", "command"):
            errors.append(f"{path}: backend {name!r} has unknown kind {kind!r}")
        if kind == "command":
            cmd = cfg.get("command")
            if cmd is not None and not isinstance(cmd, (list, str)):
                errors.append(f"{path}: backend {name!r} command must be a list or a string")
            try:
                re.compile(str(cfg.get("result_pattern") or ""))
            except re.error as exc:
                errors.append(f"{path}: backend {name!r} result_pattern is invalid: {exc}")
    return errors
