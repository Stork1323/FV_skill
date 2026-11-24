# Validation record

This record separates **current reproducible validation** from **historical evidence**.

## Current validation (this workspace)

- **Date:** 2026-11-24
- **Python:** 3.9.6
- **Yosys:** 0.71+post (AppleClang)
- **SBY:** 0.71 (smtbmc/yices/z3 available)
- **iverilog/vvp:** installed; **verilator:** installed
- **Commercial formal tools:** not installed; VC Formal adapter is mock/conformance-tested only.

### Portable unit suite

```bash
python3 -m unittest discover -s tests -v
```

**Result: 72 tests, OK.** Coverage includes contract validation, extraction/evidence strictness, rule integrity, render/history/signedness, regression semantic weakening, signoff/binding gates, P3 extraction approval gate, AI eval corpus, SBY/Yosys parser/command-backend behavior, and live backend smokes.

### Live open-source backend qualification (Yosys + SBY)

Executed by `tests/test_live_backends.py` (skips if tool missing):

| Backend | Case | Expected | Observed |
|---|---|---|---|
| Yosys | counter pass run | PASS, cover covered, proofs/bounded passes | PASS |
| Yosys | counter fail contract | FAIL with counterexample | FAIL + cex |
| SBY | counter pass run | PASS, covered, inductive next props PROVEN | PASS, `BMC_PASS=1 COVERED=1 PROVEN=3` |
| SBY | counter fail contract | FAIL with counterexample | FAIL + trace_tb excerpt |
| SBY | unreachable cover | `NOT_REACHED_WITHIN_BOUND` (bounded, not proven unreachable) | `NOT_REACHED_WITHIN_BOUND` |
| SBY | vacuous/guarded assume smoke | `VACUOUS` or `ASSUMED_UNCHECKED` | observed by live test |

**Not qualified live:** VC Formal/commercial backends (no license/environment). Any VC Formal result should be treated as conformance-only until run on a licensed environment with pass/fail/timeout/vacuity cases.

### Counter fixture (checked in, live SBY)

```bash
python3 scripts/fv.py all --design examples/counter/formal/design.json --backend symbiyosys --jobs 2 --update-baseline
```

Ordered pipeline: `rules -> extract -> contract -> render -> run -> review -> regress`. Result: verification PASS, review WARN (advisory trigger-coverage findings), regression PASS. BMC_PASS=1, COVERED=1, PROVEN=3.

### Known limitations / gaps

- VC Formal live qualification is blocked by missing commercial license/environment.
- Cover no-witness is reported as `NOT_REACHED_WITHIN_BOUND` for BMC/cover engines; `PROVEN_UNREACHABLE` is reserved for explicit backend proof.
- Config-matrix, reset-scenario matrix, transaction-level scoreboards, full sim replay, and proof-dependency soundness checks are partially/architecture-level; expand with dedicated fixtures before claiming closure.
- Documentation and rule templates should continue to be synchronized with behavior.

## Historical evidence (not current signoff)

### AXI4 SRAM controller (real design, historical)

Source design: `examples/axi4_sram_ctrl/` in `VLSIT_Q3TUI_ASIC_AI_FLOW` (Q3TUI example project). The formal inputs were built with an earlier four-step flow in a separate working copy; this run is evidence that the engine scales, not a validation of the current extraction/AI/review/signoff behavior.

Configuration: `PARA_DATA_WD=32`, `PARA_ADDR_WD=32`, `PARA_ID_WD=4`, `PARA_LEN_WD=8`, `PARA_FIFO_DEPTH=8`, `PARA_NUM_BANKS=1`, top `m_vlsi_axi4_sram`, depth 12.

| Metric | Result |
|---|---:|
| Requirements | 66 |
| Properties | 19 |
| Proven | 5 |
| BMC pass (not proven) | 9 |
| Covered | 2 |
| Assumptions satisfiable | 3 |
| Partially formalized requirements | 8 |
| Failed / errors / timeouts | 0 / 0 / 0 |
| Features (all `WARN`) | 15 |
| Overall | `PASS` (historical run) |
| Runtime (jobs 4) | ~53 s |

This historical record should not be used as evidence for current P0-P3 signoff semantics without re-running under the current skill version.
