# Report formats

Every generated report is produced by a deterministic script from a JSON
document with `schema_version: "1.0"`. Markdown is a view of the JSON, never a
separate hand-written summary. Column order, section order and status words are
fixed; a report that does not follow this page is a skill bug.

Requirements extraction (`formal/spec/requirements.md`) is the one input to
this chain that is authored by an LLM; it is fixed-format, parsed by
`fv.py extract`, and all following artifacts are generated.

All reports are written under the project's `formal/` directory and use paths
relative to the project root. Timestamps are ISO-8601 UTC.

## Status vocabulary

| Status | Meaning |
|---|---|
| `PROVEN` | BMC passed and induction proved the property unbounded. |
| `BMC_PASS` | Passed to the depth; **not** a proof. |
| `COVERED` | A cover witness was found within the depth. |
| `UNREACHABLE` | The cover was not reached within the depth. |
| `ASSUMED` | The assumption is satisfiable/active within the depth. |
| `VACUOUS` | The assumption's guard was never reached. |
| `FAIL` | A counterexample was found. |
| `ERROR` | The backend failed or its output was not classified. |
| `TIMEOUT` | The per-property timeout expired. |
| `OK` / `WARN` / `GAP` | Feature-level aggregate. |
| `PASS` / `FAIL` | Run, review gate or regression aggregate. |

`ACTION_REQUIRED` is the review aggregate when any finding has `severity:
error`; `WARN` means warnings only; `OK` means no findings.

## Requirements extraction — `formal/spec/requirements.md`

This is the one report that is authored by an LLM and then parsed. Its fixed
shape is defined in `prompts/extract.md`. The parser writes
`formal/spec/requirements.json` with:

| Field | Meaning |
|---|---|
| `schema_version` | `"1.0"` |
| `generated_at`, `generated_by` | producer metadata |
| `design.name`, `design.top`, `design.spec`, `design.spec_sha256` | identity and evidence source |
| `design.extraction_sha256` | hash of the reviewed Markdown |
| `features[]` | `id`, `class`, `text`, `source`, `requirements[]` |
| `requirements[]` | `id`, `title`, `feature`, `category`, `text`, `source`, `evidence`, `clarity`, `question`, `owner`, `blocking`, `proposed`, `notes` |
| `stats` | counts of features, requirements, clear/unclear, blocking, errors, warnings, `ready` |
| `errors[]`, `warnings[]` | deterministic findings; errors block the pipeline |

The front matter's `spec_sha256` must match the current `spec.md`. A changed
spec is an extraction error until the file is reviewed against the new text and
the hash is recorded with `fv.py extract --update-spec-hash`. `--force`
regenerates the scaffold and is not the way to accept a reviewed extraction.

## Clarification questions — `formal/reports/questions.json` / `.md`

Producer: `fv_questions.py`; rebuilt by `fv.py extract` and `fv.py questions`.
Consumer: the owner named on each question (spec agent, spec engineer,
architecture engineer, RTL agent or verification engineer).

JSON fields:

| Field | Meaning |
|---|---|
| `schema_version`, `generated_at` | format and time |
| `design` | design name/top/spec identity |
| `overall` | `OPEN` when any question is open, otherwise `CLEAR` |
| `summary.open`, `summary.answered`, `summary.blocking`, `summary.by_owner` | routing counts |
| `questions[]` | see below |
| `answers_file` | path to `formal/spec/answers.json` |
| `inputs.requirements_sha256`, `inputs.contract_sha256` | what the report was built from |

Each question has `id` (`Q-<requirement>-NN`), `requirement`, `feature`,
`severity` (`blocking` or `advisory`), `category` (the clarity value),
`owner`, `question`, `why`, `evidence`, `suggested_answer`, `status`
(`open`/`answered`), `answer` and `answered_at`.

Markdown sections, in order:

1. Title: `# Formal clarification questions: <design>`
2. Overall, open/blocking/answered counts, answers-file path
3. `Open by owner`
4. `## Open questions` with columns
   `ID | Severity | Owner | Requirement | Category | Question`
5. One `###` block per open question with `Owner`, `Why it is open`,
   `Evidence` and optional `Suggested answer`
6. `## Answered questions` with columns `ID | Requirement | Answer`
7. `## How to answer` with the fixed `answers.json` example

A blocking question fails `fv.py extract` and stops `fv.py all` before a
contract is used. Answer it in `formal/spec/answers.json`, or change the
extraction to mark it advisory with a reason.

## Verification results — `formal/results/formal_results.json`

`formal/reports/report.json` is the same document. Consumer: the verification
engineer and the RTL agent (for failures).

| Field | Meaning |
|---|---|
| `schema_version`, `generated_at` | format and time |
| `design` | name, top, clock, reset, resolved parameters |
| `scope` | contract scope, including configuration and documented limits |
| `contract.path`, `contract.sha256` | exact contract that was proven |
| `backend.name`, `backend.kind`, `backend.version` | selected tool |
| `summary` | overall, property/requirement counts, per-status counts, `statuses` map |
| `properties[]` | one row per property |
| `requirements[]` | one row per extracted requirement with disposition and status |
| `features[]` | feature aggregate with counts, gaps, unformalized warnings |
| `assumptions[]` | assumption id, requirement, status, guard, triage |
| `review`, `review_findings` | embedded review snapshot (also written separately) |
| `limitations[]` | fixed statements about bounds, backend and generated SVA |
| `elapsed_s` | wall time |
| `artifacts` | paths to harness, SVA, results, report and JUnit |

Each property carries `id`, `req`, `feature`, `title`, `kind`, `role`, `mode`,
`depth`, `status`, `severity`, `evidence`, `tags`, `triage`, `bmc`,
`induction`, `counterexample[]`, `elapsed_s` and optional `error`. `bmc` and
`induction` each carry `result`, `log`, log line and elapsed time.

Markdown sections, in order:

1. Title `# Formal verification report: <design>`
2. Top, clock, reset, configuration, backend, generated time, overall
3. `## Summary` table
4. `## Feature coverage` table
5. `## Requirement coverage` table
6. `## Partial formalization limits` when present
7. `## Property results` table
8. `## Failures and triage` when present, with counterexample excerpt and logs
9. `## Assumptions` when present
10. `## SVA and constraint review` when present
11. `## Limitations`
12. `## Reproduce`

## JUnit — `formal/reports/junit.xml`

One `<testcase>` per property, with `classname=<design>` and `name=<property>`.
`FAIL` and error-severity `UNREACHABLE`/`VACUOUS` are `<failure>`; `ERROR` and
`TIMEOUT` are `<error>`; advisory unreachable/vacuous are `<skipped>`;
`BMC_PASS`, `COVERED` and `ASSUMED` add a `<system-out>` explaining the bound.

## SVA and constraint review — `formal/reports/sva_review.json` / `.md`

Producer: `fv_review.py`; automatic after `fv.py run` and a required step in
`fv.py all`. Consumer: the verification engineer; `REQ_UNCLEAR_FORMALIZED` is
routed to the spec owner.

JSON fields:

| Field | Meaning |
|---|---|
| `schema_version`, `generated_at` | format and time |
| `design` | name and top |
| `overall` | `OK`, `WARN` or `ACTION_REQUIRED` |
| `summary.findings`, `summary.errors`, `summary.warnings`, `summary.info`, `summary.categories` | counts |
| `findings[]` | fixed finding records |
| `inputs.contract`, `inputs.contract_sha256` | what was reviewed |

Each finding has `id` (`REV-NNN`), `code`, `severity` (`error`/`warning`/
`info`), `category` (`constraint`, `coverage`, `sva`, `traceability`),
`property`, `requirement`, `owner`, `message` and `recommendation`.

Fixed codes: `CONSTRAINT_UNSAT`, `CONSTRAINT_UNGUARDED`,
`CONSTRAINT_CONSTANT`, `COVER_UNREACHABLE`, `COVERAGE_NO_COVER`,
`COVERAGE_NONE`, `TRIGGER_UNCOVERED`, `GUARD_UNCOVERED`,
`PROPERTY_CONSTANT`, `PROPERTY_TAUTOLOGY`, `PROPERTY_DUPLICATE`,
`BOUNDED_ONLY`, `SVA_PARAM_LEFTOVER`, `SVA_TEMPORAL_LEAK`,
`REQ_UNCLEAR_FORMALIZED`.

Markdown sections, in order:

1. Title `# SVA and constraint review: <design>`
2. Top, overall, finding counts, generated time
3. `## Findings` with columns
   `ID | Severity | Category | Property | Code | Message | Owner`
4. `## Recommended actions` with one block per warning/error
5. `## Reproduce`

An `ACTION_REQUIRED` review fails the ordered pipeline (use
`--allow-review-errors` only for an explicit, documented exception). Standalone
`fv.py regress --update --run` returns 1 and leaves the baseline unchanged while
the review is `ACTION_REQUIRED`; `--allow-review-errors` is the only way to
record it. Warnings make it `WARN` and remain actionable. The review never
changes the verification status; it adds findings, recommendations and owners.

## Regression — `formal/reports/regression.json` / `.md`

Producer: `fv_regress.py`. Consumer: CI, reviewer and verification engineer.

| Field | Meaning |
|---|---|
| `schema_version`, `generated_at` | format and time |
| `design`, `contract` | identity and contract hash |
| `contract_changed` | contract hash differs from the baseline |
| `verification_failed` | current run is not `PASS` |
| `overall` | `PASS`, `WARN` or `FAIL` |
| `summary.properties/requirements/features` | change counts |
| `properties[]`, `requirements[]`, `features[]` | per-item comparison rows |
| `baseline` | generation time, overall and contract hash of the baseline |
| `artifacts` | paths to JSON, Markdown and JUnit |

Each comparison row has `id`, `baseline`, `current`, `change`
(`UNCHANGED`, `REGRESSION`, `IMPROVEMENT`, `NEW`, `REMOVED`), `severity` and
`blocking`. Markdown lists overall, contract change, verification overall, a
summary table and one table per item class.

`formal/regression/baseline.json` uses the baseline schema: design, contract,
summary and status-only tables. It never stores logs or counterexamples.

`formal/reports/regression-junit.xml` has one testcase per compared design in
suite mode, or per compared item in design mode; blocking changes are failures.

## Handoff routing

| Reader | Read first | Why |
|---|---|---|
| Spec agent / spec engineer | `questions.md`, then `report.md` requirement coverage | unclear or missing behavior, with evidence and owner |
| Architecture engineer | `questions.md` rows with `category` `incomplete`, `missing_info` or `conflicting` | interface/architecture decisions that block formalization |
| RTL agent | `report.md` failures/triage and counterexample, or `analysis` log | concrete behavior that disagrees with the property |
| Verification engineer | `sva_review.md` | vacuity, unreachable covers, weak constraints, duplicate or trivial checks |
| CI / reviewer | `regression.md` and JUnit | status changes against the committed baseline |

Reports never contain free-form conclusions in place of these fields. If a
reader needs a summary, it is derived from the JSON by a script.
