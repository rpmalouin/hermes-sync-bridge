# The Maildir lifecycle

The queue is `/Volumes/appdata/hermes-Mac-Updates/` on the Mac, `/appdata/hermes-Mac-Updates` on
the DockerVM — the same directory over SMB. Everything about its safety follows from six
mechanical rules; each one is here with the failure it prevents.

## The six rules

1. **Publish atomically.** Write `tmp/<id>.json`, flush, then `rename()` into `new/<id>.json`.
   *Prevents:* a consumer reading a half-written file. `rename()` is the publish, and it is atomic.
2. **Never edit a file in `new/`.** A published message is immutable — body included.
   *Prevents:* two hosts disagreeing about what was sent. The authoritative attempt counter is
   `state/<id>.attempt` and the verdict is `state/<id>.status`; neither lives in the envelope.
3. **Claim by rename.** A consumer renames `new/<id>.json` into `processing/<consumer>/<id>.json`
   and only then reads it. *Prevents:* both loops consuming the same message, without a lock
   server. A dead claim is returned to `new/` by the reaper after the lease expires.
4. **Quarantine, don't drop.** A message that fails validation goes back to `new/` with an
   incremented attempt count; after `MAX_ATTEMPTS` it lands in `failed/<id>.json` with a
   `<id>.reason` beside it. *Prevents:* poison messages looping forever, and silent loss.
5. **Archive, never delete.** Applied mail moves to `processed/YYYY-MM/<id>.json`, with a
   `state/<id>.done` marker. *Prevents:* losing the only record of what was delivered.
6. **One-minute metronome, silent when idle.** Both consumers poll every minute and print nothing
   when there is nothing to do; any non-empty counter line is a signal.
   *Prevents:* latencies measured in hours, and log noise that hides real events.

## Directory map

```
tmp/                    in-flight publishes (swept when older than TMP_MAX_AGE_MIN)
new/                    published, unclaimed — the mail
processing/hermes-mac/  claimed by the Mac consumer, in flight (lease = 15 min by default)
processing/hermes-docker/
requests/               parked asks: a task_request another session must act on
processed/YYYY-MM/      applied, archived
failed/                 quarantined, with <id>.reason
state/                  see below — shared by both consumers, so filter by address, not by name
recent-updates.json     producer-side digest (not mail; never commit it anywhere)
SCHEMA.md               the canonical contract — lives here because both hosts can read it
generate.py             the VM's producer (runs on the VM; copied into docker/ for history)
```

## `state/` — the part operators need at 03:00

| file | meaning |
|---|---|
| `<consumer>.state.json` | `last_run`, `last_result` (`applied=… dup=… failed=… requeued=… parked=… pending=… claim_lost=… claim_errors=… reaped=… others=… legacy_strays=…`), `provisioned`. **Read this from the other host to answer "is that side alive?"** |
| `<id>.attempt` | authoritative attempt counter |
| `<id>.status` | `{phase, attempts, last_reason, author}` — written on rejection, cleared on success |
| `<id>.done` | applied and archived |
| `<id>.request.json` | a parked ask: `{status: pending\|done\|expired, request, target, requires, acceptance, source, expires_at, reply_expected, consumer}` |
| `<id>.receipt` | the receipt a parked ask produced when it was closed |

A parked ask is closed by whichever side parked it. `done` renders as "closed" in the vault's
request note; `expired` means nobody acted before `expires_at` — the one state that must never be
mistaken for success.

## Writing policy: `kind` decides the path

- `kind: notification` — applied by the loop (a `dev_update` gets written to the vault log and the
  board; a `ping` is a no-op health check).
- `kind: task` — **never applied by a loop.** It is parked in `requests/` for a reasoning session,
  whatever its `task_type` says. This is the rule that stops asks being silently "handled" by a
  script that only knew how to log them.

## Compatibility with the running system

`mac/deploy.sh` installs repo code over the live paths *after* running the suite, one file at a
time, with rollback. Nothing in the live topology moves, and no cron entry point points at
`/Volumes/Data` — that volume can unmount, and a consumer that silently stops consuming is worse
than one that fails loudly. `deploy.sh --check` reports drift between the repo and the installs.
