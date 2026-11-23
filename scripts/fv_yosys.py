"""Yosys SAT backend for one rendered property.

The runner is deliberately explicit: every property gets its own wrapper top,
its own Yosys script, its own log, a BMC run and (for assertions) a separate
temporal-induction run.  Nothing about the proof is inferred from a combined
run, so a failure or timeout is attributable to exactly one property.

Yosys-specific facts encoded here (verified against Yosys 0.71):

* ``read_verilog -formal -sv`` then ``prep -top ... -flatten``.
* ``async2sync`` is required before ``sat`` for designs with ``$adff``.
* ``memory_map`` turns small inferred memories (FIFOs) into flops.
* ``sat -set-at N signal value`` is 1-based.
* ``-set-assumes`` enforces ``$assume`` cells; ``-prove-asserts`` checks
  ``$assert`` cells.
* ``-prove <signal> 0`` returns a model when the signal can be 1 (cover hit).
* Yosys exits 0 even on errors, so the log must be parsed.
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
    resolve_path,
    run_cmd,
    sanitize,
    which_yosys,
    write_json,
    write_text,
)
from fv_render import RENDER_SCHEMA_VERSION

GOOD_ASSERT = {"PROVEN", "BMC_PASS"}
GOOD_COVER = {"COVERED"}
GOOD_ASSUME = {"ASSUMED"}

# A cover/assume "not found" in BMC is bounded absence of witness, not a proof
# of unreachability.  The backend must say PROVEN_UNREACHABLE explicitly.
COVER_NOT_REACHED = "NOT_REACHED_WITHIN_BOUND"


def _qpath(path: Path | str) -> str:
    s = str(path)
    return f'"{s}"' if any(c.isspace() for c in s) else s


def _as_path(root: Path, value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def load_harness(manifest: dict[str, Any], harness_path: Path | None = None) -> dict[str, Any]:
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    path = harness_path or (paths["sva"] / "generated" / "fv_manifest.json")
    if not path.is_file():
        raise FvError(
            f"rendered harness not found: {path}\n"
            "run `fv.py render --design ...` first"
        )
    try:
        harness = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise FvError(f"invalid harness JSON in {path}: {exc}")
    if harness.get("schema_version") != RENDER_SCHEMA_VERSION:
        raise FvError(
            f"{path}: unsupported harness schema {harness.get('schema_version')!r}; "
            "re-run `fv.py render`"
        )
    harness["_path"] = str(path.resolve())
    harness["_root"] = str(project_root(manifest))
    harness["_paths"] = dict(manifest["_paths"])
    return harness


def harness_files(harness: dict[str, Any], prop: dict[str, Any]) -> list[Path]:
    root = Path(harness["_root"])
    files = harness.get("files") or {}
    out: list[Path] = []
    for raw in files.get("dut") or []:
        out.append(_as_path(root, raw))
    if files.get("assumptions"):
        out.append(_as_path(root, files["assumptions"]))
    out.append(_as_path(root, prop["monitor_file"]))
    out.append(_as_path(root, prop["top_file"]))
    return out


def _read_command(harness: dict[str, Any], prop: dict[str, Any]) -> str:
    files = harness.get("files") or {}
    parts = ["read_verilog -formal -sv"]
    for inc in files.get("include_dirs") or []:
        parts.append(f"-I{_qpath(inc)}")
    for d in files.get("defines") or []:
        parts.append(f"-D{d}")
    for f in harness_files(harness, prop):
        if not f.is_file():
            raise FvError(f"harness source not found: {f}")
        parts.append(_qpath(f))
    return " ".join(parts)


def _prep_commands(harness: dict[str, Any], prop: dict[str, Any]) -> list[str]:
    formal = harness.get("formal") or {}
    commands = [f"prep -top {prop['top']} -flatten", "async2sync"]
    if formal.get("memory_map", True):
        commands.append("memory_map")
    commands.append("opt -fast")
    return commands


def _reset_args(harness: dict[str, Any], depth: int) -> list[str]:
    design = harness.get("design") or {}
    reset = design.get("reset") or {}
    signal = reset.get("signal")
    active = reset.get("active")
    cycles = int(design.get("reset_cycles") or reset.get("cycles") or 0)
    if not signal:
        raise FvError("harness has no reset signal")
    if cycles < 2:
        raise FvError(f"reset_cycles={cycles} is too small; need at least 2")
    if depth <= cycles:
        raise FvError(f"depth {depth} must be greater than reset_cycles {cycles}")
    active_value = 0 if active == "low" else 1
    inactive_value = 1 - active_value
    args: list[str] = []
    for t in range(1, depth + 1):
        value = active_value if t <= cycles else inactive_value
        args.extend(["-set-at", str(t), signal, str(value)])
    return args


def _start_args(depth: int) -> list[str]:
    args: list[str] = ["-set-at", "1", "fv_start", "0"]
    for t in range(2, depth + 1):
        args.extend(["-set-at", str(t), "fv_start", "1"])
    return args


def _start_args_induction(depth: int) -> list[str]:
    args: list[str] = []
    for t in range(1, depth + 1):
        args.extend(["-set-at", str(t), "fv_start", "1"])
    return args


def _show_args(harness: dict[str, Any], prop: dict[str, Any]) -> list[str]:
    design = harness.get("design") or {}
    # Wrapper inputs are the DUT inputs plus fv_start.
    args: list[str] = []
    for name in design.get("inputs") or []:
        args.extend(["-show", name])
    args.extend(["-show", "fv_start"])
    for name in design.get("outputs") or []:
        args.extend(["-show", f"u_dut.{name}"])
    if prop.get("cover_signal"):
        args.extend(["-show", prop["cover_signal"]])
    return args


def _sat_line(harness: dict[str, Any], prop: dict[str, Any], depth: int,
              induction: bool = False) -> str:
    args = ["sat", "-seq", str(depth)]
    if induction:
        skip = int(prop.get("skip") or 1)
        args.extend(["-tempinduct"])
        if skip > 0:
            args.extend(["-tempinduct-skip", str(skip)])
        args.extend(_start_args_induction(depth))
    else:
        args.extend(_reset_args(harness, depth))
        args.extend(_start_args(depth))
    if (harness.get("files") or {}).get("assumptions"):
        args.append("-set-assumes")
    if prop.get("mode") in ("cover", "assume"):
        args.extend(["-prove", prop.get("cover_signal") or "fv_cover", "0"])
        if not induction:
            args.extend(_show_args(harness, prop))
    else:
        args.append("-prove-asserts")
        if not induction:
            args.extend(_show_args(harness, prop))
    return " ".join(args)


def parse_sat(output: str) -> dict[str, Any]:
    """Classify a Yosys SAT/induction log without trusting the exit code."""
    for line in output.splitlines():
        m = re.match(r"^\s*ERROR:\s*(.*)$", line)
        if m:
            return {"result": "error", "line": line.strip(), "detail": m.group(1)}
    if "Induction step proven: SUCCESS!" in output:
        return {"result": "proven", "line": "Induction step proven: SUCCESS!"}
    if "Induction step failed:" in output:
        line = next((x.strip() for x in output.splitlines() if "Induction step failed:" in x), "")
        return {"result": "induction_fail", "line": line}
    if "SAT proof finished - no model found: SUCCESS!" in output:
        return {"result": "pass", "line": "SAT proof finished - no model found: SUCCESS!"}
    if "SAT temporal induction proof finished - model found for base case: FAIL!" in output:
        return {"result": "induction_fail",
                "line": "SAT temporal induction proof finished - model found for base case: FAIL!"}
    if "SAT proof finished - model found: FAIL!" in output:
        return {"result": "fail", "line": "SAT proof finished - model found: FAIL!"}
    if "SAT solving finished - model found: FAIL!" in output:
        return {"result": "fail", "line": "SAT solving finished - model found: FAIL!"}
    if "no model found: SUCCESS!" in output:
        return {"result": "pass", "line": next(
            x.strip() for x in output.splitlines() if "no model found: SUCCESS!" in x)}
    if "model found: FAIL!" in output:
        return {"result": "fail", "line": next(
            x.strip() for x in output.splitlines() if "model found: FAIL!" in x)}
    return {"result": "unknown", "line": ""}


def parse_counterexample(output: str, limit: int = 160) -> list[str]:
    rows: list[str] = []
    for line in output.splitlines():
        if re.match(r"^\s*\d+\s+\\", line):
            rows.append(line.rstrip())
    return rows[-limit:]


def _run_yosys(script: str, script_path: Path, log_path: Path, cwd: Path,
               timeout_s: int) -> dict[str, Any]:
    write_text(script_path, script + "\n")
    exe = which_yosys()
    res = run_cmd([exe, "-s", str(script_path)], cwd=cwd, timeout_s=timeout_s)
    write_text(log_path, res.get("output") or "")
    res["script"] = str(script_path)
    res["log"] = str(log_path)
    return res


def run_property(harness: dict[str, Any], prop: dict[str, Any],
                 depth: int | None = None, timeout_s: int | None = None,
                 induction: bool | None = None, quiet: bool = False) -> dict[str, Any]:
    root = Path(harness["_root"])
    design = harness.get("design") or {}
    formal = harness.get("formal") or {}
    pid = str(prop["id"])
    file_san = str(prop.get("file_san") or sanitize(pid).lower())
    paths = harness.get("_paths") or {}
    build = Path(paths.get("build") or (root / "formal" / "build")) / "sat" / file_san
    reports = Path(paths.get("reports") or (root / "formal" / "reports"))
    logs = reports / "logs"
    build.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    actual_depth = int(depth or prop.get("depth") or formal.get("depth") or 12)
    reset_cycles = int(design.get("reset_cycles") or (design.get("reset") or {}).get("cycles") or 0)
    if actual_depth <= reset_cycles:
        raise FvError(
            f"property {pid}: depth {actual_depth} must be greater than "
            f"reset_cycles {reset_cycles}"
        )
    actual_timeout = int(timeout_s or formal.get("timeout_s") or 300)
    want_induction = bool(formal.get("induction", True) if induction is None else induction)
    want_induction = want_induction and bool(prop.get("induction", True))

    read = _read_command(harness, prop)
    prep = "\n".join(_prep_commands(harness, prop))
    bmc_script = read + "\n" + prep + "\n" + _sat_line(harness, prop, actual_depth, False) + "\n"
    bmc_script_path = build / "bmc.ys"
    bmc_log_path = logs / f"{file_san}.bmc.log"
    if not quiet:
        eprint(f"[{pid}] BMC depth={actual_depth} timeout={actual_timeout}s")
    bmc = _run_yosys(bmc_script, bmc_script_path, bmc_log_path, root, actual_timeout)
    bmc_parsed = parse_sat(bmc.get("output") or "")
    result: dict[str, Any] = {
        "id": pid,
        "kind": prop.get("kind"),
        "role": prop.get("role"),
        "mode": prop.get("mode"),
        "depth": actual_depth,
        "reset_cycles": reset_cycles,
        "timeout_s": actual_timeout,
        "bmc": {
            "result": bmc_parsed["result"],
            "line": bmc_parsed.get("line", ""),
            "returncode": bmc.get("returncode"),
            "timed_out": bool(bmc.get("timed_out")),
            "elapsed_s": bmc.get("elapsed_s"),
            "script": rel_to(root, Path(bmc["script"])),
            "log": rel_to(root, Path(bmc["log"])),
        },
        "induction": None,
        "counterexample": [],
        "status": "ERROR",
        "error": "",
        "elapsed_s": bmc.get("elapsed_s") or 0.0,
    }

    if bmc.get("timed_out"):
        result["status"] = "TIMEOUT"
        result["error"] = f"BMC timed out after {actual_timeout}s"
        result["counterexample"] = parse_counterexample(bmc.get("output") or "")
        return result
    if bmc_parsed["result"] == "error":
        result["status"] = "ERROR"
        result["error"] = bmc_parsed.get("detail") or bmc_parsed.get("line") or "Yosys error"
        return result
    if bmc_parsed["result"] == "unknown":
        result["status"] = "ERROR"
        tail = "\n".join((bmc.get("output") or "").splitlines()[-12:])
        result["error"] = "no SAT result line in the Yosys log\n" + tail
        return result

    if prop.get("mode") in ("cover", "assume"):
        reachable = bmc_parsed["result"] == "fail"
        result["counterexample"] = parse_counterexample(bmc.get("output") or "")
        if prop.get("mode") == "cover":
            result["status"] = "COVERED" if reachable else COVER_NOT_REACHED
        else:
            result["status"] = "ASSUMED" if reachable else "VACUOUS"
        result["status_reason"] = (
            "Witness found within bound." if reachable else
            "No witness found within bound; this is not a proof of unreachability.")
        if not quiet:
            eprint(f"[{pid}] {result['status']} ({result['elapsed_s']}s)")
        return result

    # Assertion.
    if bmc_parsed["result"] == "fail":
        result["status"] = "FAIL"
        result["counterexample"] = parse_counterexample(bmc.get("output") or "")
        result["error"] = bmc_parsed.get("line") or "BMC found a counterexample"
        if not quiet:
            eprint(f"[{pid}] FAIL ({result['elapsed_s']}s)")
        return result

    # BMC passed.
    if not want_induction:
        result["status"] = "BMC_PASS"
        if not quiet:
            eprint(f"[{pid}] BMC_PASS ({result['elapsed_s']}s)")
        return result
    skip = int(prop.get("skip") or 1)
    if skip >= actual_depth:
        result["status"] = "BMC_PASS"
        result["induction"] = {
            "result": "skipped",
            "reason": f"induction skip {skip} >= depth {actual_depth}",
        }
        if not quiet:
            eprint(f"[{pid}] BMC_PASS (induction skipped) ({result['elapsed_s']}s)")
        return result

    induct_script = (
        read + "\n" + prep + "\n"
        + _sat_line(harness, prop, actual_depth, True) + "\n"
    )
    induct_script_path = build / "induct.ys"
    induct_log_path = logs / f"{file_san}.induct.log"
    if not quiet:
        eprint(f"[{pid}] induction skip={skip} timeout={actual_timeout}s")
    induct = _run_yosys(induct_script, induct_script_path, induct_log_path, root, actual_timeout)
    induct_parsed = parse_sat(induct.get("output") or "")
    result["elapsed_s"] = round(float(result["elapsed_s"]) + float(induct.get("elapsed_s") or 0), 3)
    result["induction"] = {
        "result": induct_parsed["result"],
        "line": induct_parsed.get("line", ""),
        "skip": skip,
        "returncode": induct.get("returncode"),
        "timed_out": bool(induct.get("timed_out")),
        "elapsed_s": induct.get("elapsed_s"),
        "script": rel_to(root, Path(induct["script"])),
        "log": rel_to(root, Path(induct["log"])),
    }
    if induct.get("timed_out"):
        result["status"] = "BMC_PASS"
        result["induction"]["reason"] = f"induction timed out after {actual_timeout}s"
    elif induct_parsed["result"] == "proven":
        result["status"] = "PROVEN"
    elif induct_parsed["result"] == "error":
        result["status"] = "BMC_PASS"
        result["induction"]["reason"] = induct_parsed.get("detail") or "Yosys error"
    elif induct_parsed["result"] == "unknown":
        result["status"] = "BMC_PASS"
        result["induction"]["reason"] = "no induction result line in the Yosys log"
    else:
        result["status"] = "BMC_PASS"
        result["induction"]["reason"] = induct_parsed.get("line") or "induction did not prove the property"
    if not quiet:
        eprint(f"[{pid}] {result['status']} ({result['elapsed_s']}s)")
    return result


if __name__ == "__main__":
    import argparse
    from fv_common import load_manifest

    ap = argparse.ArgumentParser(description="run one rendered property with Yosys")
    ap.add_argument("--design", required=True)
    ap.add_argument("--property", required=True)
    ap.add_argument("--harness")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--no-induction", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    m = load_manifest(args.design)
    h = load_harness(m, Path(args.harness) if args.harness else None)
    prop = next((p for p in h.get("properties") or [] if p.get("id") == args.property), None)
    if not prop:
        raise FvError(f"property {args.property!r} is not in {h['_path']}")
    res = run_property(h, prop, args.depth, args.timeout,
                       False if args.no_induction else None)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{res['id']}: {res['status']}")
        if res.get("error"):
            print(res["error"])
    good = {"PROVEN", "BMC_PASS", "COVERED", "ASSUMED", "NOT_REACHED_WITHIN_BOUND"}
    raise SystemExit(0 if res["status"] in good else 1)
