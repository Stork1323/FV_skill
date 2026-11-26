# Formal verification report: dual_clock

- **Top:** `dual_clock`
- **Clock:** `clk_a` (posedge)
- **Reset:** `rst_a_n` (low-active, async)
- **Configuration:** (none)
- **Backend:** symbiyosys SBY v0.71
- **Generated:** 2026-11-26T01:58:51+00:00
- **Overall:** **PASS**

## Human-readable summary

This run checked **5** properties across **5** requirements. **2** proven, **2** bounded-pass only, **1** covered, **0** failed, **0** errors, **0** timeouts.

## Summary

| Metric | Count |
|---|---:|
| Properties | 5 |
| Requirements | 5 |
| Proven | 2 |
| BMC pass (not proven) | 2 |
| Covered | 1 |
| Not reached within bound | 0 |
| Unreachable/proven unreachable | 0 |
| Assumptions satisfiable | 0 |
| Vacuous assumptions | 0 |
| Partially formalized requirements | 0 |
| Incomplete obligations | 0 |
| Failed | 0 |
| Errors | 0 |
| Timeouts | 0 |

## What to do with this report

### Spec / Architecture

- Confirm each requirement's disposition, evidence, and limitations match the intended specification.
- Resolve blocking clarification questions before treating a requirement as formalized.
- For `not_formalizable`/`out_of_scope`, record the reason and the alternative verification plan.

### RTL / Design

- Treat `FAIL` as a counterexample-backed mismatch: inspect trace, requirement evidence, and RTL behavior.
- Treat `BMC_PASS` as bounded only; do not claim unbounded proof.
- Treat `UNREACHABLE`/`NOT_REACHED_WITHIN_BOUND` as missing reachability evidence; check assumptions, reset, and stimulus.

## Signoff readiness

- **Schema Version:** 1.1
- **Execution Complete:** yes
- **Proof Complete:** no
- **Reachability Checked:** yes
- **Environment Checked:** yes
- **Review Clean:** no
- **Regression Clean:** yes
- **No Requirement Gaps:** yes
- **Strict Signoff Ready:** yes
- **Signoff Ready:** yes
- **Warnings:** SVA/constraint review has warnings; completeness ledger has 3 unmapped normative statement(s)


## Feature coverage

| Feature | Class | Status | Requirements | Formalized | Partial | Assumption | Not formalizable | Out of scope |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `F01` | Mandatory | **OK** | 1 | 1 | 0 | 0 | 0 | 0 |
| `F02` | Mandatory | **OK** | 2 | 2 | 0 | 0 | 0 | 0 |
| `F03` | Mandatory | **OK** | 2 | 2 | 0 | 0 | 0 | 0 |

## Requirement coverage

| Requirement | Feature | Disposition | Status | Properties | Evidence |
|---|---|---|---|---|---|
| `REQ-001` | F03 | formalized | **BMC_PASS** | PRP-REQ-001-01 | 4.1 Domain A request capture |
| `REQ-002` | F01 | formalized | **PROVEN** | PRP-REQ-002-01 | 4.1 Domain A request capture |
| `REQ-003` | F03 | formalized | **BMC_PASS** | PRP-REQ-003-01 | 4.2 Domain B synchronization and acknowledge |
| `REQ-004` | F02 | formalized | **PROVEN** | PRP-REQ-004-01 | 4.2 Domain B synchronization and acknowledge |
| `REQ-005` | F02 | formalized | **COVERED** | COV-REQ-005-01 | 4.3 Verification scenarios |

## Property results

| Property | Req | Kind | Role | Depth | BMC | Induction | Status | Why | Time (s) |
|---|---|---|---|---:|---|---|---|---|---:|
| `PRP-REQ-001-01` | `REQ-001` | reset_value | assert | 12 | pass |  | **BMC_PASS** | No counterexample was found within the bounded search, but induction did not prove the property; this is bounded evidence, not an unbounded proof. BMC: SBY  8:58:50 [/private/var/folders/lm/fz6crp5d0vsd7sqzd09xkzvw0000gn/T/opencode/dual_clock_dev/formal/build/sby/prp_req_001_01/bmc] DONE (PASS, rc=0) | 0.136 |
| `PRP-REQ-002-01` | `REQ-002` | next | assert | 12 | pass | pass | **PROVEN** | The base case passed and the induction step proved the property for all reachable states under the recorded assumptions and configuration. BMC: SBY  8:58:50 [/private/var/folders/lm/fz6crp5d0vsd7sqzd09xkzvw0000gn/T/opencode/dual_clock_dev/formal/build/sby/prp_req_002_01/bmc] DONE (PASS, rc=0) Induction: SBY  8:58:50 [/private/var/folders/lm/fz6crp5d0vsd7sqzd09xkzvw0000gn/T/opencode/dual_clock_dev/formal/build/sby/prp_req_002_01/induct] DONE (PASS, rc=0) | 0.244 |
| `PRP-REQ-003-01` | `REQ-003` | reset_value | assert | 12 | pass |  | **BMC_PASS** | No counterexample was found within the bounded search, but induction did not prove the property; this is bounded evidence, not an unbounded proof. BMC: SBY  8:58:50 [/private/var/folders/lm/fz6crp5d0vsd7sqzd09xkzvw0000gn/T/opencode/dual_clock_dev/formal/build/sby/prp_req_003_01/bmc] DONE (PASS, rc=0) | 0.119 |
| `PRP-REQ-004-01` | `REQ-004` | next | assert | 12 | pass | pass | **PROVEN** | The base case passed and the induction step proved the property for all reachable states under the recorded assumptions and configuration. BMC: SBY  8:58:51 [/private/var/folders/lm/fz6crp5d0vsd7sqzd09xkzvw0000gn/T/opencode/dual_clock_dev/formal/build/sby/prp_req_004_01/bmc] DONE (PASS, rc=0) Induction: SBY  8:58:51 [/private/var/folders/lm/fz6crp5d0vsd7sqzd09xkzvw0000gn/T/opencode/dual_clock_dev/formal/build/sby/prp_req_004_01/induct] DONE (PASS, rc=0) | 0.24 |
| `COV-REQ-005-01` | `REQ-005` | cover | cover | 12 | unreachable |  | **COVERED** | The solver found a witness trace that reaches the cover condition within the configured bound. BMC: SBY  8:58:51 [/private/var/folders/lm/fz6crp5d0vsd7sqzd09xkzvw0000gn/T/opencode/dual_clock_dev/formal/build/sby/cov_req_005_01/bmc] DONE (PASS, rc=0) | 0.127 |

## SVA and constraint review

- **Overall:** **WARN**
- **Findings:** 2 (0 error, 2 warning, 0 info)
- **Detail:** `formal/reports/sva_review.md`

- **WARNING** `TRIGGER_UNCOVERED` (PRP-REQ-002-01): PRP-REQ-002-01 asserts a trigger but no cover reaches that trigger.
- **WARNING** `TRIGGER_UNCOVERED` (PRP-REQ-004-01): PRP-REQ-004-01 asserts a trigger but no cover reaches that trigger.

## Limitations

- SymbiYosys/smtbmc BMC is bounded; a passing BMC is not a proof.
- SymbiYosys `prove` uses k-induction; properties that are not inductive remain unproven even when the BMC base case passes.
- The run covers the configuration in scope.configuration, forces reset for the first 2 cycle(s), and checks up to depth 12.
- Cover properties report reachability within the BMC depth, not exhaustive reachability.
- The generated standard SVA file is not executed by this run; it is provided for commercial tools.
- Assumptions are environment rules. A VACUOUS assumption means its active condition was not reached.

## Reproduce

```sh
python3 scripts/fv.py run --design <formal/design.json>
python3 scripts/fv.py report --design <formal/design.json>
```
