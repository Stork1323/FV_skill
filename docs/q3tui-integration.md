# Using formal-sva standalone or inside Q3TUI

The skill is self-contained. The deterministic half is stdlib-only Python 3.9+;
the LLM half is two prompts, one for extraction and one for the contract. The
same scripts, prompts, schemas, templates and rule sources run in a plain
checkout, in CI, or under a Q3TUI folder flow.

Nothing in this repository imports Q3TUI. A Q3TUI integration adds adapters
around the CLI; it does not fork the skill.

## Entry points

| Task | Command |
|---|---|
| Prepare a project from `spec.md` + RTL | `fv.py init --project DIR --top TOP --spec /path/spec --rtl /path/rtl` |
| Fill/review the extraction | LLM reads `prompts/extract.md` and writes `formal/spec/requirements.md` |
| Validate extraction and open questions | `fv.py extract --design FILE` |
| Write the contract | LLM reads `prompts/contract.md` and `formal/spec/requirements.json` |
| Run everything in order | `fv.py all --design FILE` |
| Re-run only verification | `fv.py run --design FILE` |
| Rebuild the report from results | `fv.py report --design FILE` |
| Rebuild the SVA/constraint review | `fv.py review --design FILE` |
| Compare with the baseline | `fv.py regress --design FILE` |
| Check the toolchain | `fv.py doctor --design FILE` |

`fv.py all` is `rules -> extract -> contract -> render -> run -> review ->
regress`. It records each step in `formal/state/pipeline.json`; `fv.py status`
shows what is current, missing or stale.

## Independent use

A project only needs:

```
spec/spec.md              # or any path passed with --spec
src/rtl/filelist.f        # or a directory / explicit RTL sources
```

Initialisation creates `formal/design.json` and `formal/tools.json`, generates
`formal/rules/`, and scaffolds `formal/spec/requirements.md`. For a spec-only
project the scaffold has no requirements yet, so `init` stops after creating it.
The extraction prompt is the only way behavior enters the flow:

1. Read the specification (not the RTL) and fill one section per requirement.
2. Run `fv.py extract`; fix every error and answer every blocking question.
3. Write the contract from `formal/spec/requirements.json`.
4. Run `fv.py all --jobs N --update-baseline` for the first passing run.
5. Read `report.md` and `sva_review.md` before committing the baseline.

`schemas/structured_spec.json` and `schemas/final_config.json` are optional
Q3TUI conveniences. When present, `init`/`extract` can prefill the extraction
from them, but the evidence is still checked against `spec.md`; a mismatch is an
error.

## Q3TUI folder-flow shape

Recommended layout:

```
q3tui/flows/formal/
  FLOW.md
  setup/      step.py + md/skill.md
  extract/    step.py + md/skill.md      # LLM
  contract/   step.py + md/skill.md      # LLM
  prove/      step.py + md/skill.md
  review/     step.py + md/skill.md
  signoff/    step.py + md/skill.md
  lib/        this skill's scripts/, prompts/, schemas/, templates/, rules/
```

```markdown
---
name: formal
title: Formal verification
session: flow
steps: [setup, extract, contract, prove, review, signoff]
---

Extract requirements from the specification, generate traceable SVA, prove it
with the configured backend, review the checks, and compare with the baseline.
```

| Step | LLM? | Deps | Calls |
|---|---|---|---|
| `setup` | no | spec/config/RTL | `init` once, then `extract`; `tools --list` |
| `extract` | **yes** | setup | fill `requirements.md`, then `extract` |
| `contract` | **yes** | extract | fill `formal_contract.json`, then `validate` |
| `prove` | no | contract | `run` (includes review) |
| `review` | no | prove | `review`, fail when `ACTION_REQUIRED` |
| `signoff` | no | review | `regress`, explicit `--update` |
| gate before extract | no | extract | blocking `questions.md` fails the gate |

If the flow prefers one verification step, let `prove` call `run` (which
performs the review automatically) and keep `review` as a gate that reads
`sva_review.json`; do not allow signoff when the review is `ACTION_REQUIRED`.

## Setup step

`setup` must be idempotent. Use `init` only when `formal/design.json` is
missing; otherwise use `extract`, which re-parses the reviewed file and never
overwrites the manifest. `prepare` and `validate` later rebuild the bundle from
the current extraction and RTL, so setup does not need to bundle an empty
extraction.

```python
"""formal flow: initialise the manifest or refresh the bundle."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from q3tui.pipeline.base import StepContext, StepDef, StepFailed

LIB = Path(__file__).resolve().parents[1] / "lib"


def run_skill(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LIB / "fv.py"), *args],
        cwd=str(root), text=True, capture_output=True, timeout=3600,
    )


class Step(StepDef):
    name = "setup"
    title = "Formal setup"

    def _formal(self, engine) -> Path:
        return engine.project.root / "formal"

    def inputs(self, engine) -> list[Path]:
        return [
            engine.project.spec_dir / "spec.md",
            engine.project.rtl_dir / "filelist.f",
        ]

    def outputs(self, engine) -> list[Path]:
        f = self._formal(engine)
        return [f / "design.json", f / "tools.json",
                f / "rules" / "manifest.json",
                f / "spec" / "requirements.md",
                f / "spec" / "requirements.json"]

    async def run(self, ctx: StepContext) -> None:
        root = ctx.project.root
        design = self._formal(ctx.engine) / "design.json"
        if design.is_file():
            args = ["extract", "--design", str(design)]
        else:
            args = ["init", "--project", str(root), "--spec", str(
                ctx.project.spec_dir / "spec.md"), "--rtl", str(
                ctx.project.rtl_dir)]
        proc = run_skill(root, *args)
        for line in (proc.stdout + proc.stderr).splitlines():
            ctx.emit("log", message=line)
        if proc.returncode != 0:
            raise StepFailed("formal setup failed; see the log above")
```

Notes:

* `init` prints an inferred clock/reset with a warning. Review
  `formal/design.json` once; a wrong reset polarity invalidates every proof.
* `init` does not fail because a spec-only project has no requirements yet.
  The next step is the extraction, not an empty bundle.
* `fv.py doctor --design ...` is a cheap precondition check. It reports every
  backend in `formal/tools.json` and whether its executable exists.

## Extract step (LLM)

The extraction prompt is `prompts/extract.md`. The model may write only
`formal/spec/requirements.md`; `write_files=[requirements]` enforces that.

The deterministic parser then:

* checks every feature, source line and verbatim evidence quote against
  `spec.md`;
* rejects unknown clarity/proposed values and TODO markers;
* refuses a changed `spec_sha256` until the extraction is reviewed again and
  `fv.py extract --update-spec-hash` records the accepted spec;
* writes `formal/spec/requirements.json` and
  `formal/reports/questions.{json,md}`;
* exits non-zero while a blocking question is open.

```python
class Step(StepDef):
    name = "extract"
    title = "Requirement extraction"
    deps = ("setup",)

    def outputs(self, engine) -> list[Path]:
        f = engine.project.root / "formal"
        return [f / "spec" / "requirements.md",
                f / "spec" / "requirements.json",
                f / "reports" / "questions.json",
                f / "reports" / "questions.md"]

    async def run(self, ctx: StepContext) -> None:
        root = ctx.project.root
        design = root / "formal" / "design.json"
        reqs = root / "formal" / "spec" / "requirements.md"

        # 1. If the extraction does not exist, scaffold it first.
        if not reqs.is_file():
            proc = run_skill(root, "init", "--project", str(root),
                             "--spec", str(ctx.project.spec_dir / "spec.md"),
                             "--rtl", str(ctx.project.rtl_dir))
            if proc.returncode != 0:
                raise StepFailed(proc.stderr.strip() or "scaffold failed")

        # 2. LLM writes only requirements.md, guided by prompts/extract.md.
        system = (LIB / "prompts" / "extract.md").read_text()
        prompt = f"""Extract the formal requirements for this design.

Spec:     {ctx.project.spec_dir / 'spec.md'}
Output:   {reqs}

Read the whole specification, then write the complete reviewed extraction
file. Return only a one-line summary when done."""
        stage = Stage(
            name="formal_extract",
            system_prompt=system,
            prompt=prompt,
            cwd=root,
            builtin_tools=["Read", "Grep", "Glob", "Write"],
            write_files=[reqs],
            max_turns=40,
        )
        await ctx.llm(stage)

        # 3. Deterministic parse, evidence check and clarification report.
        proc = run_skill(root, "extract", "--design", str(design))
        for line in (proc.stdout + proc.stderr).splitlines():
            ctx.emit("log", message=line)
        if proc.returncode != 0:
            raise StepFailed(
                "extraction is invalid or has blocking questions; "
                "read formal/reports/questions.md")
```

The question report is the fixed feedback format for the spec agent, spec
engineer or architecture engineer. Do not let the flow continue to a contract
with an open blocking question.

## Contract step (LLM)

The model reads `formal/spec/requirements.json`,
`formal/reports/questions.json`, `formal/schemas/input_bundle.json`, `spec.md`
and `prompts/contract.md`. It writes only
`formal/schemas/formal_contract.json`.

`fv.py validate` (and `fv.py all`) rebuilds the bundle from the current
extraction before validating, so a stale bundle cannot pass the gate. The
validator checks:

* every requirement appears exactly once with a disposition;
* non-clear requirements are not `formalized` (`REQ_UNCLEAR_FORMALIZED`);
* every disposition and property quotes evidence found in the spec;
* only top-level ports, resolved integer parameters and the portable functions
  are used;
* partial formalization carries a `limitations` sentence;
* `scope.configuration` matches the elaborated parameters.

Retry the LLM with the validator's exact error list. `ctx.feedback` is the
right channel for a reviewer-requested change; never hand-edit the contract
outside the step.

## Prove step

```python
class Step(StepDef):
    name = "prove"
    title = "Formal proof"
    deps = ("contract",)

    def outputs(self, engine) -> list[Path]:
        f = engine.project.root / "formal"
        return [f / "results" / "formal_results.json",
                f / "reports" / "report.md",
                f / "reports" / "junit.xml",
                f / "reports" / "sva_review.json"]

    async def run(self, ctx: StepContext) -> None:
        design = ctx.project.root / "formal" / "design.json"
        proc = run_skill(ctx.project.root, "run", "--design", str(design),
                         "--jobs", str(self.options.get("jobs", 4)))
        for line in (proc.stdout + proc.stderr).splitlines():
            ctx.emit("log", message=line)
        if proc.returncode != 0:
            raise StepFailed("formal verification failed; open "
                             "formal/reports/report.md and sva_review.md")
```

`run` performs the SVA/constraint review automatically. If the flow has a
separate `review` step, call `fv.py review` and fail on
`ACTION_REQUIRED`; never sign off on warnings without reading them.

## Signoff step

```python
class Step(StepDef):
    name = "signoff"
    title = "Formal regression"
    deps = ("review",)

    def outputs(self, engine) -> list[Path]:
        r = engine.project.root / "formal" / "reports"
        return [r / "regression.json", r / "regression.md",
                r / "regression-junit.xml"]

    async def run(self, ctx: StepContext) -> None:
        design = ctx.project.root / "formal" / "design.json"
        args = ["regress", "--design", str(design)]
        if self.options.get("update_baseline"):
            args.append("--update")
        proc = run_skill(ctx.project.root, *args)
        for line in (proc.stdout + proc.stderr).splitlines():
            ctx.emit("log", message=line)
        if proc.returncode != 0:
            raise StepFailed("formal regression failed; open "
                             "formal/reports/regression.md")
```

Policy:

* First run: no baseline. `fv.py all` refuses to finish until an explicit
  `--update-baseline` after the results and review have been read.
* A failing run, or a review with `ACTION_REQUIRED`, never updates the baseline
  without the documented `--force` / `--allow-review-errors` exception. The
  standalone path `fv.py regress --update --run` returns 1 and leaves the
  baseline unchanged while the review requires action.
* A contract change is reported but the status comparison is authoritative.
* Commit `design.json`, `tools.json`, `requirements.md`, `answers.json` (when
  used), `formal_contract.json` and the baseline together.

## Feedback and report routing

| Report | Consumer | Action |
|---|---|---|
| `questions.md` | spec agent / spec engineer / architecture engineer | answer the named decision in `answers.json` |
| `report.md` requirement coverage | spec agent | confirm dispositions and documented limitations |
| `report.md` failures and triage | RTL agent / verification engineer | inspect counterexample, RTL or property |
| `sva_review.md` | verification engineer | fix vacuity, unreachable covers or weak constraints |
| `regression.md` / JUnit | CI / reviewer | block a weaker status, approve a baseline update intentionally |

The exact fields and Markdown sections are in `docs/reports.md`.

## Backends

`formal/tools.json` defaults to SymbiYosys. The `yosys` backend is a
dependency-free fallback. A commercial tool such as VC Formal is a generic
`kind: "command"` backend: fill `command`, `script_template`,
`script_extension`, `result_pattern` and `status_map`. A log line that does not
match the pattern is `ERROR`, never a pass. Record the PASS/FAIL logs used to
validate the pattern in the project's validation notes.

Selection order: `--backend`, then `formal/design.json` `formal.backend`, then
`formal/tools.json` `default`. The checked-in counter fixture pins `yosys`
because the test machine has no `sby`; new projects get `symbiyosys`.

## Vendoring and staleness

Copy the skill directories verbatim:

```
scripts/    -> q3tui/flows/formal/lib/
prompts/    -> q3tui/flows/formal/lib/prompts/
schemas/    -> q3tui/flows/formal/lib/schemas/
templates/  -> q3tui/flows/formal/lib/templates/
rules/      -> q3tui/flows/formal/lib/rules/
SKILL.md    -> q3tui/flows/formal/lib/SKILL.md   (reference)
```

Include in each step's `inputs`:

| Step | Inputs | Outputs |
|---|---|---|
| setup | spec.md, final_config, RTL filelist/sources | design.json, tools.json, rules manifest, requirements.md scaffold, requirements.json |
| extract | spec.md, requirements.md, design.json | requirements.json, questions.{json,md} |
| contract | requirements.json, requirements.md, spec.md, design.json, bundle | bundle, interface, formal_contract.json |
| prove | contract, design.json, bundle, RTL sources, rules manifest, tools.json | results, report, JUnit, sva_review |
| review | results, contract, generated SVA | sva_review.{json,md} |
| signoff | results, review, contract, baseline | regression.{json,md}, regression JUnit |

Use the skill's `source_files()` (or include every RTL source) so an edited
`.sv` invalidates the proof. Do not include `formal/results/*` as a contract
input: a failed proof is not a contract change.

## Mergeability

* The scripts and prompts contain no Q3TUI imports, no network access and no
  dependencies outside the Python standard library.
* All writes stay under the project's `formal/` directory and the paths in
  `design.json`.
* Generated files are marked and overwritten; reviewed files are explicit.
* The Q3TUI repository is not modified by running the skill. Integration code
  lives in the new flow folder, and the existing project tree is untouched.

## CI without Q3TUI

```bash
python3 q3tui/flows/formal/lib/fv.py all \
  --design build/formal/design.json --jobs 8 --update-baseline   # first run only
python3 q3tui/flows/formal/lib/fv.py all --design build/formal/design.json --jobs 8
```

`fv.py regress --suite suite.json --run` covers several designs with one exit
code. `doctor` and `validate --json` are suitable preflight checks. Exit codes:
`0` success, `1` verification/review/regression failure, `2` usage/setup error.
