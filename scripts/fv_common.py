"""Shared helpers of the formal-sva skill.

Standard library only (Python >= 3.9) so the skill runs in a plain checkout,
inside Q3TUI's Python environment, or in CI without extra packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL_VERSION = "2.0.0"

DEFAULT_PATHS = {
    "bundle": "formal/schemas/input_bundle.json",
    "interface": "formal/schemas/design_interface.json",
    "contract": "formal/schemas/formal_contract.json",
    "requirements": "formal/spec/requirements.md",
    "extraction": "formal/spec/requirements.json",
    "questions": "formal/reports/questions.json",
    "review": "formal/reports/sva_review.json",
    "sva": "formal/src",
    "build": "formal/build",
    "reports": "formal/reports",
    "results": "formal/results/formal_results.json",
    "tools": "formal/tools.json",
    "rules": "formal/rules",
    "state": "formal/state/pipeline.json",
}

DEFAULT_FORMAL = {
    # "auto" asks formal/tools.json; the tools file defaults to SymbiYosys.
    "backend": "auto",
    "depth": 12,
    "timeout_s": 300,
    "jobs": 1,
    "induction": True,
    "reset_cycles": 2,
}


class FvError(RuntimeError):
    """A user-facing error: printed without a traceback."""


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def read_json(path: Path) -> Any:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FvError(f"file not found: {path}")
    except json.JSONDecodeError as exc:
        raise FvError(f"invalid JSON in {path}: {exc}")


def write_json(path: Path, obj: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


def write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def sha256_paths(paths: Iterable[Path]) -> str:
    """Stable hash of a set of files (path + content), independent of listing order."""
    h = hashlib.sha256()
    for path in sorted({Path(p).resolve() for p in paths}, key=lambda p: str(p)):
        h.update(str(path).encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(path).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def resolve_path(root: Path, value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (Path(root) / p)


def rel_to(root: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(Path(path).resolve())


def sanitize(name: str, upper: bool = False) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
    if not s:
        s = "x"
    if s[0].isdigit():
        s = "n_" + s
    return s.upper() if upper else s


def normalize_text(text: str) -> str:
    """Whitespace/markdown-insensitive text for evidence matching."""
    s = text.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace("`", "")
    s = re.sub(r"[*_#>]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_'\\-]+", text)


def short(text: str, limit: int = 72) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def which_yosys() -> str:
    exe = shutil.which("yosys")
    if not exe:
        raise FvError(
            "yosys was not found on PATH. Install OSS CAD Suite / Yosys, or select "
            "another backend in formal/tools.json."
        )
    return exe


def yosys_version() -> str:
    exe = which_yosys()
    try:
        out = subprocess.run([exe, "-V"], text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=20).stdout
    except Exception:
        return "yosys (unknown version)"
    return " ".join(out.split())[:200]


def run_cmd(argv: list[str], cwd: Path, timeout_s: int | float,
            env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a command, capturing stdout+stderr. Never raises on a non-zero exit."""
    started = time.time()
    full_env = os.environ.copy()
    if env:
        full_env.update({str(k): str(v) for k, v in env.items()})
    try:
        proc = subprocess.run(
            [str(x) for x in argv],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=float(timeout_s),
            env=full_env,
        )
        output = proc.stdout or ""
        return {
            "argv": [str(x) for x in argv],
            "returncode": proc.returncode,
            "output": output,
            "timed_out": False,
            "elapsed_s": round(time.time() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {
            "argv": [str(x) for x in argv],
            "returncode": None,
            "output": out,
            "timed_out": True,
            "elapsed_s": round(time.time() - started, 3),
        }


def load_manifest(design_arg: str | Path) -> dict[str, Any]:
    """Load formal/design.json (or a project directory) and resolve its paths.

    Returns a dict with the raw manifest plus:
      _path, _root, _paths (resolved), _formal (defaults filled)
    """
    p = Path(design_arg).expanduser()
    if p.is_dir():
        candidates = [p / "formal" / "design.json", p / "design.json"]
        path = next((c for c in candidates if c.is_file()), None)
        if not path:
            raise FvError(
                f"no formal/design.json under {p}; run `fv.py init --project {p}` first"
            )
    else:
        path = p
    if not path.is_file():
        raise FvError(f"design manifest not found: {path}")
    data = read_json(path)
    if not isinstance(data, dict):
        raise FvError(f"{path}: the design manifest must be a JSON object")
    if path.parent.name == "formal":
        root = path.parent.parent.resolve()
    else:
        root = path.parent.resolve()
    paths = dict(DEFAULT_PATHS)
    paths.update(data.get("paths") or {})
    resolved = {k: resolve_path(root, v) for k, v in paths.items()}
    formal = dict(DEFAULT_FORMAL)
    formal.update(data.get("formal") or {})
    data = dict(data)
    data["_path"] = str(path.resolve())
    data["_root"] = str(root.resolve())
    data["_paths"] = {k: str(v) for k, v in resolved.items()}
    data["_formal"] = formal
    return data


def design_paths(manifest: dict[str, Any]) -> dict[str, Path]:
    return {k: Path(v) for k, v in manifest["_paths"].items()}


def project_root(manifest: dict[str, Any]) -> Path:
    return Path(manifest["_root"])


# -- file lists ----------------------------------------------------------------------------------


def _resolve_member(value: str, bases: list[Path]) -> Path:
    v = Path(value)
    if v.is_absolute():
        return v
    for base in bases:
        candidate = (base / v).resolve()
        if candidate.exists():
            return candidate
    return (bases[0] / v).resolve()


def parse_filelist(filelist: Path, root: Path) -> dict[str, Any]:
    """Expand a Verilog file list, resolving paths against several plausible bases.

    Q3TUI's `src/rtl/filelist.f` contains `rtl/...` (relative to `src/`), while
    ordinary projects use paths relative to the file list itself. Both work.
    """
    filelist = Path(filelist)
    if not filelist.is_file():
        raise FvError(f"RTL file list not found: {filelist}")
    bases = [filelist.parent, filelist.parent.parent, Path(root)]
    sources: list[Path] = []
    incdirs: list[Path] = []
    defines: list[str] = []
    missing: list[str] = []
    seen_lists: set[Path] = set()

    def add_source(value: str) -> None:
        p = _resolve_member(value, bases)
        if p.exists():
            if p not in sources:
                sources.append(p)
        else:
            missing.append(value)

    def add_incdir(value: str) -> None:
        p = _resolve_member(value, bases)
        if p not in incdirs:
            incdirs.append(p)

    def parse_one(fl: Path) -> None:
        fl = fl.resolve()
        if fl in seen_lists:
            return
        seen_lists.add(fl)
        try:
            lines = fl.read_text().splitlines()
        except OSError as exc:
            raise FvError(f"cannot read file list {fl}: {exc}")
        local_bases = [fl.parent, fl.parent.parent, Path(root)]
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            line = re.sub(r"//.*$", "", line).strip()
            if not line:
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                tokens = line.split()
            i = 0
            while i < len(tokens):
                tok = tokens[i]
                if tok in ("-f", "-F"):
                    i += 1
                    if i < len(tokens):
                        nested = _resolve_member(tokens[i], local_bases)
                        if nested.exists():
                            parse_one(nested)
                        else:
                            missing.append(tokens[i])
                elif tok.startswith("-f") and len(tok) > 2:
                    nested = _resolve_member(tok[2:], local_bases)
                    if nested.exists():
                        parse_one(nested)
                    else:
                        missing.append(tok[2:])
                elif tok.startswith("+incdir+"):
                    for part in tok[len("+incdir+"):].split("+"):
                        if part:
                            p = _resolve_member(part, local_bases)
                            if p not in incdirs:
                                incdirs.append(p)
                elif tok.startswith("-I"):
                    part = tok[2:] or ""
                    if not part:
                        i += 1
                        part = tokens[i] if i < len(tokens) else ""
                    if part:
                        p = _resolve_member(part, local_bases)
                        if p not in incdirs:
                            incdirs.append(p)
                elif tok.startswith("+define+"):
                    defines.extend(x for x in tok[len("+define+"):].split("+") if x)
                elif tok.startswith("-D"):
                    part = tok[2:]
                    if not part:
                        i += 1
                        part = tokens[i] if i < len(tokens) else ""
                    if part:
                        defines.append(part)
                elif tok.startswith("-"):
                    # -y, -v, -sverilog, ... are not needed for the formal read;
                    # a project that needs them can put absolute sources in design.json.
                    pass
                elif tok.startswith("+"):
                    pass
                else:
                    p = _resolve_member(tok, local_bases)
                    if p.exists():
                        if p not in sources:
                            sources.append(p)
                    else:
                        missing.append(tok)
                i += 1

    parse_one(filelist)
    return {
        "filelist": str(filelist.resolve()),
        "sources": [str(p) for p in sources],
        "include_dirs": [str(p) for p in incdirs],
        "defines": list(dict.fromkeys(defines)),
        "missing": missing,
    }


def source_files(manifest: dict[str, Any]) -> dict[str, Any]:
    """The RTL source set of a manifest (file list and/or explicit sources)."""
    root = project_root(manifest)
    rtl = manifest.get("rtl") or {}
    sources: list[Path] = []
    incdirs: list[Path] = []
    defines: list[str] = []
    missing: list[str] = []
    if rtl.get("filelist"):
        fl = parse_filelist(resolve_path(root, rtl["filelist"]), root)
        sources.extend(Path(p) for p in fl["sources"])
        incdirs.extend(Path(p) for p in fl["include_dirs"])
        defines.extend(fl["defines"])
        missing.extend(fl["missing"])
    for raw in rtl.get("sources") or []:
        p = resolve_path(root, raw)
        if p.is_file():
            if p not in sources:
                sources.append(p)
        else:
            missing.append(str(raw))
    for raw in rtl.get("include_dirs") or []:
        p = resolve_path(root, raw)
        if p not in incdirs:
            incdirs.append(p)
    defines.extend(rtl.get("defines") or [])
    # Stable, de-duplicated.
    sources = list(dict.fromkeys(p.resolve() for p in sources))
    incdirs = list(dict.fromkeys(p.resolve() for p in incdirs))
    defines = list(dict.fromkeys(defines))
    return {
        "sources": sources,
        "include_dirs": incdirs,
        "defines": defines,
        "missing": missing,
    }


# -- statuses / regression -----------------------------------------------------------------------

STATUS_ORDER = {
    # Strongest evidence first.  A role-aware comparison in fv_regress uses the
    # obligation kind as well as this rank: an assertion changed into a cover is
    # never "unchanged", even when both statuses rank as a success.
    "PROVEN": 0,
    "PARTIALLY_PROVEN": 1,
    "BMC_PASS": 2,
    "INCONCLUSIVE": 2,
    "COVERED": 0,
    "PROVEN_UNREACHABLE": 0,
    "NOT_REACHED_WITHIN_BOUND": 1,
    "UNREACHABLE": 1,
    "VACUOUS": 1,
    "ASSUMED": 0,
    "ASSUMED_UNCHECKED": 1,
    "INCOMPLETE_OBLIGATION": 2,
    "NEW": 0,
    "NOT_FORMALIZABLE": 2,
    "OUT_OF_SCOPE": 3,
    "NOT_FORMALIZED": 3,
    "OK": 0,
    "WARN": 1,
    "GAP": 2,
    "UNKNOWN": 3,
    "MISSING": 3,
    "FAIL": 3,
    "ERROR": 4,
    "TIMEOUT": 4,
    "SKIPPED": 2,
}

BAD_STATUSES = {"FAIL", "ERROR", "TIMEOUT", "MISSING", "INCOMPLETE_OBLIGATION"}

# Statuses that mean "this obligation is discharged with unbounded evidence".
PROOF_STATUSES = {"PROVEN", "PROVEN_UNREACHABLE"}
# Statuses that are useful evidence but only within the configured bound.
BOUNDED_STATUSES = {"BMC_PASS", "COVERED", "NOT_REACHED_WITHIN_BOUND",
                    "UNREACHABLE", "ASSUMED", "INCONCLUSIVE"}
# A cover that was not reached inside the bound is not the same as a proof that
# it is unreachable.  Keep both names so reports cannot collapse them.
NOT_REACHED = {"NOT_REACHED_WITHIN_BOUND", "UNREACHABLE"}

CLAIM_LEVELS = {
    "mechanically_validated": "Checked by a deterministic script (hash, schema, span, type or structural comparison).",
    "model_interpretation": "An LLM translated specification text into a property; a human should review the meaning.",
    "engineering_decision": "A bound, assumption, abstraction or scope choice made by an engineer, not stated by the spec.",
    "solver_result": "Produced by the configured formal backend for the recorded artifact and configuration.",
}


def status_rank(status: str) -> int:
    return STATUS_ORDER.get(status, 2)


def is_proof(status: str | None) -> bool:
    return str(status) in PROOF_STATUSES


def is_bounded(status: str | None) -> bool:
    return str(status) in BOUNDED_STATUSES
