"""Contract validation: schema, traceability, scope, expressions.

This is the gate that keeps the LLM honest.  It refuses a contract that:
  * invents a requirement, feature or signal;
  * quotes evidence that is not in the spec;
  * leaves a requirement without a disposition;
  * uses temporal SVA syntax instead of the small, portable expression language;
  * proves a different parameter configuration than the one that was rendered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fv_common import (
    FvError,
    normalize_text,
    project_root,
    read_json,
    resolve_path,
    sha256_file,
    sha256_paths,
    source_files,
    words,
)
from fv_design import const_eval

FUNCS = {"prev", "rose", "fell", "stable", "onehot", "onehot0"}
HISTORY_FUNCS = {"prev", "rose", "fell", "stable"}
DISPOSITIONS = {"formalized", "partially_formalized", "assumption",
                "not_formalizable", "out_of_scope"}
KINDS = {"always", "next", "response", "stability", "reset_value", "cover"}
ROLES = {"assert", "assume", "cover"}
OBLIGATIONS = {"safety", "reachability", "bounded_response", "environment",
               "structural", "liveness"}
REQUIREMENT_OWNERS = {"dut", "environment", "both", "none"}
PROPERTY_OWNERS = {"dut", "environment", "verification"}
DELAY_SOURCES = {"spec", "interface", "project", "engineering_decision"}

DESIGN_KEYS = {"name", "top", "clock", "reset", "parameters", "notes"}
CLOCK_KEYS = {"signal", "edge"}
RESET_KEYS = {"signal", "active", "kind", "cycles"}
REQUIREMENT_KEYS = {"id", "feature", "disposition", "reason", "limitations",
                    "evidence", "properties", "owner", "obligations", "notes"}
EVIDENCE_KEYS = {"section", "quote", "source_sha256", "source_lines"}
PROPERTY_KEYS = {
    "id", "req", "feature", "title", "kind", "role", "when", "expr",
    "trigger", "response", "min_delay", "max_delay", "signal", "cycles",
    "disable_reset", "depth", "severity", "tags", "evidence", "notes",
    "obligation", "owner", "intent", "proof", "configurations",
    "delay_source", "induction_helpers", "limitations",
}
INTENT_KEYS = {"type", "owner", "sampling", "reset_policy", "antecedent",
               "consequent", "delay", "bound_source", "bound_justification"}
PROOF_KEYS = {"method", "helpers", "soundness", "backend", "notes"}
SCOPE_KEYS = {"configuration", "configurations", "proven", "not_proven",
              "reset_plan", "exclusions", "notes"}

TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<escaped>\\[^\s]+)
    | (?P<based>\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ?_]+)
    | (?P<unbased>'[01xXzZ])
    | (?P<num>\d[\d_]*)
    | (?P<id>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<op>===|!==|==|!=|<=|>=|&&|\|\||<<|>>|\*\*|[-+*/%&|^~!<>?:(),\[\]{}])
    | (?P<other>.)
    """,
    re.X,
)

SIMPLE_ARG_RE = re.compile(r"^(\\?[A-Za-z_][A-Za-z0-9_$]*)((?:\s*\[[^\]]+\])*)$")
SELECT_RE = re.compile(r"\[([^\]]+)\]")


@dataclass
class Issue:
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} [{self.where}]: {self.message}"


@dataclass
class Validation:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, where: str, message: str) -> None:
        self.errors.append(Issue(code, where, message))

    def warn(self, code: str, where: str, message: str) -> None:
        self.warnings.append(Issue(code, where, message))


def tokenize(expr: str) -> list[tuple[str, str]]:
    out = []
    for m in TOKEN_RE.finditer(expr):
        kind = m.lastgroup
        val = m.group(0)
        if kind == "ws":
            continue
        out.append((kind, val))
    return out


def _check_keys(obj: dict[str, Any], allowed: set[str], where: str,
                result: Validation, label: str) -> None:
    """Reject unknown keys instead of silently ignoring a typo or a new obligation."""
    for key in obj:
        if key not in allowed:
            result.error("SCHEMA_KEY", where,
                         f"unknown {label} key {key!r}; allowed keys are "
                         + ", ".join(sorted(allowed)))


def _norm_expr(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def property_obligation(prop: dict[str, Any]) -> str:
    """The verification obligation a property discharges.

    The mapping is deliberately explicit.  A cover is reachability evidence, an
    assumption is an environment obligation, and only an assertion can
    discharge a safety or bounded-response obligation.
    """
    explicit = str(prop.get("obligation") or "")
    if explicit:
        return explicit
    role = str(prop.get("role") or "")
    kind = str(prop.get("kind") or "")
    if role == "assume":
        return "environment"
    if role == "cover" or kind == "cover":
        return "reachability"
    if kind == "response":
        return "bounded_response"
    if kind in ("always", "next", "stability", "reset_value"):
        return "safety"
    return "unknown"


def property_owner(prop: dict[str, Any]) -> str:
    explicit = str(prop.get("owner") or "")
    if explicit:
        return explicit
    return "environment" if prop.get("role") == "assume" else (
        "verification" if prop.get("role") == "cover" else "dut")


def _section_span(spec_text: str, section: str) -> tuple[int, int, bool]:
    """Line span [start, end) for an evidence section.

    A section is either an explicit line range (``L12`` or ``L12-L18``) or a
    Markdown heading.  Headings extend to the next heading of the same or a
    higher level.  A section that cannot be located returns ``(0, 0, False)``:
    the caller must not silently fall back to the whole document.
    """
    text = str(section or "").strip()
    lines = spec_text.splitlines()
    m = re.fullmatch(r"L?(\d+)(?:\s*[-–]\s*L?(\d+))?", text, re.I)
    if m:
        start = max(1, int(m.group(1)))
        end = int(m.group(2) or m.group(1))
        if end < start:
            start, end = end, start
        if start > len(lines):
            return 0, 0, False
        return start - 1, min(end, len(lines)), True
    if not text:
        return 0, 0, False
    needle = re.sub(r"\s+", " ", text).strip().lower()
    needle_key = re.sub(r"[^a-z0-9]+", " ", needle).strip()
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    for i, line in enumerate(lines):
        hm = heading_re.match(line.strip())
        if not hm:
            continue
        title = re.sub(r"\s+", " ", hm.group(2)).strip().lower()
        title_key = re.sub(r"[^a-z0-9]+", " ", title).strip()
        if (needle == title or needle in title or title in needle
                or needle_key == title_key or needle_key in title_key
                or title_key in needle_key):
            level = len(hm.group(1))
            end = len(lines)
            for j in range(i + 1, len(lines)):
                nxt = heading_re.match(lines[j].strip())
                if nxt and len(nxt.group(1)) <= level:
                    end = j
                    break
            return i, end, True
    return 0, 0, False


def _balance(expr: str, result: Validation, where: str) -> None:
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for kind, val in tokenize(expr):
        if val in "([{":
            stack.append(val)
        elif val in ")]}":
            if not stack or stack.pop() != pairs[val]:
                result.error("EXPR_BALANCE", where, f"unbalanced '{val}' in {expr!r}")
                return
    if stack:
        result.error("EXPR_BALANCE", where, f"unclosed '{stack[-1]}' in {expr!r}")


CALL_RE = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def find_calls(expr: str) -> list[tuple[str, str, int, int]]:
    """Every portable `func(...)` call: (name, raw argument, start, end).

    The identifier boundary is important: after an SVA rewrite, ``$rose(x)``
    contains the text ``rose(``.  Without the boundary check, ``map_calls``
    would rewrite its own output forever.
    """
    out: list[tuple[str, str, int, int]] = []
    for m in CALL_RE.finditer(expr):
        name = m.group(1)
        if name not in FUNCS:
            continue
        ident_start = m.start()
        paren = m.end() - 1
        depth = 1
        j = paren + 1
        while j < len(expr) and depth:
            if expr[j] == "(":
                depth += 1
            elif expr[j] == ")":
                depth -= 1
            j += 1
        if depth:
            continue
        out.append((name, expr[paren + 1:j - 1], ident_start, j))
    return out


def map_calls(expr: str, mapper) -> str:
    """Replace function calls, innermost/right-most first, with mapper(name, raw_arg, mapped_arg)."""
    calls = find_calls(expr)
    if not calls:
        return expr
    # Replace the right-most call first (an inner call always starts after its
    # enclosing call), then re-scan the whole expression so offsets of the
    # remaining calls are recomputed after the replacement changed lengths.
    name, arg, start, end = max(calls, key=lambda c: c[2])
    mapped_arg = map_calls(arg, mapper)
    replacement = mapper(name, arg, mapped_arg)
    return map_calls(expr[:start] + replacement + expr[end:], mapper)


def expr_identifiers(expr: str) -> list[str]:
    out = []
    for kind, val in tokenize(expr):
        if kind in ("id", "escaped"):
            out.append(val)
    return out


def _dim_bounds(dim: str, env: dict[str, int]) -> tuple[int, int] | None:
    body = str(dim).strip()
    if not (body.startswith("[") and body.endswith("]")):
        return None
    body = body[1:-1]
    if ":" not in body:
        return None
    msb_s, lsb_s = body.split(":", 1)
    msb = const_eval(msb_s, env)
    lsb = const_eval(lsb_s, env)
    if msb is None or lsb is None:
        return None
    return min(msb, lsb), max(msb, lsb)


def _dim_width(dim: str, env: dict[str, int]) -> int | None:
    bounds = _dim_bounds(dim, env)
    return (bounds[1] - bounds[0] + 1) if bounds else None


def arg_type(arg: str, interface: dict[str, Any], result: Validation,
             where: str) -> dict[str, Any] | None:
    """Type of a history-function argument (full signal or constant slice)."""
    arg = arg.strip()
    m = SIMPLE_ARG_RE.match(arg)
    if not m:
        result.error("EXPR_ARG", where,
                     f"history function argument must be a signal or a slice, got {arg!r}")
        return None
    name, selectors_text = m.group(1), m.group(2)
    ports = {p["name"]: p for p in interface.get("ports") or []}
    port = ports.get(name)
    if not port:
        result.error("EXPR_SIGNAL", where, f"unknown signal {name!r}")
        return None
    selectors = SELECT_RE.findall(selectors_text)
    dims = list(port.get("dims") or [])
    env = {}
    env.update(interface.get("parameters") or {})
    env.update(interface.get("localparams") or {})
    d = 0
    for sel in selectors:
        if d >= len(dims):
            result.error("EXPR_SLICE", where, f"too many indices for {name!r}: {arg!r}")
            return None
        if ":" in sel:
            # Part-select of the current packed dimension.  The selected slice
            # remains packed, and later dimensions keep their own widths.
            msb_s, lsb_s = sel.split(":", 1)
            msb, lsb = const_eval(msb_s, env), const_eval(lsb_s, env)
            if msb is None or lsb is None:
                result.error("EXPR_SLICE", where,
                             f"part-select bounds must be constant: {arg!r}")
                return None
            bounds = _dim_bounds(dims[d], env)
            if bounds and not (bounds[0] <= min(msb, lsb) <= max(msb, lsb) <= bounds[1]):
                result.error("EXPR_RANGE", where,
                             f"part-select {sel} is outside the declared range "
                             f"{dims[d]} of {name!r}")
                return None
            width = abs(msb - lsb) + 1
            remaining = dims[d + 1:]
            for dim in remaining:
                part = _dim_width(dim, env)
                if part is None:
                    result.error("EXPR_SLICE", where,
                                 f"cannot resolve dimension {dim} of {name!r}")
                    return None
                width *= part
            return {"name": name, "arg": arg, "dims": remaining, "width": width,
                    "signed": bool(port.get("signed")),
                    "key": re.sub(r"\s+", "", arg)}
        bounds = _dim_bounds(dims[d], env)
        idx = const_eval(sel, env)
        if bounds and idx is not None and not (bounds[0] <= idx <= bounds[1]):
            result.error("EXPR_RANGE", where,
                         f"index {sel} is outside the declared range {dims[d]} of {name!r}")
            return None
        d += 1
    remaining = dims[d:]
    width = 1
    for dim in remaining:
        part = _dim_width(dim, env)
        if part is None:
            # An unresolved dimension is only safe when the port has a single
            # packed dimension and Yosys supplied its flat width.
            if len(remaining) == 1 and d == 0:
                width = int(port.get("width") or 1)
                continue
            result.error("EXPR_SLICE", where, f"cannot resolve dimension {dim} of {name!r}")
            return None
        width *= part
    return {"name": name, "arg": arg, "dims": remaining, "width": width,
            "signed": bool(port.get("signed")),
            "key": re.sub(r"\s+", "", arg)}


SELECTED_IDENT_RE = re.compile(
    r"(?<![A-Za-z0-9_$])([A-Za-z_][A-Za-z0-9_$]*)\s*((?:\[[^\]]+\])+)(?!\s*\()")


def _check_selectors(expr: str, interface: dict[str, Any], result: Validation,
                     where: str) -> None:
    """Validate index/part-select bounds and packed-dimension use outside history calls."""
    seen: set[str] = set()
    for m in SELECTED_IDENT_RE.finditer(expr):
        name = m.group(1)
        if name in FUNCS or name in seen:
            continue
        seen.add(name)
        arg_type(name + m.group(2), interface, result, where)


def validate_expr(expr: str, interface: dict[str, Any], result: Validation,
                  where: str, allow_history: bool = True) -> None:
    expr = (expr or "").strip()
    if not expr:
        result.error("EXPR_EMPTY", where, "expression is empty")
        return
    # Raw temporal syntax must not leak in: the contract is a data format, not
    # SVA text.  Check before tokenizing because these markers are built from
    # characters the expression grammar would otherwise accept.
    for marker in ("|->", "|=>", "##"):
        if marker in expr:
            result.error("EXPR_TEMPORAL", where,
                         f"raw temporal operator {marker!r} is not allowed; describe the "
                         "sequence with property kind/role fields and the portable expression subset")
            return
    if re.search(r"\[\s*[*=+-]\s*\d", expr):
        result.error("EXPR_TEMPORAL", where,
                     "SVA repetition syntax is not allowed; use property kind/role fields instead")
        return
    _balance(expr, result, where)
    ports = {p["name"]: p for p in interface.get("ports") or []}
    params = set((interface.get("parameters") or {})) | set((interface.get("localparams") or {}))
    for kind, val in tokenize(expr):
        if kind == "other":
            result.error("EXPR_TOKEN", where,
                         f"unsupported character {val!r} in {expr!r}; use the portable expression subset")
            return
        if kind == "based" and re.search(r"[xXzZ?]", val):
            result.error("EXPR_X", where, f"x/z literal {val!r} makes a property vacuous")
        if kind == "unbased" and re.search(r"[xXzZ]", val):
            result.error("EXPR_X", where, f"x/z literal {val!r} makes a property vacuous")
        if kind in ("id", "escaped"):
            if val in FUNCS:
                continue
            if val in ("and", "or", "not"):
                continue
            if val in ports or val in params:
                continue
            result.error("EXPR_SIGNAL", where,
                         f"unknown signal/parameter {val!r}; only top-level ports and resolved "
                         "parameters are in scope")
    calls = find_calls(expr)
    for name, arg, _s, _e in calls:
        if name not in FUNCS:
            result.error("EXPR_FUNC", where, f"function {name!r} is not allowed")
            continue
        if name in HISTORY_FUNCS:
            if not allow_history:
                result.error("EXPR_FUNC", where, f"{name}() is not allowed here")
            if not arg.strip():
                result.error("EXPR_ARG", where, f"{name}() needs an argument")
                continue
            nested = [c for c in find_calls(arg) if c[0] in HISTORY_FUNCS]
            if nested:
                result.error("EXPR_NESTED", where,
                             f"{name}() cannot contain another history function")
            info = arg_type(arg, interface, result, where)
            if info and name in ("rose", "fell") and info["width"] != 1:
                result.error("EXPR_WIDTH", where,
                             f"{name}() needs a 1-bit signal, got {info['width']} bits")
        elif name in ("onehot", "onehot0"):
            if not arg.strip():
                result.error("EXPR_ARG", where, f"{name}() needs an argument")
    _check_selectors(expr, interface, result, where)


def validate_contract(manifest: dict[str, Any], contract: dict[str, Any],
                      bundle: dict[str, Any], interface: dict[str, Any],
                      strict: bool = False) -> Validation:
    r = Validation()
    root = project_root(manifest)

    # -- top-level structure -------------------------------------------------------------------
    for key in ("schema_version", "design", "requirements", "properties", "scope"):
        if key not in contract:
            r.error("SCHEMA", "contract", f"missing required key {key!r}")
    if contract.get("schema_version") != "1.0":
        r.error("SCHEMA", "contract", "schema_version must be \"1.0\"")
    for key in ("design", "requirements", "properties", "scope"):
        if key not in contract:
            continue
        if key in ("design", "scope") and not isinstance(contract[key], dict):
            r.error("SCHEMA", "contract", f"{key!r} must be an object")
        if key in ("requirements", "properties") and not isinstance(contract[key], list):
            r.error("SCHEMA", "contract", f"{key!r} must be a list")
    if r.errors:
        return r

    _check_keys(contract, {"schema_version", "design", "requirements", "properties",
                           "scope", "metadata", "provenance", "auxiliary"},
                "contract", r, "contract")

    design = contract.get("design") or {}
    for key in ("name", "top", "clock", "reset", "parameters"):
        if key not in design:
            r.error("SCHEMA", "design", f"missing {key!r}")
    _check_keys(design, DESIGN_KEYS, "design", r, "design")
    _check_keys(design.get("clock") or {}, CLOCK_KEYS, "design.clock", r, "clock")
    _check_keys(design.get("reset") or {}, RESET_KEYS, "design.reset", r, "reset")
    reset_kind = str((design.get("reset") or {}).get("kind") or "unknown")
    if reset_kind == "unknown":
        r.error("DESIGN_RESET_KIND", "design.reset.kind",
                "reset kind must be 'async' or 'sync'; 'unknown' cannot be used to "
                "justify a reset model or a proof")
    reset_cycles = int((design.get("reset") or {}).get("cycles") or 0)
    if reset_cycles and reset_cycles < 2:
        r.error("DESIGN_RESET_CYCLES", "design.reset.cycles",
                "reset must be active for at least two cycles before monitors arm")

    # -- design / configuration ----------------------------------------------------------------
    bdesign = bundle.get("design") or {}
    if str(design.get("top")) != str(bdesign.get("top")):
        r.error("DESIGN_TOP", "design.top",
                f"contract top {design.get('top')!r} != design top {bdesign.get('top')!r}")
    if str(design.get("name")) != str(bdesign.get("name")):
        r.warn("DESIGN_NAME", "design.name",
               f"contract name {design.get('name')!r} != bundle name {bdesign.get('name')!r}")
    man_clock = (manifest.get("clock") or {})
    if man_clock.get("signal") and (design.get("clock") or {}).get("signal") != man_clock.get("signal"):
        r.error("DESIGN_CLOCK", "design.clock.signal",
                f"contract clock {(design.get('clock') or {}).get('signal')!r} != "
                f"manifest clock {man_clock.get('signal')!r}")
    man_reset = (manifest.get("reset") or {})
    if man_reset.get("signal") and (design.get("reset") or {}).get("signal") != man_reset.get("signal"):
        r.error("DESIGN_RESET", "design.reset.signal",
                f"contract reset {(design.get('reset') or {}).get('signal')!r} != "
                f"manifest reset {man_reset.get('signal')!r}")
    if man_reset.get("active") and (design.get("reset") or {}).get("active") != man_reset.get("active"):
        r.error("DESIGN_RESET", "design.reset.active",
                f"contract reset polarity {(design.get('reset') or {}).get('active')!r} != "
                f"manifest {man_reset.get('active')!r}")
    ports = {p["name"]: p for p in interface.get("ports") or []}
    clock_sig = (design.get("clock") or {}).get("signal")
    reset_sig = (design.get("reset") or {}).get("signal")
    if clock_sig and clock_sig not in ports:
        r.error("DESIGN_CLOCK", "design.clock.signal", f"clock {clock_sig!r} is not a top-level port")
    if reset_sig and reset_sig not in ports:
        r.error("DESIGN_RESET", "design.reset.signal", f"reset {reset_sig!r} is not a top-level port")

    expected_params = dict(interface.get("parameters") or {})
    got_params = design.get("parameters") or {}
    for name, value in expected_params.items():
        if name not in got_params:
            r.error("PARAM_MISSING", "design.parameters", f"missing parameter {name}")
        elif got_params[name] != value:
            r.error("PARAM_VALUE", f"design.parameters.{name}",
                    f"contract value {got_params[name]!r} != resolved {value!r}")
    for name in got_params:
        if name not in expected_params:
            r.error("PARAM_EXTRA", f"design.parameters.{name}",
                    f"parameter {name} is not a top-level RTL parameter")
    scope = contract.get("scope") or {}
    _check_keys(scope, SCOPE_KEYS, "scope", r, "scope")
    conf = scope.get("configuration") or {}
    for name, value in expected_params.items():
        if name not in conf:
            r.error("SCOPE_MISSING", "scope.configuration", f"missing parameter {name}")
        elif conf[name] != value:
            r.error("SCOPE_VALUE", f"scope.configuration.{name}",
                    f"scope says {conf[name]!r}, resolved value is {value!r}")
    configurations = scope.get("configurations")
    config_ids: set[str] = set()
    if configurations is not None:
        if not isinstance(configurations, list) or not configurations:
            r.error("SCOPE_CONFIGS", "scope.configurations",
                    "configurations must be a non-empty list when present")
            configurations = []
        for i, cfg in enumerate(configurations):
            where_cfg = f"scope.configurations[{i}]"
            if not isinstance(cfg, dict):
                r.error("SCOPE_CONFIGS", where_cfg, "must be an object")
                continue
            cid = str(cfg.get("id") or "")
            if not cid:
                r.error("SCOPE_CONFIGS", where_cfg, "missing id")
            elif cid in config_ids:
                r.error("SCOPE_CONFIGS", where_cfg, f"duplicate configuration id {cid!r}")
            config_ids.add(cid)
            cparams = cfg.get("parameters")
            if not isinstance(cparams, dict):
                r.error("SCOPE_CONFIGS", where_cfg, "parameters must be an object")
                continue
            for name in expected_params:
                if name not in cparams:
                    r.error("SCOPE_CONFIGS", where_cfg,
                            f"configuration {cid!r} is missing parameter {name}")
            for name in cparams:
                if name not in expected_params:
                    r.error("SCOPE_CONFIGS", where_cfg,
                            f"configuration {cid!r} has unknown parameter {name}")
            if cfg.get("status") not in (None, "reviewed", "proposed"):
                r.error("SCOPE_CONFIGS", where_cfg,
                        "status must be 'reviewed' or 'proposed'")
        active = {str(k): v for k, v in conf.items()}
        if configurations and not any(
                all(c.get("parameters", {}).get(k) == v for k, v in active.items())
                for c in configurations if isinstance(c, dict)):
            r.error("SCOPE_CONFIGS", "scope.configurations",
                    "the active scope.configuration is not one of the listed "
                    "configurations")

    # -- requirements --------------------------------------------------------------------------
    bundle_reqs = {str(q["id"]): q for q in bundle.get("requirements") or []}
    reqs = contract.get("requirements") or []
    seen_reqs: set[str] = set()
    for i, req in enumerate(reqs):
        if not isinstance(req, dict):
            r.error("SCHEMA", f"requirements[{i}]", "must be an object")
            continue
        rid = str(req.get("id") or "")
        where = f"requirement {rid or i}"
        if not rid:
            r.error("REQ_ID", where, "missing id")
            continue
        if rid in seen_reqs:
            r.error("REQ_DUP", where, f"duplicate requirement {rid}")
        seen_reqs.add(rid)
        if rid not in bundle_reqs:
            r.error("REQ_UNKNOWN", where,
                    f"{rid} is not in the requirement extraction "
                    "(formal/spec/requirements.json)")
            continue
        _check_keys(req, REQUIREMENT_KEYS, where, r, "requirement")
        disp = req.get("disposition")
        if disp not in DISPOSITIONS:
            r.error("REQ_DISPOSITION", where, f"invalid disposition {disp!r}")
        owner = str(req.get("owner") or "")
        if owner and owner not in REQUIREMENT_OWNERS:
            r.error("REQ_OWNER", where,
                    f"owner must be one of {sorted(REQUIREMENT_OWNERS)}, got {owner!r}")
        elif not owner:
            owner = ("environment" if disp == "assumption" else
                     "none" if disp in ("not_formalizable", "out_of_scope") else "dut")
        obligations = req.get("obligations")
        if obligations is not None:
            if not isinstance(obligations, list) or not obligations:
                r.error("REQ_OBLIGATIONS", where,
                        "obligations, when present, must be a non-empty list")
            else:
                for ob in obligations:
                    if ob not in OBLIGATIONS:
                        r.error("REQ_OBLIGATIONS", where,
                                f"unknown obligation {ob!r}")
        clarity = str(bundle_reqs[rid].get("clarity") or "clear")
        if disp == "formalized" and clarity != "clear":
            r.error("REQ_UNCLEAR_FORMALIZED", where,
                    f"requirement clarity is {clarity!r}; a non-clear requirement cannot be "
                    "formalized. Answer its clarification question first, or use "
                    "partially_formalized / assumption / not_formalizable / out_of_scope.")
        if clarity != "clear" and not str(req.get("reason") or "").strip():
            r.error("REQ_UNCLEAR_REASON", where,
                    f"clarity is {clarity!r}; the disposition reason must explain how the "
                    "unclear part was handled")
        reason = str(req.get("reason") or "")
        if len(reason.strip()) < 20:
            r.error("REQ_REASON", where, "reason must be a real sentence (>= 20 characters)")
        limitations = req.get("limitations")
        if disp == "partially_formalized":
            if not isinstance(limitations, str) or len(limitations.strip()) < 20:
                r.error("REQ_LIMITATIONS", where,
                        "partially_formalized requires a limitations sentence (>= 20 characters) "
                        "stating which part is not checked")
        elif limitations is not None and (not isinstance(limitations, str)
                                          or len(limitations.strip()) < 20):
            r.error("REQ_LIMITATIONS", where,
                    "limitations, when present, must be a real sentence (>= 20 characters)")
        ev = req.get("evidence")
        if not isinstance(ev, dict):
            r.error("REQ_EVIDENCE", where, "missing evidence object")
        else:
            _check_evidence(ev, where, root, bundle, r, extra_text=str(bundle_reqs[rid].get("text") or ""))
        got_feature = str(req.get("feature") or "")
        want_feature = str(bundle_reqs[rid].get("feature") or "")
        if got_feature and want_feature and got_feature != want_feature:
            r.error("REQ_FEATURE", where,
                    f"feature {got_feature!r} != structured_spec feature {want_feature!r}")
    for rid in bundle_reqs:
        if rid not in seen_reqs:
            r.error("REQ_MISSING", "requirements", f"requirement {rid} has no disposition")

    # -- properties ----------------------------------------------------------------------------
    props = contract.get("properties") or []
    seen_props: set[str] = set()
    by_req: dict[str, list[dict[str, Any]]] = {}
    reset_cycles = int((design.get("reset") or {}).get("cycles") or
                       (manifest.get("_formal") or {}).get("reset_cycles") or 2)
    default_depth = int((manifest.get("_formal") or {}).get("depth") or 12)
    for i, prop in enumerate(props):
        if not isinstance(prop, dict):
            r.error("SCHEMA", f"properties[{i}]", "must be an object")
            continue
        pid = str(prop.get("id") or "")
        where = f"property {pid or i}"
        if not pid:
            r.error("PROP_ID", where, "missing id")
            continue
        if pid in seen_props:
            r.error("PROP_DUP", where, f"duplicate property {pid}")
        seen_props.add(pid)
        rid = str(prop.get("req") or "")
        if rid not in bundle_reqs:
            r.error("PROP_REQ", where, f"req {rid!r} is not a requirement")
        else:
            by_req.setdefault(rid, []).append(prop)
            want_feature = str(bundle_reqs[rid].get("feature") or "")
            got_feature = str(prop.get("feature") or "")
            if got_feature and want_feature and got_feature != want_feature:
                r.error("PROP_FEATURE", where,
                        f"feature {got_feature!r} != requirement feature {want_feature!r}")
        kind = prop.get("kind")
        role = prop.get("role")
        if kind not in KINDS:
            r.error("PROP_KIND", where, f"invalid kind {kind!r}")
        if role not in ROLES:
            r.error("PROP_ROLE", where, f"invalid role {role!r}")
        _check_keys(prop, PROPERTY_KEYS, where, r, "property")
        obligation = property_obligation(prop)
        if obligation not in OBLIGATIONS:
            r.error("PROP_OBLIGATION", where,
                    f"cannot infer a verification obligation for kind {kind!r} / role {role!r}")
        elif prop.get("obligation") and prop.get("obligation") != obligation:
            r.error("PROP_OBLIGATION", where,
                    f"obligation {prop.get('obligation')!r} does not match the property "
                    f"shape ({obligation})")
        if obligation == "liveness":
            r.error("PROP_LIVENESS", where,
                    "unbounded liveness is not supported by the portable subset or the "
                    "configured backends; use a bounded response with a spec-justified "
                    "bound, or mark the requirement partially_formalized with the "
                    "missing liveness argument")
        owner = str(prop.get("owner") or "")
        inferred_owner = property_owner(prop)
        if owner and owner not in PROPERTY_OWNERS:
            r.error("PROP_OWNER", where,
                    f"owner must be one of {sorted(PROPERTY_OWNERS)}, got {owner!r}")
        elif owner and owner != inferred_owner:
            r.error("PROP_OWNER", where,
                    f"owner {owner!r} does not match role {role!r} ({inferred_owner})")
        if kind == "cover" and role == "cover" and prop.get("severity") == "error":
            r.warn("COVER_SEVERITY", where,
                   "an error-severity cover is treated as a required reachability "
                   "obligation; confirm the spec requires the scenario to exist")
        if len(str(prop.get("title") or "").strip()) < 8:
            r.error("PROP_TITLE", where, "title must say what is checked (>= 8 characters)")
        ev = prop.get("evidence")
        if not isinstance(ev, dict):
            r.error("PROP_EVIDENCE", where, "missing evidence object")
        else:
            extra = str(bundle_reqs.get(rid, {}).get("text") or "")
            _check_evidence(ev, where, root, bundle, r, extra_text=extra)
        if role == "cover" and kind != "cover":
            r.error("PROP_ROLE_KIND", where, "role cover requires kind cover")
        if kind == "cover" and role != "cover":
            r.error("PROP_ROLE_KIND", where, "kind cover requires role cover")
        if role == "assume" and kind != "always":
            r.error("PROP_ROLE_KIND", where, "role assume is only supported for kind always")
        if role == "assert" and kind == "cover":
            r.error("PROP_ROLE_KIND", where, "kind cover cannot be an assertion")
        if kind in ("response", "stability") and role != "assert":
            r.error("PROP_ROLE_KIND", where, f"kind {kind} must be an assertion")
        if kind == "reset_value" and role != "assert":
            r.error("PROP_ROLE_KIND", where, "kind reset_value must be an assertion")

        if kind in ("always", "reset_value", "cover"):
            if not str(prop.get("expr") or "").strip():
                r.error("PROP_FIELD", where, f"kind {kind} needs `expr`")
            else:
                validate_expr(str(prop.get("expr")), interface, r, f"{where}.expr",
                              allow_history=(kind != "reset_value"))
        elif kind in ("next", "response", "stability"):
            if not str(prop.get("trigger") or "").strip():
                r.error("PROP_FIELD", where, f"kind {kind} needs `trigger`")
            else:
                validate_expr(str(prop.get("trigger")), interface, r, f"{where}.trigger")
        if kind in ("next",):
            if not str(prop.get("expr") or "").strip():
                r.error("PROP_FIELD", where, "kind next needs `expr`")
            else:
                validate_expr(str(prop.get("expr")), interface, r, f"{where}.expr")
        if kind == "response":
            if not str(prop.get("response") or "").strip():
                r.error("PROP_FIELD", where, "kind response needs `response`")
            else:
                validate_expr(str(prop.get("response")), interface, r, f"{where}.response")
            mn = prop.get("min_delay")
            mx = prop.get("max_delay")
            if not isinstance(mn, int) or not isinstance(mx, int):
                r.error("PROP_DELAY", where, "response needs integer min_delay and max_delay")
            elif not (1 <= mn <= mx <= 64):
                r.error("PROP_DELAY", where, "need 1 <= min_delay <= max_delay <= 64")
            else:
                depth = int(prop.get("depth") or default_depth)
                if depth < reset_cycles + mx + 2:
                    r.error("PROP_DEPTH", where,
                            f"depth {depth} must be >= reset_cycles ({reset_cycles}) + "
                            f"max_delay ({mx}) + 2")
        if kind == "stability":
            if not str(prop.get("signal") or "").strip():
                r.error("PROP_FIELD", where, "kind stability needs `signal`")
            else:
                info = arg_type(str(prop.get("signal")), interface, r, f"{where}.signal")
                if info is None:
                    pass
            cyc = prop.get("cycles")
            if not isinstance(cyc, int) or cyc < 1:
                r.error("PROP_CYCLES", where, "stability needs `cycles` >= 1")
            elif cyc > 64:
                r.error("PROP_CYCLES", where, "stability cycles > 64 is not supported")
        if kind == "reset_value" and prop.get("disable_reset") is True:
            r.error("PROP_RESET", where, "reset_value must not disable reset")
        if prop.get("depth") is not None:
            if not isinstance(prop["depth"], int) or prop["depth"] < 1:
                r.error("PROP_DEPTH", where, "depth must be a positive integer")
            elif prop["depth"] < reset_cycles + 2:
                r.error("PROP_DEPTH", where,
                        f"depth {prop['depth']} must be >= reset_cycles + 2 ({reset_cycles + 2})")
        if prop.get("disable_reset") is not None and not isinstance(prop["disable_reset"], bool):
            r.error("PROP_FIELD", where, "disable_reset must be a boolean")
        if prop.get("when"):
            validate_expr(str(prop["when"]), interface, r, f"{where}.when",
                          allow_history=(kind != "reset_value"))
        if prop.get("severity") not in (None, "error", "warning", "info"):
            r.error("PROP_SEVERITY", where, "severity must be error, warning or info")
        if role == "assume":
            for ident in expr_identifiers(str(prop.get("expr") or "")):
                port = ports.get(ident)
                if port and port.get("direction") != "input":
                    r.error("ASSUME_OUTPUT", where,
                            f"assumption expression references DUT {port.get('direction')} "
                            f"signal {ident!r}; an environment assumption must constrain "
                            "inputs, not assume a DUT guarantee. A DUT handshake signal may "
                            "appear in `when` as a guard, but not in `expr`.")
            guard_outputs = [x for x in expr_identifiers(str(prop.get("when") or ""))
                             if (ports.get(x) or {}).get("direction") != "input"]
            if guard_outputs:
                r.warn("ASSUME_OUTPUT_GUARD", where,
                       "assumption guard references DUT signal(s) "
                       + ", ".join(sorted(set(guard_outputs)))
                       + "; this is allowed only when the signal is a handshake "
                       "qualifier, not when it restates the DUT obligation")
            for other in props:
                if other is prop or other.get("role") != "assert":
                    continue
                if (_norm_expr(other.get("expr")) == _norm_expr(prop.get("expr"))
                        and _norm_expr(other.get("trigger")) == _norm_expr(prop.get("trigger"))
                        and _norm_expr(other.get("when")) == _norm_expr(prop.get("when"))):
                    r.error("ASSUME_ASSERT_SAME", where,
                            f"assumption {pid} is identical to assertion {other.get('id')}; "
                            "this converts a DUT guarantee into an environment rule")
        if kind == "response":
            delay_source = str(prop.get("delay_source") or "")
            if not delay_source:
                r.warn("PROP_DELAY_SOURCE", where,
                       "response has no delay_source; record whether the bound comes "
                       "from the spec, the interface, the project or an engineering "
                       "decision so the bound is not mistaken for a spec fact")
            elif delay_source not in DELAY_SOURCES:
                r.error("PROP_DELAY_SOURCE", where,
                        f"delay_source must be one of {sorted(DELAY_SOURCES)}")
            elif delay_source == "engineering_decision":
                justification = (str(prop.get("notes") or "") + " "
                                 + str(prop.get("limitations") or "")).strip()
                if len(justification) < 20:
                    r.error("PROP_DELAY_JUSTIFICATION", where,
                            "an engineering-decision bound needs a notes/limitations "
                            "sentence explaining why it is the smallest justified bound")
        intent = prop.get("intent")
        if intent is not None:
            if not isinstance(intent, dict):
                r.error("PROP_INTENT", where, "intent must be an object")
            else:
                _check_keys(intent, INTENT_KEYS, f"{where}.intent", r, "intent")
                if intent.get("type") and intent.get("type") != obligation:
                    r.error("PROP_INTENT", where,
                            f"intent.type {intent.get('type')!r} != inferred obligation "
                            f"{obligation!r}")
                if intent.get("owner") and intent.get("owner") != inferred_owner:
                    r.error("PROP_INTENT", where,
                            f"intent.owner {intent.get('owner')!r} != property owner "
                            f"{inferred_owner!r}")
                sampling = intent.get("sampling")
                if isinstance(sampling, dict):
                    edge = sampling.get("edge")
                    clock_sig = (design.get("clock") or {}).get("signal")
                    if edge and edge != (design.get("clock") or {}).get("edge"):
                        r.error("PROP_INTENT", where,
                                f"intent.sampling.edge {edge!r} != design clock edge "
                                f"{(design.get('clock') or {}).get('edge')!r}")
                    if sampling.get("signal") and sampling.get("signal") != clock_sig:
                        r.error("PROP_INTENT", where,
                                f"intent.sampling.signal {sampling.get('signal')!r} != "
                                f"design clock {clock_sig!r}")
                elif sampling is not None:
                    r.error("PROP_INTENT", where, "intent.sampling must be an object")
                reset_policy = intent.get("reset_policy")
                if reset_policy not in (None, "disabled", "active", "none"):
                    r.error("PROP_INTENT", where,
                            "intent.reset_policy must be disabled, active or none")
                elif reset_policy == "disabled" and prop.get("disable_reset") is False:
                    r.error("PROP_INTENT", where,
                            "intent.reset_policy is disabled but disable_reset is false")
                elif reset_policy == "active" and prop.get("disable_reset") is not False:
                    r.error("PROP_INTENT", where,
                            "intent.reset_policy is active but disable_reset is not false")
                bound_source = intent.get("bound_source")
                if bound_source is not None and bound_source not in DELAY_SOURCES:
                    r.error("PROP_INTENT", where,
                            f"intent.bound_source must be one of {sorted(DELAY_SOURCES)}")
        proof = prop.get("proof")
        if proof is not None:
            if not isinstance(proof, dict):
                r.error("PROP_PROOF", where, "proof must be an object")
            else:
                _check_keys(proof, PROOF_KEYS, f"{where}.proof", r, "proof")
                method = str(proof.get("method") or "")
                if method and method not in ("bmc", "induction", "helper_invariants",
                                             "decomposition", "abstraction",
                                             "bounded_reset_check", "unsupported"):
                    r.error("PROP_PROOF", where, f"unknown proof method {method!r}")
                helpers = proof.get("helpers")
                if helpers is not None and not isinstance(helpers, list):
                    r.error("PROP_PROOF", where, "proof.helpers must be a list of property ids")
                if method in ("helper_invariants", "decomposition", "abstraction"):
                    soundness = str(proof.get("soundness") or "").strip()
                    if len(soundness) < 20:
                        r.error("PROP_PROOF", where,
                                f"proof method {method!r} needs a soundness obligation "
                                "(>= 20 characters) stating what must be proved separately")
        cfg_refs = prop.get("configurations")
        if cfg_refs is not None:
            if not isinstance(cfg_refs, list) or not cfg_refs:
                r.error("PROP_CONFIG", where, "configurations must be a non-empty list")
            else:
                for cid in cfg_refs:
                    if config_ids and str(cid) not in config_ids:
                        r.error("PROP_CONFIG", where,
                                f"configuration {cid!r} is not in scope.configurations")
                if config_ids and not cfg_refs:
                    r.error("PROP_CONFIG", where,
                            "the contract has a configuration matrix; each property must "
                            "name the configurations it covers")
        elif config_ids:
            r.error("PROP_CONFIG", where,
                    "the contract has a configuration matrix; each property must name "
                    "the configurations it covers")

    # -- disposition <-> properties ------------------------------------------------------------
    req_map = {str(q.get("id")): q for q in reqs if isinstance(q, dict) and q.get("id")}
    for rid, req in req_map.items():
        disp = req.get("disposition")
        plist = by_req.get(rid, [])
        roles = [p.get("role") for p in plist]
        owner = str(req.get("owner") or (
            "environment" if disp == "assumption" else
            "none" if disp in ("not_formalizable", "out_of_scope") else "dut"))
        breq = bundle_reqs.get(rid) or {}
        hint = str(breq.get("sva_hint") or "")
        category = str(breq.get("category") or "").lower()
        text_l = str(breq.get("text") or "").lower()
        reachability_only = (hint == "cover" or category in ("reachability", "coverage")
                             or "reachable" in text_l or "reachability" in text_l)
        if disp in ("formalized", "partially_formalized"):
            has_assert = any(x == "assert" for x in roles)
            if not has_assert and not (disp == "formalized" and reachability_only):
                r.error("REQ_NO_ASSERT", f"requirement {rid}",
                        f"disposition is {disp} but it has no assertion; a cover or "
                        "assumption cannot discharge a safety, bounded-response or "
                        "liveness obligation")
            if disp == "formalized" and not has_assert and not reachability_only:
                r.error("REQ_COVER_ONLY", f"requirement {rid}",
                        "a non-reachability requirement cannot be formalized by a "
                        "cover alone")
            if owner == "environment" and has_assert:
                r.error("REQ_OWNERSHIP", f"requirement {rid}",
                        "owner is environment but the requirement has DUT assertions; "
                        "split environment rules from DUT guarantees")
        elif disp == "assumption":
            if "assume" not in roles:
                r.error("REQ_NO_ASSUME", f"requirement {rid}",
                        "disposition is assumption but it has no assume property")
            if "assert" in roles:
                r.warn("REQ_ASSUME_ASSERT", f"requirement {rid}",
                       "marked assumption but also has assertions; split the requirement or use formalized")
            if owner == "dut":
                r.error("REQ_OWNERSHIP", f"requirement {rid}",
                        "a requirement owned by the DUT cannot be discharged only by "
                        "an environment assumption; this weakens the obligation")
        elif disp in ("not_formalizable", "out_of_scope"):
            if plist:
                r.error("REQ_HAS_PROP", f"requirement {rid}",
                        f"disposition is {disp} but properties cite it: "
                        + ", ".join(str(p.get('id')) for p in plist))
        declared = req.get("properties")
        if declared is not None:
            if not isinstance(declared, list):
                r.error("REQ_PROPERTIES", f"requirement {rid}",
                        "properties must be a list of property ids")
            else:
                actual = {str(p.get("id")) for p in plist}
                for pid in declared:
                    if str(pid) not in actual:
                        r.error("REQ_PROPERTIES", f"requirement {rid}",
                                f"declares property {pid!r} but no property cites {rid}")
                for pid in actual - {str(x) for x in declared}:
                    r.warn("REQ_PROPERTIES_UNDECLARED", f"requirement {rid}",
                           f"property {pid!r} cites {rid} but is not in the "
                           "requirement's properties list")
        if rid in bundle_reqs:
            hint = bundle_reqs[rid].get("sva_hint")
            if hint == "static" and disp == "formalized":
                r.warn("STATIC_FORMALIZED", f"requirement {rid}",
                       "sva_hint is static (structural); a temporal property needs an explicit "
                       "reason in the contract")
            if hint == "property" and disp == "not_formalizable":
                r.warn("PROPERTY_NOT_FORMALIZED", f"requirement {rid}",
                       "sva_hint says property but the requirement is marked not_formalizable")
            if hint == "assume" and disp != "assumption":
                r.warn("ASSUME_NOT_ASSUMPTION", f"requirement {rid}",
                       "sva_hint says assume but the disposition is not assumption")
            if hint == "cover" and not any(p.get("role") == "cover" for p in plist):
                r.warn("COVER_MISSING", f"requirement {rid}",
                       "sva_hint says cover but no cover property cites it")

    # -- feature / requirement coverage --------------------------------------------------------
    features = bundle.get("features") or []
    req_by_id = bundle_reqs
    for feature in features:
        fid = str(feature.get("id") or "")
        if not fid:
            continue
        f_reqs = [rid for rid in (feature.get("requirements") or []) if rid in req_map]
        if not f_reqs:
            r.warn("FEATURE_EMPTY", f"feature {fid}", "feature has no requirement in structured_spec")
            continue
        formalized = [rid for rid in f_reqs
                      if req_map[rid].get("disposition") in ("formalized", "partially_formalized")]
        documented = [rid for rid in f_reqs if req_map[rid].get("disposition") in
                      ("not_formalizable", "out_of_scope")]
        assumptions = [rid for rid in f_reqs if req_map[rid].get("disposition") == "assumption"]
        if not formalized and not documented:
            code = "FEATURE_GAP" if str(feature.get("class", "")).lower().startswith("mand") else "FEATURE_OPTIONAL_GAP"
            (r.error if code == "FEATURE_GAP" else r.warn)(
                code, f"feature {fid}",
                f"{len(f_reqs)} requirement(s), none formalized or documented as not formalizable"
                + (f"; assumptions: {', '.join(assumptions)}" if assumptions else ""))
    # Assumption satisfiability / vacuity hints.
    if any(p.get("role") == "assume" for p in props) and not any(p.get("role") == "cover" for p in props):
        r.warn("NO_COVER", "properties", "there are assumptions but no cover property; "
               "an unsatisfiable assumption makes every proof vacuous")
    if not any(p.get("role") == "cover" for p in props):
        r.warn("NO_COVER", "properties", "no cover property: reachability of triggers is not checked")
    if any(p.get("role") == "assume" for p in props):
        for p in props:
            if p.get("role") == "assume" and not p.get("when") and p.get("kind") == "always":
                r.warn("ASSUME_ALWAYS", f"property {p.get('id')}",
                       "an unguarded assume constrains every cycle; confirm it is an environment rule")

    # -- provenance / auxiliary state ----------------------------------------------------------
    metadata = contract.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        r.error("SCHEMA", "metadata", "metadata must be an object")
    provenance = contract.get("provenance")
    if provenance is not None:
        if not isinstance(provenance, dict):
            r.error("PROVENANCE", "provenance", "provenance must be an object")
        else:
            for key in ("model", "prompt_versions", "input_hashes", "validation_feedback",
                        "repair_history", "candidates"):
                if key in provenance and provenance[key] is None:
                    r.error("PROVENANCE", f"provenance.{key}", "must not be null")
            prompt_versions = provenance.get("prompt_versions")
            if prompt_versions is not None and not isinstance(prompt_versions, dict):
                r.error("PROVENANCE", "provenance.prompt_versions", "must be an object")
            repair = provenance.get("repair_history")
            if repair is not None and not isinstance(repair, list):
                r.error("PROVENANCE", "provenance.repair_history", "must be a list")
            if not provenance.get("model"):
                r.warn("PROVENANCE_MODEL", "provenance",
                       "record the generating model/agent so a result can be reproduced")
    else:
        r.warn("PROVENANCE_MISSING", "contract",
               "no provenance block: record the model, prompt/template versions, input "
               "hashes, validation feedback and repair history with the contract")
    auxiliary = contract.get("auxiliary")
    if auxiliary is not None:
        if not isinstance(auxiliary, dict):
            r.error("AUXILIARY", "auxiliary", "auxiliary must be an object")
        else:
            for key in ("scoreboards", "reference_models", "symbolic_transactions",
                        "bindings", "limitations"):
                if key in auxiliary and not isinstance(auxiliary[key], list):
                    r.error("AUXILIARY", f"auxiliary.{key}", "must be a list")
            for item in auxiliary.get("limitations") or []:
                if not isinstance(item, dict) or not item.get("label") or not item.get("reason"):
                    r.error("AUXILIARY", "auxiliary.limitations",
                            "each limitation needs a label and a reason")
                elif item.get("label") not in ("unsupported_by_skill",
                                               "unsupported_by_backend",
                                               "not_implemented",
                                               "reviewed_abstraction"):
                    r.error("AUXILIARY", "auxiliary.limitations",
                            f"unknown limitation label {item.get('label')!r}")

    # -- stats ---------------------------------------------------------------------------------
    dispositions: dict[str, int] = {k: 0 for k in DISPOSITIONS}
    for req in req_map.values():
        if req.get("disposition") in dispositions:
            dispositions[req["disposition"]] += 1
    r.stats = {
        "requirements": len(req_map),
        "features": len(features),
        "properties": len(props),
        "assertions": sum(1 for p in props if p.get("role") == "assert"),
        "assumptions": sum(1 for p in props if p.get("role") == "assume"),
        "covers": sum(1 for p in props if p.get("role") == "cover"),
        "obligations": {
            key: sum(1 for p in props if property_obligation(p) == key)
            for key in sorted(OBLIGATIONS)
        },
        "owners": {
            key: sum(1 for p in props if property_owner(p) == key)
            for key in sorted(PROPERTY_OWNERS)
        },
        "dispositions": dispositions,
        "clarity": {
            "clear": sum(1 for q in bundle_reqs.values()
                         if str(q.get("clarity") or "clear") == "clear"),
            "unclear": sum(1 for q in bundle_reqs.values()
                           if str(q.get("clarity") or "clear") != "clear"),
        },
        "parameters": expected_params,
        "configurations": len(config_ids) or 1,
        "reset_cycles": reset_cycles,
        "depth": default_depth,
    }
    if strict:
        # In strict mode warnings about a missing formalization decision become errors.
        for w in list(r.warnings):
            if w.code in ("FEATURE_OPTIONAL_GAP", "PROPERTY_NOT_FORMALIZED",
                          "STATIC_FORMALIZED", "ASSUME_NOT_ASSUMPTION"):
                r.errors.append(w)
                r.warnings.remove(w)
    return r


def _check_evidence(ev: dict[str, Any], where: str, root: Path, bundle: dict[str, Any],
                    result: Validation, extra_text: str = "") -> None:
    """Check evidence against the declared source span, not the whole document.

    ``extra_text`` is retained for call-site compatibility but deliberately not
    searched: a quote that only occurs in the requirement statement (which the
    model wrote) is not evidence from the specification.
    """
    _check_keys(ev, EVIDENCE_KEYS, f"{where}.evidence", result, "evidence")
    section = str(ev.get("section") or "").strip()
    quote = str(ev.get("quote") or "").strip()
    if not section:
        result.error("EVIDENCE_SECTION", where, "evidence.section is empty")
    if len(quote) < 20 or len(words(quote)) < 3:
        result.error("EVIDENCE_QUOTE", where,
                     "evidence.quote must be a verbatim sentence/clause (>= 20 chars, >= 3 words)")
        return
    spec_path = (bundle.get("spec") or {}).get("markdown")
    spec_text = ""
    if spec_path:
        try:
            spec_text = resolve_path(root, spec_path).read_text()
        except OSError:
            result.error("SPEC_MISSING", where, f"cannot read spec Markdown {spec_path}")
            return
    else:
        result.error("SPEC_MISSING", where, "bundle has no spec Markdown path")
        return
    recorded_sha = str(ev.get("source_sha256") or "")
    current_sha = str((bundle.get("sources") or {}).get("spec_markdown", {}).get("sha256")
                      or sha256_file(resolve_path(root, spec_path)))
    if recorded_sha and recorded_sha != current_sha:
        result.error("EVIDENCE_REVISION", where,
                     "evidence.source_sha256 does not match the current spec revision; "
                     "re-read the changed section and re-approve the quote")
    start, end, found = _section_span(spec_text, section)
    if not found:
        result.error("EVIDENCE_SECTION", where,
                     f"evidence.section {section!r} is neither a line range nor a "
                     "heading in the specification")
        return
    selected = "\n".join(spec_text.splitlines()[start:end])
    if normalize_text(quote) not in normalize_text(selected):
        result.error("EVIDENCE_OUTSIDE_SOURCE", where,
                     f"evidence quote is not verbatim inside the declared section "
                     f"{section!r}: {quote[:160]!r}")


def _bundle_is_current(manifest: dict[str, Any], paths: dict[str, Path]) -> bool:
    """True when the bundle was built from the current spec, extraction and RTL."""
    if not paths["bundle"].is_file():
        return False
    try:
        bundle = read_json(paths["bundle"])
    except FvError:
        return False
    sources = bundle.get("sources") or {}
    spec = manifest.get("spec") or {}
    spec_markdown = resolve_path(project_root(manifest),
                                spec.get("markdown") or "spec/spec.md")
    checks = (
        ("spec_markdown", spec_markdown),
        ("requirements", paths["requirements"]),
        ("extraction", paths["extraction"]),
    )
    for source_key, path in checks:
        record = sources.get(source_key) or {}
        if record.get("sha256"):
            if not path.is_file() or sha256_file(path) != record["sha256"]:
                return False
    design = sources.get("design") or {}
    if design.get("sha256") and sha256_file(Path(manifest["_path"])) != design["sha256"]:
        return False
    recorded_rtl = sources.get("rtl_set_sha256") or ""
    if recorded_rtl:
        current = source_files(manifest)
        if current["missing"] or sha256_paths(current["sources"]) != recorded_rtl:
            return False
    return True


def load_contract(manifest: dict[str, Any], contract_path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load (contract, bundle, interface) and rebuild a stale bundle if needed."""
    paths = {k: Path(v) for k, v in manifest["_paths"].items()}
    cpath = contract_path or paths["contract"]
    if not cpath.is_file():
        raise FvError(
            f"contract not found: {cpath}\n"
            "Write it from formal/schemas/input_bundle.json using prompts/contract.md, "
            "then run `fv.py validate`."
        )
    contract = read_json(cpath)
    if not paths["interface"].is_file() or not _bundle_is_current(manifest, paths):
        from fv_bundle import prepare
        prepare(manifest, paths["bundle"], quiet=True)
    bundle = read_json(paths["bundle"])
    interface = read_json(paths["interface"])
    return contract, bundle, interface


def validate_design(manifest: dict[str, Any], contract_path: Path | None = None,
                    strict: bool = False) -> tuple[Validation, dict[str, Any], dict[str, Any], dict[str, Any]]:
    contract, bundle, interface = load_contract(manifest, contract_path)
    result = validate_contract(manifest, contract, bundle, interface, strict=strict)
    return result, contract, bundle, interface


if __name__ == "__main__":
    import argparse
    from fv_common import load_manifest

    ap = argparse.ArgumentParser(description="validate a formal SVA contract")
    ap.add_argument("--design", required=True)
    ap.add_argument("--contract")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    m = load_manifest(args.design)
    res, _c, _b, _i = validate_design(m, Path(args.contract) if args.contract else None, args.strict)
    for e in res.errors:
        print(f"ERROR {e}")
    for w in res.warnings:
        print(f"WARN  {w}")
    print(json.dumps(res.stats, indent=2))
    raise SystemExit(1 if res.errors else 0)
