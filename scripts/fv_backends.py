"""Backend dispatch for one property."""

from __future__ import annotations

from typing import Any

from fv_common import FvError
import fv_command
import fv_sby
import fv_yosys


def run_property(harness: dict[str, Any], prop: dict[str, Any], cfg: dict[str, Any],
                 depth: int | None = None, timeout_s: int | None = None,
                 induction: bool | None = None, quiet: bool = False) -> dict[str, Any]:
    kind = str(cfg.get("kind") or cfg.get("name") or "")
    if kind == "yosys":
        return fv_yosys.run_property(harness, prop, depth, timeout_s, induction, quiet=quiet)
    if kind == "symbiyosys":
        return fv_sby.run_property(harness, prop, cfg, depth, timeout_s, induction, quiet=quiet)
    if kind == "command":
        return fv_command.run_property(harness, prop, cfg, depth, timeout_s, induction, quiet=quiet)
    raise FvError(f"unsupported backend kind {kind!r}")
