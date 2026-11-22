"""Generic command backend for commercial formal tools.

A commercial tool is configured in ``formal/tools.json``:

    "vc_formal": {
      "kind": "command",
      "enabled": true,
      "executable": "vc_fv",
      "command": ["vc_fv", "-f", "{script}", "-top", "{top}"],
      "script_template": "read_file {files}\nelaborate {top}\n...",
      "script_extension": ".tcl",
      "result_pattern": "RESULT: (?P<status>PROVEN|PASS|FAIL|ERROR|TIMEOUT)",
      "status_map": {...},
      "result_file": "{build}/result.json"
    }

The backend runs one command per property and phase, so a failure is
attributable to exactly one property.  It never guesses: a log without a
recognised result line is an ERROR, not a pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    eprint,
    project_root,
    rel_to,
    run_cmd,
    sanitize,
    write_text,
)
from fv_yosys import harness_files


def _quote(value: str) -> str:
    return f'"{value}"' if any(c.isspace() for c in value) else value


def _placeholders(harness: dict[str, Any], prop: dict[str, Any], depth: int,
                  timeout_s: int, phase: str, build: Path, script: Path,
                  log: Path) -> dict[str, str]:
    root = Path(harness["_root"])
    if prop.get("monitor_file") and prop.get("top_file"):
        files = harness_files(harness, prop)
    else:
        files = [Path(root) / p for p in (harness.get("files") or {}).get("dut") or []]
    return {
        "root": str(root),
        "build": str(build),
        "script": str(script),
        "log": str(log),
        "top": str(prop.get("top")),
        "property": str(prop.get("id")),
        "depth": str(depth),
        "timeout": str(timeout_s),
        "phase": phase,
        "files": " ".join(_quote(str(p)) for p in files),
        "dut": " ".join(_quote(str(p)) for p in
                        [Path(root) / p for p in (harness.get("files") or {}).get("dut") or []]),
    }


def _format(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        try:
            return value.format(**values)
        except KeyError as exc:
            raise FvError(f"unknown command placeholder {exc} in {value!r}")
    if isinstance(value, list):
        return [_format(v, values) for v in value]
    return value


def _read_result_file(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        for key in ("status", "result", "verdict"):
            if data.get(key):
                return str(data[key])
    return ""


def _classify(cfg: dict[str, Any], output: str, result_file: str) -> str:
    if result_file:
        raw = result_file
    else:
        pattern = cfg.get("result_pattern")
        raw = ""
        if pattern:
            rx = re.compile(str(pattern), re.MULTILINE | re.IGNORECASE)
            for match in rx.finditer(output):
                raw = (match.groupdict().get("status")
                       or (match.group(1) if match.groups() else match.group(0)))
                break
    if not raw:
        return ""
    status_map = {str(k).upper(): str(v).upper()
                  for k, v in (cfg.get("status_map") or {}).items()}
    return status_map.get(raw.strip().upper(), raw.strip().upper())


def _phase(cfg: dict[str, Any], harness: dict[str, Any], prop: dict[str, Any],
           depth: int, timeout_s: int, phase: str, quiet: bool) -> dict[str, Any]:
    root = Path(harness["_root"])
    pid = str(prop["id"])
    file_san = str(prop.get("file_san") or sanitize(pid).lower())
    paths = harness.get("_paths") or {}
    build_root = Path(paths.get("build") or (root / "formal" / "build")) / "command" / file_san
    build = build_root / phase
    logs = Path(paths.get("reports") or (root / "formal" / "reports")) / "logs"
    build.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    ext = str(cfg.get("script_extension") or ".tcl")
    script = build / f"{phase}{ext}"
    log = logs / f"{file_san}.{phase}.log"
    values = _placeholders(harness, prop, depth, timeout_s, phase, build, script, log)

    template = cfg.get("script_template") or ""
    if template:
        write_text(script, str(_format(template, values)) + "\n")
    elif not script.is_file():
        write_text(script, f"// formal-sva {phase} script for {pid}\n")

    command = cfg.get("command")
    if not command:
        raise FvError(
            f"commercial backend {cfg.get('name')!r} has no command; fill "
            "`command` in formal/tools.json"
        )
    argv = _format(command, values)
    if isinstance(argv, str):
        argv = ["/bin/sh", "-c", argv]
    if not quiet:
        eprint(f"[{pid}] {cfg.get('name')} {phase}")
    res = run_cmd(argv, cwd=build, timeout_s=timeout_s)
    output = res.get("output") or ""
    write_text(log, output)

    result_file = cfg.get("result_file")
    file_status = ""
    if result_file:
        file_status = _read_result_file(Path(str(_format(result_file, values))))
    raw = _classify(cfg, output, file_status)
    if res.get("timed_out"):
        raw = "TIMEOUT"
    elif not raw and res.get("returncode") == 0 and cfg.get("assume_pass_on_zero"):
        raw = "PASS"
    elif not raw and res.get("returncode") not in (0, None):
        raw = "ERROR"

    return {
        "result": raw.lower() if raw else "unknown",
        "status": raw,
        "line": next((x.strip() for x in output.splitlines()
                      if cfg.get("result_pattern")
                      and re.search(str(cfg["result_pattern"]), x, re.IGNORECASE)), ""),
        "returncode": res.get("returncode"),
        "timed_out": bool(res.get("timed_out")),
        "elapsed_s": res.get("elapsed_s"),
        "script": rel_to(root, script),
        "log": rel_to(root, log),
        "argv": res.get("argv"),
    }


def _counterexample(output: str, limit: int = 160) -> list[str]:
    rows = [line.rstrip() for line in output.splitlines()
            if re.match(r"^\s*\d+\s+[\\$A-Za-z_]", line)]
    return rows[-limit:]


def run_property(harness: dict[str, Any], prop: dict[str, Any], cfg: dict[str, Any],
                 depth: int | None = None, timeout_s: int | None = None,
                 induction: bool | None = None, quiet: bool = False) -> dict[str, Any]:
    root = Path(harness["_root"])
    design = harness.get("design") or {}
    formal = harness.get("formal") or {}
    pid = str(prop["id"])
    actual_depth = int(depth or prop.get("depth") or formal.get("depth") or 12)
    actual_timeout = int(timeout_s or cfg.get("timeout_s") or formal.get("timeout_s") or 300)
    want_induction = bool(formal.get("induction", True) if induction is None else induction)
    want_induction = want_induction and bool(prop.get("induction", True))

    bmc = _phase(cfg, harness, prop, actual_depth, actual_timeout, "bmc", quiet)
    result: dict[str, Any] = {
        "id": pid,
        "kind": prop.get("kind"),
        "role": prop.get("role"),
        "mode": prop.get("mode"),
        "depth": actual_depth,
        "reset_cycles": int(design.get("reset_cycles") or
                            (design.get("reset") or {}).get("cycles") or 0),
        "timeout_s": actual_timeout,
        "backend": cfg.get("name"),
        "bmc": {k: bmc.get(k) for k in
                ("result", "status", "line", "returncode", "timed_out",
                 "elapsed_s", "script", "log", "argv")},
        "induction": None,
        "counterexample": [],
        "status": "ERROR",
        "error": "",
        "elapsed_s": bmc.get("elapsed_s") or 0.0,
    }

    if bmc.get("timed_out"):
        result["status"] = "TIMEOUT"
        result["error"] = f"BMC timed out after {actual_timeout}s"
        return result
    status = str(bmc.get("status") or "")
    if not status:
        result["status"] = "ERROR"
        result["error"] = (
            "no result line matched the backend's result_pattern; inspect the log "
            "and update formal/tools.json"
        )
        return result
    if status in ("ERROR", "TIMEOUT"):
        result["status"] = status
        result["error"] = bmc.get("line") or f"backend reported {status}"
        return result

    mode = prop.get("mode")
    if mode in ("cover", "assume"):
        good = {"COVERED", "PASS", "ASSUMED"}
        bad = {"UNREACHABLE", "FAIL", "VACUOUS"}
        if status in good:
            result["status"] = ("ASSUMED" if mode == "assume" else "COVERED")
        elif status in bad:
            result["status"] = ("VACUOUS" if mode == "assume" else "UNREACHABLE")
        else:
            result["status"] = "ERROR"
            result["error"] = f"backend returned {status!r} for a {mode} property"
        if result["status"] in ("VACUOUS", "UNREACHABLE"):
            log_path = root / str(bmc.get("log") or "")
            result["counterexample"] = _counterexample(
                log_path.read_text() if log_path.is_file() else "")
        if not quiet:
            eprint(f"[{pid}] {result['status']} ({result['elapsed_s']}s)")
        return result

    if status in ("FAIL", "BMC_FAIL"):
        result["status"] = "FAIL"
        log_path = root / str(bmc.get("log") or "")
        result["counterexample"] = _counterexample(
            log_path.read_text() if log_path.is_file() else "")
        result["error"] = bmc.get("line") or "backend found a counterexample"
        return result
    if status == "PROVEN":
        result["status"] = "PROVEN"
        return result
    if status not in ("PASS", "BMC_PASS"):
        result["status"] = "ERROR"
        result["error"] = f"backend returned unexpected status {status!r}"
        return result

    result["status"] = "BMC_PASS"
    if not want_induction:
        return result
    induction_cfg = cfg.get("induction") if isinstance(cfg.get("induction"), dict) else cfg
    induct = _phase({**cfg, **induction_cfg}, harness, prop, actual_depth,
                    actual_timeout, "induction", quiet)
    result["elapsed_s"] = round(float(result["elapsed_s"]) + float(induct.get("elapsed_s") or 0), 3)
    result["induction"] = {
        "result": str(induct.get("status") or "").lower(),
        "status": induct.get("status"),
        "line": induct.get("line"),
        "returncode": induct.get("returncode"),
        "timed_out": bool(induct.get("timed_out")),
        "elapsed_s": induct.get("elapsed_s"),
        "script": induct.get("script"),
        "log": induct.get("log"),
    }
    if induct.get("status") == "PROVEN":
        result["status"] = "PROVEN"
    elif induct.get("timed_out"):
        result["induction"]["reason"] = f"induction timed out after {actual_timeout}s"
    else:
        result["induction"]["reason"] = induct.get("line") or "induction did not prove the property"
    if not quiet:
        eprint(f"[{pid}] {result['status']} ({result['elapsed_s']}s)")
    return result
