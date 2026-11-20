---
name: deepseek-harness
description: "Use for any coding task; delegates to the local dsh CLI."
version: 1.3.0
author: Ron Malouin
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Coding-Agent, DeepSeek, dsh, Delegate, Code-Review, Refactoring, Mandatory-Coding-Skill]
    related_skills: [opencode, hermes-agent]
---

# DeepSeek Harness (dsh)

Delegate coding tasks to the local [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
(`dsh`) CLI running at `/Users/ron/Data/Development/deepseek-harness`
(real path `/Volumes/BigData/Development/deepseek-harness`; `/Users/ron/Data/Development`
is a symlink to `/Volumes/BigData/Development`, so `pwd` inside the repo reports the
`/Volumes/BigData/...` form).

## Mandatory Policy

- **ALWAYS use for coding tasks**: This skill MUST ALWAYS be used when performing any coding task (building features, refactoring, issue fixing, code reviews, writing or modifying code, multi-step engineering tasks).
- **ALWAYS use direct DeepSeek API Key & Models**: All tasks MUST run using the direct DeepSeek API key (`DEEPSEEK_API_KEY`) pointing directly to official DeepSeek models (`deepseek-official` provider route). Never route through OpenRouter or third-party proxies.

## Pinned versions and the per-host profile

Both tools are pinned in `hermes-sync-bridge/config/toolchain-pin.yaml`. Read the pin instead of
picking a version per session. As of 2026-11-20: code-review-graph **2.4.0** (graph schema 15; the
9 -> 15 migration is forward-only, so both hosts move in one window), dsh at fork master
**`8255317b0f`** = **0.2.2-rc.5**, node >= 24.9.0, pnpm 11.28.5.

| | Mac | dockerVM |
|---|---|---|
| wrapper | `~/.local/bin/dsh` (a copy, not a symlink) | `/usr/local/bin/dsh` |
| checkout | `~/Data/Development/deepseek-harness` | `/appdata/deepseek-harness` |
| node | `/opt/homebrew/bin/node` | `/root/.hermes/tools/node-*-linux-x64/bin/node`, off every login PATH |
| permission mode | `workspace-write` works here | `danger-full-access` only; workspace-write has no usable backend |
| key file | `~/.hermes/.env` | `/root/.hermes/.env` |
| CRG runtime | `~/.local/venvs/crg/bin/python3` | `/usr/bin/python3` (PEP 668: pip needs `--break-system-packages`) |
| dsh graph wiring | `~/.dsh/cordis.patch.yml` | same file, same shape |

Rules earned by running it on both hosts:

- Install the wrapper from `shared/bin/dsh-wrapper.sh`; never hand-edit the installed copy. It
  resolves node explicitly because node is off cron's PATH on dockerVM, and it always runs the
  built CLI — `pnpm dsh` in a tree with built `lib/` loads a second `@deepseek-ai/dsh-tools` and
  every tool call dies.
- `env -i <wrapper> --version` is the acceptance probe. `env -i PATH= <wrapper> --version` fails
  on both hosts with `/usr/bin/env: 'bash': No such file or directory` because the shebang
  resolves bash through PATH; that is not a wrapper defect.
- Write the dsh graph row into `~/.dsh/cordis.patch.yml` by hand (`python -m code_review_graph
  serve` from the pinned interpreter). `code-review-graph install --platform dsh` on 2.4.0 still
  emits `command: uvx`, which spawns a second, unmanaged copy next to the venv one.
- Never copy a wrapper over `bin.js` with `cp`: `cp` follows the symlink and overwrites the build
  output. `rm` the link first, then copy.
- The Mac's harness graph carries a `.code-review-graphignore` dropping `*.i18n.yaml`,
  `package.json`, `tsconfig*.json` and `locale/` — 241,461 nodes -> 182,649 at the same commit.
  Compare node counts only against a host with the same ignore set.

## When to use

- Building features and writing new code
- Refactoring and codebase restructures
- Issue fixing and bug remediation
- Code review, analysis, and software engineering tasks
- Any programming or development task requested by the user

## Prerequisites

- Read the engine pins from the tree, never from this file: `grep -A3 engines package.json`
  and `grep packageManager package.json`. A release bump raises them (0.2.x needs
  node ≥24.9.0), and the skill's numbers go stale the day it lands.
- `pnpm` must be the version the root `packageManager` pin names: `corepack enable`, then
  `corepack prepare pnpm@<pin> --activate`. A pnpm older than the pin fails the install on
  lockfile drift, not with a message that names the cause.
- Checkout at `/Users/ron/Data/Development/deepseek-harness` installed and built:
  `cd /Users/ron/Data/Development/deepseek-harness && pnpm install && pnpm run build`
- Direct `DEEPSEEK_API_KEY` configured in the environment (stored in `~/.hermes/.env` and `~/.dsh/.credentials.yaml`). Provider credentials resolve from the inherited environment, `$DSH_HOME/.credentials.yaml`, the invoking dir's `.env`, then `$DSH_HOME/.env`.
- Binary at `~/.local/bin/dsh` (ensure `~/.local/bin` is on PATH or invoke `~/.local/bin/dsh`).
- TUI profile installed: `dsh plugin --profile tui add @deepseek-harness-tui/dsh-tui`
- `deepseek` alias in `.zshrc`: `alias deepseek='dsh --profile tui'`

## State check before dispatch

The checkout is a pinned release, not a branch kept current with upstream. Before the
first `dsh` run of a session, prove the binary matches the tree you are about to hand it:

```
git -C /Users/ron/Data/Development/deepseek-harness log -1 --format='%h %s'
~/.local/bin/dsh --version                                        # must name the same release
find apps/cli/src -newer apps/cli/lib/bin.js -name '*.ts' | head  # empty = build is current
git status -sb | head -2                                           # M/A lines are local edits
```

- **Rebuild, do not re-pull, when the tree carries local edits.** `pnpm run build`
  makes the binary current with the working tree; `git pull` on a checkout whose
  `AGENTS.md`/`.gitignore` were written by tooling conflicts and can drop those edits.
  Pull only on an explicit instruction, then `pnpm install && pnpm run build` and
  re-prove `dsh --version` against the new HEAD.
- **Being behind upstream is not a defect.** A pinned RC that passes the checks above is
  a valid base for delegated work. Report the divergence (`git status -sb` prints
  `behind N`) as context, never as a reason to stop.
- **A stale build fails quietly.** `~/.local/bin/dsh` is a symlink into
  `apps/cli/lib/bin.js`; the CLI runs whatever that built file last held, so src newer
  than the binary means the version banner lies about what the agent will run.

## Which checkout you are on (the fork)

This checkout tracks the personal fork `rpmalouin/deepseek-harness`, not upstream directly:
`origin` = the fork, `upstream` = `deepseek-ai/deepseek-harness`. The fork carries the
graph-first agent scaffolding and owns the sync procedure in its own `FORK.md` — read that
file before moving the tree, because a stock upstream pull silently drops what makes the
checkout useful here.

- **Move forward by merging, not pulling:** `git fetch upstream` → `git checkout master` →
  `git merge upstream/master` → `git push origin master`.
- **`pnpm install` before committing a sync merge.** Upstream's pre-commit gates execute
  from `node_modules`; a tree left over from the previous sync fails on third-party notices.
- **Doc gates reject additions that ignore them:** root `AGENTS.md` has a word cap
  (`verify-doc-budgets`), prose is one physical line per paragraph (`verify-md-wrap`), and
  README edits are bilingual pairs (`verify-translation-pairing`). Resolve conflicts by
  keeping both sides, then re-run the gate.
- **Resetting to a fork tip is lossless only when proven:** `git fetch origin --tags`, then
  `git merge-base --is-ancestor HEAD origin/master` before `git reset --hard origin/master`.
  Take a backup branch anyway; a `behind N` status alone does not prove ancestry.
- **Report `dsh --version` and `grep -m1 '"version"' package.json` as a pair.** A mismatch
  means the built binary or the tree is not what the run will use.
- **A second host runs the same fork, and neither can reuse the other's build.** The dockerVM
  keeps its own checkout (`/appdata/deepseek-harness`) on the same SHA, driven by its own
  wrapper at `/usr/local/bin/dsh`; `node_modules` and `lib/` are platform-specific, so parity
  means *same commit, built locally on each host* — never a copied tree. Compare
  `dsh --version` plus `git -C <checkout> rev-parse HEAD` on both before believing a task ran
  on the version you pinned, and read `engines`/`packageManager` from each tree rather than
  assuming the two agree.

## Upgrading the checkout to a new fork tag

Reset only against a proven ancestor, then rebuild — the gates below are what actually fail.

```
cd <checkout>
git stash push -u -m "pre-upgrade local files"          # untracked integration files
 git branch backup-pre-upgrade-$(date +%Y%m%d)
git remote rename origin upstream && git remote add origin https://github.com/rpmalouin/deepseek-harness.git
git fetch origin --tags
git merge-base --is-ancestor HEAD origin/master && echo ancestry-ok
git reset --hard origin/master
corepack prepare pnpm@<the packageManager pin> --activate
CI=true pnpm install --frozen-lockfile
pnpm run build && dsh --version
```

- **`CI=true` is mandatory when the pnpm major changes.** pnpm wants to purge
  `node_modules` and asks for confirmation; with no TTY it aborts with
  `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` and installs nothing. The env var is the
  documented CI switch for exactly this, not a workaround.
- **`stash -u` before `reset --hard`.** The reset overwrites paths that were untracked in
  the old tip but tracked in the new one — on this fork that is the whole graph-first
  scaffolding (`.agents/skills/`, `.cursorrules`, `.gemini/`, `.codebuddy/`), so without a
  stash the local copies are gone with no diff to re-apply.
- **Untracked ≠ dirty.** After the reset `git status --porcelain` must be empty; whatever
  remains is drift the fork itself does not carry, and it belongs in the stash, not in the
  tree.
- **Prove the upgrade with four checks, never the build's own summary:** `dsh --version`,
  `grep -m1 '"version"' package.json`, `find apps/cli/src -newer apps/cli/lib/bin.js -name
  '*.ts' | head` (empty), and one headless run that writes a file and runs bash
  (`DSH_PERMISSION_MODE=workspace-write dsh --profile headless '…'`, exit 0). Perception
  mode is host-specific: workspace-write has a working backend on macOS and none on the
  dockerVM, where `danger-full-access` is the only usable preset.
- **Install the wrapper with `rm` first, never `cp` onto the existing path.**
  `~/.local/bin/dsh` was a symlink into `apps/cli/lib/bin.js`; `cp wrapper.sh
  ~/.local/bin/dsh` writes *through* the link and replaces the built artifact with shell
  text, which node then rejects as `SyntaxError: Invalid or unexpected token`. Remove the
  link, copy, `chmod 755`, then re-prove with `--version` both normally and under `env -i`.
- **The launchd web job pins its own interpreter and goes stale.** An argv[0] of
  `/Users/ron/.hermes/node/bin/node` keeps an old runtime (v22 against a 24.9.0 engine
  floor) and crash-loops under KeepAlive after an upgrade. Point argv[0] at a node that
  satisfies the tree's `engines`, `plutil -lint` the plist, and prove the argv by hand
  before bootstrapping — the gateway guard refuses `launchctl bootstrap` from inside a
  Hermes session, so that one line is the user's to run in a Terminal.
- **The web profile mints a per-launch token.** Bare `http://127.0.0.1:3080/` answers 401;
  the startup line `dsh web: http://127.0.0.1:3080/?token=…` is the only usable URL, so a
  saved bookmark breaks on any release that adds auth.

## One-Shot Tasks

`dsh` is at `~/.local/bin/dsh` (symlinked from the repo's built binary).

```
terminal(command="dsh --profile headless 'Add dark mode toggle to settings'", workdir="/Users/ron/Data/Development/deepseek-harness", timeout=600)
```

`--profile headless "<task>"` builds a fresh persisted Agent, submits the task,
prints the final answer on stdout, and exits 0 on `completed`, 1 otherwise.
The invoking directory is the workspace root, and applicable `AGENTS.md`/`CLAUDE.md`
instructions are loaded (65,536-byte render budget).

## TUI Interactive Mode

```
terminal(command="deepseek", workdir="/Users/ron/Data/Development/deepseek-harness", pty=true, timeout=600)
```

Launches the `dsh-tui` terminal UI (Ink-based) via the `deepseek` alias. Requires
a real interactive terminal (PTY=true). Use for open-ended agent tasks with
interactive approval prompts.

## Background Mode (Long Tasks)

```
terminal(command="dsh --profile headless 'Refactor the auth module'", workdir="/Users/ron/Data/Development/deepseek-harness", background=true)
# Returns session_id

process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")
process(action="kill", session_id="<id>")
```

No PTY needed for headless — it writes plain stdout. Use `pty=true` only if a run
unexpectedly waits on stdin.

## Review gate (author ≠ reviewer)

A `dsh` run's final output is a self-report. Every delegated coding task gets a second,
independent read by a **separate `dsh` process with a fresh context** before you call the
work done — gated on both spec compliance and code quality.

Minimum sequence: record `BASE=$(git rev-parse HEAD)` → dispatch implementer with a brief
file and a report file → build one diff package `BASE..HEAD` → dispatch reviewer with
`workspace-write` → fix loop (cap 5, scoped re-review each round) →
ledger line.

No reviewer means the task is **implemented, unreviewed** — that phrase goes in the note,
the card and the reply. Never "done". The reviewer runs `workspace-write`, because
`read-only` here cannot even start pytest, and the no-edit rule is then proven by
`git rev-parse HEAD` + `git status --porcelain -uno` being identical before and after.

Full contract, reviewer prompt template, fix-loop caps and ledger format:
[references/reviewer-contract.md](references/reviewer-contract.md).

## Configuration

| Mechanism | Effect |
|-----------|--------|
| `--profile headless` | One-shot run, no server/web/browser |
| `DSH_PERMISSION_MODE` | Process-wide permission preset fallback (e.g. `workspace-write` default, `danger-full-access`) |
| `DSH_TOOLS_MODE` | `native` \| `code` \| `both` for the process |
| `--dump-config` | Print the composed tree without booting (requires `--profile`) |
| `$DSH_HOME` | Harness home (default `~/.dsh`) |

Default permissions confine bash/filesystem mutations to the session workspace
and platform temp roots; reads, network, and process visibility are not confined.

## MCP servers and config layers

`dsh` composes its plugin tree from YAML patch layers, and on this build the user layer
is **per profile**: `~/.dsh/profiles/<profile>/cordis.patch.yml` — a top-level YAML array
of patch operations, `[]` until you add one. An MCP server is a single `insert` row
naming `@deepseek-ai/dsh-mcp-client`, and its tools reach the model as
`mcp__<serverName>__<toolName>`. Prove a row loaded with
`dsh --profile <name> --dump-config` rather than trusting that the file was read.

Row shape, the `streamable-http` variant, and the home-level-layer trap that makes an
installer's wiring a silent no-op: [references/mcp-wiring.md](references/mcp-wiring.md).

## Direct API Key & Model Configuration

`dsh` uses `deepseek-official` provider route by default, which reads `DEEPSEEK_API_KEY`.
Do NOT use OpenRouter patches or third-party provider overlays.

- Key Location: `~/.hermes/.env` and `~/.dsh/.credentials.yaml`
- Provider Route: `deepseek-official`
- Default Model: Official DeepSeek models (e.g., `deepseek-v4-flash`, `deepseek-reasoner`, `deepseek-chat`)

## PR / Batch Work

dsh treats the cwd as the workspace root, so point it at the right directory and
review the diff afterward. It does not require a git repo, but for safety on
existing repos verify with `git diff` and targeted tests before committing. For
parallel work use git worktrees and one background `dsh --profile headless` per
worktree.

## Diagnosing

- **`MISSING_CREDENTIAL: llm-deepseek: no API key for provider route "deepseek-official"`**
  — no `DEEPSEEK_API_KEY`. Ensure `DEEPSEEK_API_KEY` is exported in `~/.hermes/.env` or present in `~/.dsh/.credentials.yaml`.
- **`ENOENT ... lib/bin.js`** during `pnpm install` — expected before the first
  `pnpm run build`; re-run `pnpm run build` and the bins resolve.
- **`node: not found` from a cron job or `env -i` shell** — a wrapper that execs a bare `node`
  only works inside a session whose PATH carries the interpreter; on dockerVM node lives at
  `/root/.hermes/tools/node-*-linux-x64/bin/node` and no login shell has it. Resolve the
  interpreter explicitly (DSH_NODE, then the highest Hermes-managed node, then system paths,
  then PATH) and default `HOME`, or the failure reads as a broken checkout when it is a
  broken PATH. Prove it with `env -i <wrapper> --version`, never from an interactive shell.
- **Tool calls dying with `Cannot read properties of undefined (reading 'prepare')`** — `pnpm dsh`
  (tsx source launch) in a tree that already has built `lib/` output loads a second copy of
  `@deepseek-ai/dsh-tools`. Run the built CLI (`apps/cli/lib/bin.js`) through a wrapper instead.

## Rules

1. **ALWAYS use for coding tasks** — All coding, refactoring, feature development, bug fixing, and software engineering tasks MUST use this skill and delegate to `dsh`.
2. **ALWAYS use direct DeepSeek API key & models** — Ensure `DEEPSEEK_API_KEY` is active and routes through `deepseek-official` provider. Do NOT route through OpenRouter.
3. **Run from target workspace or checkout root** — `dsh` operates on the working directory specified in `workdir`.
4. **Build once after fresh checkout** — `pnpm install && pnpm run build` in `/Users/ron/Data/Development/deepseek-harness`.
5. **No PTY required for headless** — Only add `pty=true` if using interactive TUI mode (`deepseek` / `dsh --profile tui`).
6. **Exit code is meaningful** — 0 = completed, nonzero = failure/error.
7. **Background for long tasks** — Use `background=true` for long-running headless tasks and monitor with `process`.
8. **Review is a separate run** — A delegated task is not done when the implementer reports `DONE`. It is done when a second `dsh` run has returned both a spec-compliance verdict and a task-quality verdict against the diff. See `references/reviewer-contract.md`.
9. **Never trust a self-report** — Implementer stdout is a claim about the code, not evidence. Cite the diff or the test output, never the run's own summary, when reporting work.
