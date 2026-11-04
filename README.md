# hermes-sync-bridge

The messaging and board plumbing shared between the **Mac** (vault owner) and the **DockerVM**
(producer), kept in one place with history. Nothing here is a framework — it is the set of
scripts that makes two Hermes instances coexist, and it all used to live unversioned under
`~/.hermes/` and in an SMB share where a bad edit could not be reverted.

```
shared/   the message contract, implemented and checked against the canonical SCHEMA.md
mac/      the consumer, the board tooling, the archive roll, the acceptance suite, deploy.sh
docker/   the VM's scripts, as a clone target (see Ownership below)
config/   example configs and path templates — never real hostnames or credentials
docs/     architecture, lifecycle, the Obsidian-Kanban contract, invariants
```

## The architecture, as of 2026-09-22

**Both hosts are writers and consumers.** This is worth stating because older notes describe a
"single-writer Mac and a producer-only VM", and that stopped being true: the VM runs its own
inbox consumer with its own state file, has a request-close helper, and since the Hatchdoor
restore holds a vault write path under a write-when-asked policy.

```
        publish (tmp -> new, atomic rename)
DockerVM ───────────────────────────────────►  /Volumes/appdata/hermes-Mac-Updates
   ▲                                                   │
   │ claim by rename                                   │ claim by rename
   │                                                   ▼
VM inbox consumer ◄──────────────────────────  Mac consumer (~/.hermes)
   │                                                   │
   │ parks asks, never applies them                    │ applies dev_updates,
   ▼                                                   ▼ writes the vault +
asks reach a VM session                        parks asks for a Mac session
```

There is **no VM dispatcher and no broker**. That direction was considered and declined in favour
of the filesystem: a Maildir on a share needs no daemon, survives reboots, and gets its locking
from `rename()`. Do not document or build one.

The **Activity Kanban** is a separate surface with four possible writers — this repo's consumer
(opt-in, `card: true`), `mac/kanban-card.py`, the VM's Hermes through its own Obsidian API, and
the Obsidian plugin itself. `docs/KANBAN-PLUGIN-CONTRACT.md` records what the plugin owns.

## The Maildir lifecycle

`docs/MAILDIR-LIFECYCLE.md` is the full version; the short one:

| stage | who | rule |
|---|---|---|
| `tmp/<id>.json` | publisher | in-flight write, not yet mail |
| `new/<id>.json` | publisher → consumer | published. Never edit or truncate a file here |
| `processing/<consumer>/<id>.json` | consumer | held only via `rename()` — that rename *is* the lock |
| `requests/<id>.json` | consumer | a `task_request`, parked for a session, never applied by a loop |
| `processed/YYYY-MM/` | consumer | archive, never delete |
| `failed/<id>.reason` | consumer | quarantined after N attempts, with the reason beside it |

Side-state that every operator eventually needs: `state/<id>.attempt` (authoritative counter),
`state/<id>.status` (why it was rejected), `state/<id>.done`, `state/<id>.request.json` (a parked
ask's verdict), and `state/<consumer>.state.json` (`last_run` + counters — readable from the other
host, which is how you answer "is that side alive?").

## Setup

**Mac (consumer + board tooling)**

```bash
git clone https://github.com/rpmalouin/hermes-sync-bridge.git
cd hermes-sync-bridge
python3 shared/test_protocol.py --require-schema     # contract, incl. drift vs SCHEMA.md
python3 mac/acceptance_tests.py                      # ~100 end-to-end checks, sandboxed
./mac/deploy.sh --check                              # repo vs the live installs
./mac/deploy.sh                                      # install (tests first, atomic, rollback)
```

The live paths do **not** move. `deploy.sh` installs the repo's code over them, one file at a
time, after the suite passes: the consumer and its tests into both skill copies, the board tools
into `~/.hermes/scripts/`. Nothing under `/Volumes/Data` is ever a cron's entry point — that
volume can unmount, and a 1-minute loop whose script lives behind a removable mount is a silent
stall waiting to happen.

**DockerVM (producer + its own consumer)**

Their scripts run from their own paths; `docker/` holds copies for history and for their clone.
Ownership is in the next section — their deploy step is theirs to run.

## Ownership (one pen per file)

- `mac/` — Mac-owned. Edit on the Mac, deploy from the Mac.
- `docker/` — VM-owned. The Mac keeps it in sync; the VM commits changes to its own scripts.
- `shared/`, `docs/`, `README.md`, `config/` — change by agreement. `shared/protocol.py` follows
  `SCHEMA.md`, which is **canonical and lives in the queue**, because the contract has to be
  readable by both hosts. The drift check enforces the agreement; do not fork the document.

## What is deliberately not in this repo

- **The skills** (`~/.hermes/**/skills/homelab/*/SKILL.md`). They are the agent-facing contract
  and are edited in place by Hermes's curator; mirroring them here would create a third copy that
  drifts. `docs/ARCHITECTURE.md` points at the live paths instead.
- **The vault** (`/Volumes/Data/vault`) — not a git repo by design; its history is
  `Co-edit log.md`.
- **Adjacent tooling not yet consolidated**: `vault-drift-report.py`, `container-sync*.py`,
  `vault-audit/`. Same house style, different job; they can join later under `mac/`.

## Configuration, not hardcoded hosts

Nothing host-specific is compiled in. `config/mailbox.env.example` and `config/paths.example.yaml`
are the templates, and the scripts read `MAILBOX_ROOT`, `MAILBOX_SCHEMA`, `VAULT_ROOT`,
`KANBAN_VAULT` and `HATCHDOOR_VAULT_ID` from the environment. The paths in the examples describe
the shape of a working deployment rather than a machine: copy the example, edit the copy, never
commit it.

Two consequences worth knowing before you deploy:

- **A missing vault root is a hard error.** `ingest.py` stops with a sentence naming the variable
  instead of reporting `applied=0` against a directory that is not there — an empty queue and a
  misconfigured one must never look alike.
- **A missing vault id is a hard error too.** `docker-kanban-card.py` refuses to guess which
  Hatchdoor vault to edit.

## Maintainership

Built for one homelab and published because the patterns are worth reusing. New functionality gets
added when that deployment needs it and no earlier: no support schedule, no API-stability promise,
no roadmap. Fork it and support your fork.

## License

MIT — see [LICENSE](LICENSE).
