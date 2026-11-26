# Formal SVA Skill (`formal-sva`)

`formal-sva` is an OpenCode skill for turning a hardware specification into traceable SystemVerilog Assertions (SVA), running formal checks, reviewing quality, and keeping a regression baseline.

It is designed to be **spec-traceable**: requirements come from the spec, SVA in the spec is treated as compulsory input, and results are reported with evidence.

## What it does

- Extracts reviewable requirements from `spec/spec.md`.
- Detects SVA/assertions written in the spec and turns them into compulsory `REQ-SVA-*` requirements.
- Requires human approval of extracted requirements before downstream formal work.
- Builds a formal contract (`formal_contract.json`) that maps requirements to properties.
- Renders standard SVA + backend harness files.
- Runs formal checks with **SymbiYosys** (default) or **Yosys** SAT, or a configured command backend.
- Produces review, questions, reports, and waveform evidence (VCD + SVG) for properties.
- Compares results against a committed regression baseline.

## Prerequisites

- Python 3 (skill scripts).
- For live formal runs:
  - `yosys` for Yosys backend.
  - `sby` (+ Yosys/solver) for SymbiYosys backend.
- Optional: `iverilog`/`vvp` for replay, `fst2vcd` for FST trace conversion.
- Commercial backends (e.g. VC Formal) require a licensed environment and backend configuration in `formal/tools.json`.

## Quick start

Run commands from the skill directory, or call scripts by path. `--design` points to the target project's `formal/design.json`.

```bash
# 1) Create formal/design.json scaffold
python3 scripts/fv.py init \
  --project /path/to/project \
  --top <top_module> \
  --spec spec/spec.md \
  --rtl src/rtl/filelist.f

# 2) Extract requirements from spec
python3 scripts/fv.py extract --design /path/to/project/formal/design.json

# 3) Human reviews formal/spec/requirements.md, then approves extraction
python3 scripts/fv.py approve-extraction \
  --design /path/to/project/formal/design.json \
  --reviewer "<human-name>" \
  --note "Reviewed extraction"

# 4) Build bundle for contract writing
python3 scripts/fv.py prepare --design /path/to/project/formal/design.json

# 5) Write/review formal/schemas/formal_contract.json, then validate
python3 scripts/fv.py validate --design /path/to/project/formal/design.json

# 6) Render, run, review, regress (or use the ordered pipeline)
python3 scripts/fv.py render --design /path/to/project/formal/design.json
python3 scripts/fv.py run    --design /path/to/project/formal/design.json --backend auto --jobs 4
python3 scripts/fv.py review --design /path/to/project/formal/design.json
python3 scripts/fv.py regress --design /path/to/project/formal/design.json

# One-command ordered flow (after extraction/contract are ready)
python3 scripts/fv.py all --design /path/to/project/formal/design.json --backend auto --jobs 4
```

## Workflow / Flow

The normal flow is:

1. **Init** (`init`)
   - Creates `formal/design.json` and standard formal directories.
   - Infers clock/reset when possible; confirm them.

2. **Extract** (`extract`)
   - Reads spec and produces reviewable extraction artifacts:
     - `formal/spec/requirements.md` (human-reviewable)
     - `formal/spec/requirements.json` (machine-readable)
     - `formal/spec/spec_sva.*` and `spec_sva_requirements.md` when spec contains SVA
   - Spec-authored SVA is compulsory: it becomes `REQ-SVA-*` requirements.

3. **Human approval** (`approve-extraction`) — **compulsory gate**
   - A human reviews extraction and records approval in `formal/spec/extraction_approval.json`.
   - Approval includes a canonical requirements digest; stale approvals are rejected.
   - Pipeline/signoff/regress-update refuse to continue without valid approval.

4. **Prepare** (`prepare`)
   - Builds design/interface bundle used for contract writing:
     - `formal/schemas/input_bundle.json`
     - `formal/schemas/design_interface.json`

5. **Contract** (human/LLM writes, then `validate`)
   - `formal/schemas/formal_contract.json` maps requirements to properties.
   - Every requirement needs a disposition: `formalized`, `partially_formalized`, `not_formalizable`, `assumption`, or `out_of_scope`.
   - Spec SVA requirements must map to properties with matching role and `spec_sva_id`.
   - Only explicit human-approved `formal/spec/spec_sva_waivers.json` can waive a compulsory spec SVA.

6. **Render** (`render`)
   - Generates SVA/harness files under `formal/src/generated/`.
   - Produces `fv_manifest.json` describing rendered properties, clocks/resets/domains.

7. **Run** (`run`)
   - Runs properties through selected backend.
   - Produces `formal/results/formal_results.json`, logs, report, JUnit.

8. **Review / Questions / Waveforms** (`review`, `questions`, `waveforms`)
   - `review`: checks SVA quality/vacuity/constraint issues.
   - `questions`: reports blocking clarification items.
   - `waveforms`: always writes both `.vcd` and `.svg` per property in `formal/reports/waveforms/`.

9. **Regress** (`regress`)
   - Compares current results to `formal/regression/baseline.json`.
   - `--update` records a new baseline; strict gate prevents updating from bad/review-blocked states.

## Key concepts

### Spec SVA is compulsory
If the spec contains SVA/assertions, extraction creates `REQ-SVA-*` requirements. The contract must preserve them (same intent/role) by mapping them to properties with `spec_sva_id`. Do not silently drop, weaken, or convert assert/assume/cover.

### Human extraction approval is compulsory
Downstream commands require `formal/spec/extraction_approval.json` with valid reviewer/timestamp and matching canonical requirements digest. This prevents stale or unreviewed extraction from flowing into formal signoff.

### Multi-clock/reset domains
Contracts can declare:

- `design.clock/reset` as defaults.
- `design.clocks[]` / `design.resets[]` for named domains.
- Per-property `clock`, `reset`, or `domain`.

Render/run bind each property to its declared domain. CDC/metastability/MTBF/analog timing are out of scope unless explicitly modeled.

### Waveform evidence
For each property, the skill emits **both**:

- `formal/reports/waveforms/<PROP>.vcd`
- `formal/reports/waveforms/<PROP>.svg`

Real backend traces are used when available; otherwise a clearly labeled synthetic sanity VCD is generated so evidence always includes VCD+snapshot.

## Project artifacts (target project)

A typical target project gets:

```text
<project>/
  spec/
    spec.md
  formal/
    design.json                     # manifest: paths, top, clock/reset, backend, depth
    tools.json                      # backend/tool config
    rules/                          # generated project rules
    spec/
      requirements.md               # human-reviewable extraction
      requirements.json             # machine-readable extraction
      spec_sva.json/.md             # SVA found in spec (if any)
      spec_sva_import.json          # contract import candidates
      spec_sva_requirements.md      # compulsory REQ-SVA requirements
      spec_sva_waivers.json         # human-approved SVA waivers (only when needed)
      extraction_approval.json      # compulsory human approval
    schemas/
      input_bundle.json             # prepare output
      design_interface.json         # prepare output
      formal_contract.json          # requirement->property contract
    src/generated/                  # rendered SVA/harness/manifest
    build/                          # backend build artifacts
    results/
      formal_results.json
    reports/
      report.md / report.json
      junit.xml
      sva_review.md / .json
      questions.md / .json
      waveforms/                    # PRP-XXX.vcd + PRP-XXX.svg
    regression/
      baseline.json
    state/
      pipeline.json
```

## Skill file structure

```text
skills/formal-sva/
  README.md                         # This file
  SKILL.md                          # Skill behavior/instructions for agents
  scripts/
    fv.py                           # CLI entry point
    fv_pipeline.py                  # Ordered pipeline + extraction approval gate
    fv_extract.py                   # Spec extraction + spec SVA merge
    fv_contract.py                  # Contract validation
    fv_render.py                    # SVA/harness rendering
    fv_run.py                       # Backend execution + result aggregation
    fv_sby.py / fv_yosys.py         # Backend adapters
    fv_waveform.py                  # VCD+SVG evidence generation
    fv_review.py / fv_regress.py    # Review and regression
    fv_*.py                         # Supporting utilities
  prompts/
    extract.md                      # Extraction prompt guidance
    contract.md                     # Contract-writing prompt guidance
  rules/                            # Rule templates copied into target projects
  schemas/
    formal_contract.schema.json     # Contract schema
  templates/                        # Contract/pattern templates
  examples/
    counter/                        # Small single-clock example
    dual_clock/                     # Multi-clock domain fixture
  tests/                            # Unit/integration/live qualification tests
  docs/                             # Validation/report/integration notes
```

## Useful commands

```bash
# Check toolchain/backends
python3 scripts/fv.py doctor

# Validate contract only
python3 scripts/fv.py validate --design /path/to/project/formal/design.json

# Run with a specific backend
python3 scripts/fv.py run --design ... --backend yosys --jobs 1
python3 scripts/fv.py run --design ... --backend symbiyosys --jobs 2

# Generate waveform evidence from existing results
python3 scripts/fv.py waveforms --design /path/to/project/formal/design.json

# See pipeline state
python3 scripts/fv.py status --design /path/to/project/formal/design.json
```

## Caveats / limits

- **Bounded vs proven**: `BMC_PASS` is bounded; only `PROVEN` is an inductive proof. `NOT_REACHED_WITHIN_BOUND` is not unreachability proof.
- **CDC/cross-domain**: domain binding is supported, but metastability/MTBF/analog CDC effects are out of scope unless explicitly modeled.
- **Commercial backends**: command adapters can be configured, but live qualification requires a licensed environment. Report live qualification separately from mocked/conformance tests.
- **Spec SVA waivers**: use only with human approval; waivers are explicit exceptions, not normal flow.
