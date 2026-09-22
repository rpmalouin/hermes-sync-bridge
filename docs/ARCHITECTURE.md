# Architecture

## The two hosts

| | Mac | DockerVM |
|---|---|---|
| **Writes to** | the vault, the board, its own skill/script dirs | the queue, its own scripts; the vault **only when asked** |
| **Queue role** | consumer of `hermes-mac` mail; publisher of receipts and asks | producer of `dev_update` / `ping` / `task_request`; consumer of `hermes-docker` mail |
| **Loop** | cron `cron-job-id`, every 1 minute, `deliver=local`, prompt-mediated | its own inbox consumer, every 1 minute, delivers to a chat |
| **Board role** | keeps `Hermes-Mac (Active)` → `Done` live | keeps `Hermes-dockerVM (Active)` → `Done` live |

Neither side is "the writer". Both consume, both publish, and since the Hatchdoor restore the VM
can also write the vault when it is asked to. Design anything here as a two-writer system.

## Why a filesystem queue instead of a broker

A Maildir on an SMB share needs no daemon, survives reboots and survives one host being down, and
takes its mutual exclusion from `rename()` — which is atomic server-side (measured, not assumed).
Those six mechanical rules are in `MAILDIR-LIFECYCLE.md`. The rejected alternatives, and why:

- **a broker (MQTT/Redis):** better for fan-out and latency, worse for everything else here —
  nothing survives the broker being down, and artifact delivery is not a streaming problem;
- **an agent-to-agent MCP dispatcher:** the shape was fine, but MCP cannot push, so "dispatch"
  would have meant an async job with an HTTP surface — more moving parts than a directory;
- **a `dev_update` carrying an ask:** rejected on principle and then by code. A notification is
  applied by a loop; an ask needs a reasoning session, so asks are `task_request` with
  `kind: task`, and the loop parks them instead of acting on them.

## The surfaces, and who owns each

1. **The queue** (`/Volumes/appdata/hermes-Mac-Updates/`) — the interface. `SCHEMA.md` is
   canonical here, not in this repo, because the contract must be readable from both hosts.
2. **The vault** (`/Volumes/Data/vault/`) — Mac-owned content, not a git repo; its history
   is `Co-edit log.md` plus per-task notes under `Hermes/Hermes Logs/`. The VM writes here only
   when asked.
3. **The board** (`Hermes/Hermes Kanban.md`) — a plain markdown file with a *third-party* plugin
   asserting its shape. See `KANBAN-PLUGIN-CONTRACT.md`.
4. **The skills** — the agent-facing contract, live at
   `~/.hermes/skills/homelab/*/SKILL.md` **and** `~/.hermes/profiles/<p>/skills/homelab/*/SKILL.md`.
   Hermes's curator edits them, so they are not mirrored here. When they must change, both copies
   change and stay byte-identical.

## Where the code runs

```
Mac                                        DockerVM
~/.hermes/scripts/kanban-card.py           /root/.hermes/scripts/…  (theirs)
~/.hermes/scripts/kanban-archive-roll.py
~/.hermes/skills/homelab/hermes-mac-ingest/scripts/ingest.py   ← the live consumer
~/.hermes/profiles/<profile>/skills/…/scripts/ingest.py         ← second copy, same bytes
        ▲
        │  installed from this repo by mac/deploy.sh (tests first, atomic, rollback)
<clone>/mac/…             ← source of truth
```

The live paths are deliberately **not** symlinks into this repo: `/Volumes/Data` is an external
volume that can unmount, so a symlinked entry point would take the 1-minute loop down with it —
silently, which is the failure class this system exists to avoid. `deploy.sh` refuses to run when
the volume is absent, and verifies every install by reading it back.

## Failure modes that have actually happened

Each of these is encoded in a check somewhere in this repo, or in a doc here. They are listed so
the next person recognises the shape rather than re-deriving it.

| Symptom | Cause | Where it bit |
|---|---|---|
| "the other side never claimed my messages" | `task_type: dev-update` (hyphen) against a closed set; rejected, requeued, quarantined — while the counters read `applied=0 dup=0 failed=0`, indistinguishable from idle | slug canonicalization + a `requeued` counter + a producer-readable `.status` |
| an ask was "applied" and closed with nobody acting | it was sent as a `dev_update` notification, so the loop applied it | `task_request` + `kind: task`, parked for a session |
| a card vanished from the board | a move anchored on `## Done\n\n` after another writer reflowed that blank line: removed from the source, failed to insert at the destination | `mac/kanban-card.py` — regex parsing, placement check, card-count invariant |
| archived cards disappeared into nowhere | the plugin's archive is written into the board file and never rendered | `mac/kanban-archive-roll.py` into `Hermes/05 Archive/` |
| phantom "poison" messages | `pathlib.glob("*.json")` matches macOS AppleDouble `._*` stubs on the SMB share | hidden-name filter in the consumer; `._*` in `.gitignore` |
| an ask sent to the *other* side showed up as my pending work | both consumers keep state in the same `state/` directory | `load_requests()` filters on the address in the parked message |
