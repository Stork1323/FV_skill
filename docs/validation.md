# Validation record

The skill is tested at three levels: a stdlib-only unit suite, a checked-in
synthetic fixture that runs the whole ordered pipeline, and a temporary
spec-only project created from just `spec.md` plus RTL. A prior run against a
real Q3TUI design is recorded at the end as historical evidence for the
contract/render/run engine.

All current runs used Python 3.9.6, Yosys `0.71+post` and the Yosys backend.
`sby` is not installed on this machine, so SymbiYosys is covered by generated
file and output-classifier tests, not a live `sby` invocation. No commercial
tool is installed; the `command` backend is covered with a fake executable.

## Unit suite

```bash
python3 -m unittest discover -s tests -v
```

**62 tests, all passing.** The suite covers:

* expression/type validation and history functions;
* contract traceability, dispositions and clarity gating;
* extraction evidence checks, changed-`spec_sha256` rejection and questions;
* rule generation, hash checking, refresh and hand-edit rejection;
* review findings (vacuous assumptions, unreachable covers, uncovered
  triggers, constant/duplicate properties, SVA leftovers);
* tool defaults, backend selection and command-backend classification;
* SymbiYosys script generation and log parsing;
* pipeline ordering, missing/stale steps, bundle freshness and gates;
* regression ranking, baseline creation and refusal after failure;
* the counter fixture and an intentional failure contract;
* report/JUnit formats and checked-in JSON validity;
* extraction format validation (unknown owners and malformed sources);
* `fv.py extract --update-spec-hash` after a changed spec;
* pipeline gates: hand-edited rules stop the rules step, review-blocked
  baselines refuse both the ordered pipeline and standalone `regress --update
  --run`; an explicit `--allow-review-errors` exception records the baseline.

## Counter fixture (checked in)

Location: `examples/counter/`; the fixture pins `backend: yosys` because the
test machine has no `sby`. New projects default to SymbiYosys.

```bash
python3 scripts/fv.py all \
  --design examples/counter/formal/design.json --jobs 2
```

The ordered run is `rules -> extract -> contract -> render -> run -> review ->
regress`.

| Property | Requirement | Kind | Status |
|---|---|---|---|
| `PRP-REQ-001-01` | `REQ-001` | `reset_value` | `BMC_PASS` |
| `PRP-REQ-003-01` | `REQ-003` | `next` | `PROVEN` |
| `PRP-REQ-004-01` | `REQ-004` | `next` | `PROVEN` |
| `PRP-REQ-005-01` | `REQ-005` | `next` | `PROVEN` |
| `COV-REQ-006-01` | `REQ-006` | `cover` | `COVERED` |

Overall verification `PASS`; regression `PASS` (5 unchanged properties).
Feature `F02` (increment/hold/wrap) is `OK`; `F01` is `WARN` because two of its
requirements (single-clock structure and port width) are documented as
`not_formalizable`.

The SVA/constraint review is `WARN`, not `ACTION_REQUIRED`: it reports three
`TRIGGER_UNCOVERED` warnings because the assertion triggers have no matching
cover in this minimal fixture. They are advisory here and are visible in
`formal/reports/sva_review.md`. A review with an `error` finding fails the
pipeline and refuses a baseline update unless `--allow-review-errors` is an
explicit exception; the suite tests both the refusal and the exception.

The intentional failure contract `formal_contract_fail.json` produces `FAIL`
with a counterexample for `PRP-REQ-004-02`; the suite asserts this. A failing
run also refuses a baseline update.

## Spec-only project

The suite creates a project from `spec/spec.md` plus `src/rtl/filelist.f` with
no pre-existing `formal/` directory:

```bash
python3 scripts/fv.py init --project PROJ --top counter \
  --spec PROJ/spec/spec.md --rtl PROJ/src/rtl/filelist.f
```

This generates `formal/design.json`, `formal/tools.json` (default
`symbiyosys`), `formal/rules/`, the interface and an empty extraction scaffold.
`init` exits successfully without creating `input_bundle.json`, because no
requirements exist yet. After the extraction and contract are written, `all`
runs the complete ordered pipeline and reports `PASS`.

## AXI4 SRAM controller (real design, historical)

Source design: `examples/axi4_sram_ctrl/` in
`VLSIT_Q3TUI_ASIC_AI_FLOW` (Q3TUI example project). The formal inputs were
built with the skill's earlier four-step contract/render/run/regress flow in a
separate working copy; this run is kept as evidence that the engine scales, not
as a validation of the current extraction step.

Configuration: `PARA_DATA_WD=32`, `PARA_ADDR_WD=32`, `PARA_ID_WD=4`,
`PARA_LEN_WD=8`, `PARA_FIFO_DEPTH=8`, `PARA_NUM_BANKS=1`, top
`m_vlsi_axi4_sram`, depth 12.

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
| Overall | `PASS` |
| Runtime (jobs 4) | ~53 s |

All 66 requirements have a disposition. Eight are `partially_formalized` with
an explicit limitations sentence; the rest are `not_formalizable` (internal
FIFO/FSM/arbiter/burst metadata, structural facts), `out_of_scope`, or
`assumption`. Every feature is `WARN`, not `OK`, because each feature contains
at least one documented unformalized requirement. The report never claims
transfer-level completeness that the portable subset cannot establish.

The 9 `BMC_PASS` properties include response/handshake checks that pass to
depth 12 but are not inductive from an arbitrary state. The report labels their
induction as `not inductive`; they are not presented as proofs. The 5
`PROVEN` properties are invariants that temporal induction discharged.

Regression: a baseline was created with `fv.py regress --update` and a second
run compared `UNCHANGED` for all 19 properties, 66 requirements and 15
features, with `contract_changed: false`.

## Environment-dependent checks

| Capability | State on this machine | How it is covered |
|---|---|---|
| Yosys backend | available (`0.71+post`) | live on the counter fixture |
| SymbiYosys backend | `sby` absent | generated `.sby`/script and log-classifier tests |
| Commercial backend | none installed | `command` backend with a fake executable and fixed status map |
| Commercial result parsing | no live logs | a line that does not match `result_pattern` is `ERROR`, never a pass |

`fv.py doctor --design ...` reports every configured backend and whether its
executable exists, so these gaps are visible at the start of a run instead of
being mistaken for a pass.

## What this establishes

* The extraction/validation/rendering pipeline is independent of the design:
  the same scripts process a 4-bit counter, a spec-only temporary project and a
  parameterized AXI4 SRAM controller.
* Traceability is enforced: every requirement is accounted for, every property
  quotes the spec, and non-formalizable behavior is visible instead of being
  silently dropped.
* The status model distinguishes proofs (`PROVEN`) from bounded passes
  (`BMC_PASS`) and reports documented gaps as `WARN` rather than `OK`.
* The ordered pipeline cannot skip rule generation, extraction, review or
  regression, and a stale bundle or changed spec is rejected.
* Reports are deterministic and machine-readable; the JSON is authoritative and
  Markdown is rendered from it.

It does not establish that the AXI contract covers every internal behavior; it
does not, by design, and the report says so.
