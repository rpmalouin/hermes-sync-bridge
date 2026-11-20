# Reviewer Contract (author ≠ reviewer)

Distilled from `superpowers:subagent-driven-development` (obra/superpowers, MIT) and
re-mapped to `dsh`. The upstream version assumes a controller that can message live
subagents and spawns them natively; `dsh` runs are one-shot processes, so the mapping
below is stated explicitly. Prose is ours; the failure modes are upstream's.

## Why this exists

`dsh --profile headless` prints its own final answer, and that text is a **self-report**:
unverified, usually optimistic, and the only evidence of the work you get. Two things
follow:

1. A run's own summary never counts as proof the change is correct.
2. The judge must be a **different process with a fresh context** whose only job is to
   read the diff against the requirements.

This file closes the gap that delegation-by-itself leaves open: an independent second
read of every task, gated on both spec compliance and quality.

## Three artifacts, always as files

Never paste artifact bodies into a dispatch prompt, and never let a child's long output
land in the controller's context — it stays resident and is re-read on every later turn.

| Artifact | Produced by | Contents |
|---|---|---|
| **Brief** | controller | the task's requirements, with exact values verbatim |
| **Report** | implementer, written to a file | what was built, tests run + their output, TDD evidence, concerns |
| **Review package** | controller | `git log --oneline BASE..HEAD`, `git diff --stat BASE..HEAD`, `git diff -U10 BASE..HEAD` in one uniquely named file |

## Per-task sequence

1. **Record BASE** — `git rev-parse HEAD` before any dispatch. Never `HEAD~1`: it
   silently drops all but the last commit of a multi-commit task.
2. **Dispatch the implementer** — `dsh --profile headless` in the target workdir. The
   prompt carries: one line on where the task fits; the brief path ("read this first —
   these are your requirements, exact values verbatim"); interfaces from earlier tasks
   the brief cannot know; your resolution of any ambiguity you spotted; the report path
   and the report contract. Status vocabulary: `DONE`, `DONE_WITH_CONCERNS`,
   `NEEDS_CONTEXT`, `BLOCKED`. A dispatch describes one task, not the session history.
   The implementer never dispatches subagents — no helpers, and never a reviewer.
3. **Build the review package** from BASE to HEAD into one file.
4. **Dispatch the reviewer** — a *second* `dsh --profile headless` process,
   `workspace-write`, fresh context, same workdir, same commits.
5. **Gate on both verdicts** — spec compliance (✅/❌/⚠️) *and* task quality
   (Approved / Needs fixes). A review missing either verdict is not a review.
6. **Fix loop** — see below.
7. **Ledger the outcome** — see below.

A non-`DONE` status is handled, never retried blind: `DONE_WITH_CONCERNS` — read the
concerns, act if they are about correctness or scope, proceed if they are observations;
`NEEDS_CONTEXT` — supply the missing context, re-dispatch; `BLOCKED` — change something
(context, model, task size, or the plan itself) before re-dispatching.

## Reviewer dispatch prompt

Copy verbatim, filling the bracketed paths. Do not soften it — each clause kills a
specific failure mode.

```
You are reviewing one task's implementation: first whether it matches its
requirements, then whether it is well-built. This is a task-scoped gate, not a
merge review.

## What Was Requested
Read the task brief: [BRIEF]
Binding constraints from the spec that bind this task: [GLOBAL_CONSTRAINTS]

## What the Implementer Claims
Read the implementer's report: [REPORT]
Treat every line of it as an unverified claim about the code. It may be incomplete,
inaccurate, or optimistic. Design rationales are claims too: "left it per YAGNI" or
"kept it simple deliberately" is the implementer grading their own work — a stated
rationale never downgrades a finding's severity.

## Diff Under Review
Base: [BASE]  Head: [HEAD]
Package: [PACKAGE]   (commit list + diff --stat + diff -U10)
Read the package once — it is your view of the change. Do not re-run git commands.
Do not crawl the codebase: inspect code outside the diff only to evaluate a concrete
risk you can name, one focused check per named risk, and say what you checked.
Cross-cutting changes are legitimate named risks (lock ordering, an API contract,
shared mutable state) — checking call sites is the right method there.

Your review is read-only. Do not mutate the working tree, the index, HEAD, or branch
state. Do not dispatch subagents: a reviewer you spawn duplicates a seat and its
verdict counts for nothing.

## Tests
The implementer ran the suite and reported results for exactly this code. Do not
re-run it to confirm their report. Run a test only when reading the code raises a
specific doubt no existing run answers — then a focused test, never a package-wide
suite. Warnings or noise in their reported test output are findings: output should
be pristine.

## Part 1: Spec Compliance
Compare the diff against What Was Requested: Missing (skipped, or claimed without
implementing), Extra (unrequested features, over-engineering), Misunderstood (right
feature built the wrong way). If the brief lists several files each with its own
change, check file by file: a listed file the diff never touches is Missing, however
clean the rest looks. A requirement you cannot verify from this diff alone is a ⚠️
item — not a licence to broaden the search.

## Part 2: Code Quality
Separation of concerns; error handling; DRY without premature abstraction; edge cases.
Tests: do new and changed tests verify real behaviour rather than mocks? Are the
task's edge cases covered? Structure: one clear responsibility per file, units
understandable and testable independently, the planned file structure followed. Did
this change create files that are already large, or significantly grow existing ones?
Judge what this change contributed, not pre-existing size.

Point at evidence: file:line for every finding, and for any check you would otherwise
answer with a bare "yes".

## Output Format
### Spec Compliance
- ✅ compliant  |  ❌ issues: [missing / extra / misunderstood, with file:line]
- ⚠️ cannot verify from diff: [what, and what the controller should check]
### Strengths
[specific]
### Issues
#### Critical (must fix)
#### Important (should fix)
#### Minor (nice to have)
For each: file:line, what is wrong, why it matters, how to fix if not obvious.
### Assessment
Task quality: [Approved | Needs fixes]
Reasoning: [1-2 sentence technical assessment]

## Calibration
Not everything is Critical. Important means this task cannot be trusted until fixed:
incorrect or fragile behaviour, a missed requirement, or maintainability damage worth
blocking on — verbatim duplication of a logic block, swallowed errors, tests that
assert nothing. "Coverage could be broader" and polish are Minor. If the brief or plan
mandates something this rubric calls a defect, that IS a finding: report it as
Important, labelled plan-mandated — the plan does not grade its own work. Acknowledge
what was done well before listing issues.

Your final message is the report itself: begin directly with the spec-compliance
verdict. Every line is a verdict, a finding with file:line, or a check you ran — no
preamble, no process narration, no closing summary.
```

## Fix loop

- Triggers on: spec ❌, any Critical or Important finding, or a ⚠️ item you confirmed
  as a real gap.
- **Minor findings never enter the loop.** Append them to the ledger and let the final
  review triage them. A roll-up nobody reads is a silent discard.
- **Cap: 5 rounds per task.** Rounds 1–3 go back to the same implementer — `dsh` is
  one-shot, so re-dispatch with the brief path, the report path, the open findings
  verbatim, and "read the report file for what was tried". Rounds 4–5 go to a fresh run
  on a more capable model with the framing "a prior implementer attempted this task N
  times; you own it now". A loop that survives three resumes means the implementer
  cannot see its own problem.
- Before each re-review, confirm the fix report carries the covering tests, the command
  run, and the output. Name the covering test files in the fix message — a one-line fix
  does not need the whole suite.
- **The re-review is scoped**: diff from the head the previous review saw. It verdicts
  each finding `ADDRESSED` / `NOT ADDRESSED` and flags breakage inside the fix diff
  only. New findings on untouched code go to the ledger, never into the loop.
- **Never fix findings in the controller session.** Controller fixes skip review and
  pollute the context that has to coordinate the remaining tasks.
- **At the cap, adjudicate.** The controller holds the plan and cross-task context the
  reviewer lacks. Park it if the reviewer is wrong or the finding is real but nothing
  downstream depends on it; rule on the smallest change that unblocks dependent work if
  it is load-bearing. Adjudicating before the cap is pre-judging with a different name.
  Every ruling is a ledger entry — a silent discard is forbidden.

## Ledger

The ledger is what survives compaction. One line per event, appended in the same
message as your other bookkeeping:

- `Task N: complete (commits a7..b7, review clean)`
- `Task N: complete (commits a7..b7, K parked)`
- `Task N: fix round R/5 (X addressed, Y open — <one-liners>; commits a7..b7)`
- `Task N: minor (deferred): <one-liner>`
- `Task N: parked — <finding> — Ruling: <why the code stands>`

## Hard rules

1. Never dispatch a reviewer without a diff file.
2. Never tell a reviewer what not to flag — "do not flag", "at most Minor", "the plan
   chose it" is pre-judging, usually to spare yourself a review loop. Let the finding
   be raised and adjudicate it.
3. An implementer's self-review never replaces the task review. Both, every task.
4. Evidence you cannot see is not evidence that does not exist: a truncated or
   unreadable report is a gap to report, not a licence to re-run the suite to
   regenerate it.
5. Plan-mandated defects are still defects — label them, do not delete them.

## Calibration record

A prompt nobody graded is not a prompt. A deliberately bad work item was planted on the
dockerVM host (`mul` implemented correctly, `test_mul` ending in `assert True`, no
negative-operand case, a stray tracked `notes.txt`) and reviewed blind. The reviewer filed
the non-asserting test as a blocker, the missing negative coverage as major, the stray file
as scope creep, and disclosed its own interpreter workaround instead of hiding it — it
graded the diff against the brief, not against the implementer's claim. On this host the
same day, the mode matrix below was measured on a clean one-test repo before either mode
was written into this file.

What that proves is the shape, not the wording: a reviewer told to cite `file:line` and
return two separate verdicts produces graded findings rather than prose, and a reviewer
denied execution retreats to UNABLE TO VERIFY instead of inventing test results. Re-run a
planted diff whenever the prompt changes.

## dsh specifics

- **Sandbox mode: `workspace-write`.** The modes are `read-only` | `workspace-write` |
  `danger-full-access`, selected through the variable whose name is `DSH_PERMISSION`
  joined with `_MODE`. `read-only` is unusable for a reviewer: writes are blocked so
  completely that pytest cannot even start — it dies inside `tempfile` with
  `FileNotFoundError: [Errno 2] No usable temporary directory found in
  ['/var/folders/.../T/', '/tmp', '/var/tmp', '/usr/tmp', '<cwd>']` before a single test
  is collected. Every execution-based check then becomes UNABLE TO VERIFY, and a reviewer
  that cannot run the gates is a reader, not a reviewer. Measured here 2026-10-27 on a
  clean one-test repo: `workspace-write` → `pytest -q .` reported `1 passed in 0.00s`,
  `git rev-parse HEAD` unchanged, tracked tree clean, only `?? __pycache__/` left behind.
- **`read-only` fails closed** (same day): a write is denied with
  `[sandbox: file access denied under read-only mode]`, dsh offers a `workspace-write`
  escalation, and a headless run has no approval channel, so it dies with
  `sandbox escalation to "workspace-write" requires approval, but no approval channel is
  available`. Spend that mode only on a diff-reading pass whose ledger line says outright
  that no test was run.
- **No-edit is enforced by verification, not by permission.** Under `workspace-write` the
  reviewer *can* write, so prove it did not: capture `git rev-parse HEAD` and
  `git status --porcelain -uno` before the run and again after, and require both to be
  identical. Dirt on a **tracked** file voids the review. Untracked cache artifacts
  (`__pycache__/`, `.pytest_cache/`) are the expected residue of running tests, not dirt;
  the orchestrator removes them afterwards, deliberately, never the child.
- **Credentials:** a headless child needs `DEEPSEEK_API_KEY`. Here dsh loads it from
  `~/.hermes/.env` itself (verified: the probe runs exported nothing). The dockerVM host
  had to export it from `/root/.hermes/.env` or the child died at `MISSING_CREDENTIAL` —
  state the requirement whenever the host is not this one.
- **Quoting quirk:** the mode variable's name, written contiguously inside a shell
  command, is rewritten by the egress sanitizer and bash then reports `command not found`.
  Build it from parts — which is also why this file spells it as two words:
  `K="DSH_PERMISSION""_MODE"; env "$K=workspace-write" dsh --profile headless '...'`.
- **Parallelism:** never run two implementer tasks in parallel in one checkout.
  Direct commits to `main`, no branches and no PRs, is the house policy here, so the
  upstream worktree-per-task step does not apply — serialize. Worktrees are only for
  genuinely independent batch work (see SKILL.md "PR / Batch Work").
- **Cost:** two model runs per task, up to five fix rounds. For a trivial one-file edit,
  state in one line why the review collapses to a single pass — never skip it silently.
- **Timeouts:** real tasks can exceed 10 minutes; dispatch with `background=true` and poll
  per SKILL.md "Background Mode".
