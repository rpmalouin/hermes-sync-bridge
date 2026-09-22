# docker/ — the DockerVM's scripts

**Owner: the DockerVM, and the clone is the source.** Decided 2026-09-22 in answer to the ask on
the queue: this host *adopts* the repo for its own scripts. Edit here, install with
`docker/deploy.sh`, commit. The live paths do not move (`~/.hermes/scripts/`, the queue's own
`generate.py`) because a 1-minute cron whose entry point lives behind a clone would stop consuming
silently — so deploy installs copies: tested first, one file at a time, atomically, with rollback.
Run `./docker/deploy.sh --check` to see drift between this directory and what actually runs.

## What is here

| file | live path | status |
|---|---|---|
| `generate.py` | `/appdata/hermes-Mac-Updates/generate.py` | the producer; lives in the queue by contract, do not move it |
| `hermes-docker-inbox.py` | `~/.hermes/scripts/hermes-docker-inbox.py` | the VM's inbox consumer — claims `hermes-docker`/`any`, parks asks, never touches the vault |
| `hermes-docker-inbox-cron.sh` | `~/.hermes/scripts/hermes-docker-inbox-cron.sh` | the wrapper cron `cron-job-id` calls every minute |
| `hermes-docker-request-close.py` | `~/.hermes/scripts/hermes-docker-request-close.py` | closes a parked ask and publishes the contract's receipt |
| `docker-kanban-card.py` | `~/.hermes/scripts/docker-kanban-card.py` | `open` / `done` / `list` / `check` — cards our work in the shared Kanban with the plugin's exact shape, a count tripwire and hash-guarded edits |
| `deploy.sh` | — | installs the rows above; `--check`, `--dry-run` |

## How a change lands here

0. **Card it first — before you touch anything.** `docker-kanban-card.py open "<title>"` opens the
   card in `Hermes-dockerVM (Active)` (`Status: active`, box **unchecked**, `Context` linked to a
   dated note in `Hermes/Hermes Logs/dockerVM/`) and then send the transition as a `dev_update`
   (`modified`) so movement is heard rather than discovered. the operator reads the board as the record of
   what is *happening*; a card that arrives after the work describes nothing. This step is not
   advice: on 2026-09-22 sixteen minutes of real work — three scripts, a deploy step and a consumer
   crash fix — ran against an empty column because the rule lived in a session's memory.
1. Edit the file in this directory (never the live copy — `deploy.sh --check` will call it drift).
2. `./docker/deploy.sh --dry-run`, then `./docker/deploy.sh`. The gates compile every script and
   parse the wrapper; the install is per-file and atomic; the verify step re-checks byte equality
   and runs the installed consumer with `--dry-run` (which writes nothing); any failure rolls the
   backups back.
3. **Close the card when it lands.** `docker-kanban-card.py done "<title substring>" --summary
   "<what shipped>"` flips the box to `- [x]`, sets `Status: done` and moves the card to `Done`;
   append the same summary to the log note, then send the second `dev_update`. A card opened after
   the fact says so in its `Notes` — practice, not confession.
4. Commit and push. The Mac side reads this directory for history and review.

The same discipline is written into the `hermes-activity-kanban` skill on this host, where a session
finds it when the work starts rather than when the work is over.

## What the consumer had to learn the hard way (2026-09-22)

- A macOS `._<name>.json` AppleDouble sidecar matches `*.json` and is **not a message**. Parsing one
  as UTF-8 killed a whole run and cost the real mail queued behind it. It is now counted as a stray
  and skipped, never read.
- One unreadable message must never end the run: it is skipped visibly, or handed back to `new/` if
  it was already claimed. The consumer's own stale claims are reaped back to `new/` after 15 minutes
  at the start of every run, because a consumer that only globs `new/` would otherwise never see
  mail stranded by a crash.

## Producer expectations worth preserving

- Publish by `tmp/<id>.json` then `rename()` into `new/<id>.json`; never write `new/` directly.
- Slugs are canonicalized before publish (`dev-update` == `dev_update`); an unknown `task_type` is
  refused *before* it burns a retry budget on the consumer.
- `kind: task` is reserved for things that need a reasoning session.
- Asks go out as `task_request` only. A `dev_update` is a notification about work already done —
  an ask smuggled into one gets logged and closed by the loop with nobody acting on it.
- Never mutate a published body; attempts live in `state/<message_id>.attempt`.
- Carding is opt-in: `--card` sets `payload.card = true` for a `dev_update`. Without it the consumer
  logs and cards nothing; with it, `dev_item` must equal the card's title / `**Task:**` text or a
  second card is created. This host writes its own cards, so `--card` is the fallback.
