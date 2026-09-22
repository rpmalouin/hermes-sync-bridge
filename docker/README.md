# docker/ — the DockerVM's scripts

**Owner: the DockerVM.** The Mac keeps this directory in sync for history and review; the VM
commits changes to its own scripts here. See "one pen per file" in the top-level README.

## What is here

| file | status |
|---|---|
| `generate.py` | the producer, copied byte-identical from the live share (`/appdata/hermes-Mac-Updates/generate.py`) on 2026-09-22 |

## What is not here yet, and why

The VM's other scripts run from `root@dockervm:/root/.hermes/scripts/` and were not readable from
the Mac when this repo was created:

- `hermes-docker-inbox.py` — its inbox consumer (claims `hermes-docker` mail, parks asks, never
  touches the vault)
- `hermes-docker-inbox-cron.sh` — the wrapper its 1-minute job calls
- `hermes-docker-request-close.py` — closes a parked ask and publishes the contract's receipt

They belong in this directory. The ask sent over the queue
(`task_request`, 2026-09-22) is what decides the model:

- **adopt the clone as the source** — the VM clones this repo, edits here, and installs into its own
  live paths the same way `mac/deploy.sh` does on this side; or
- **keep this a read-only mirror** — the VM's live scripts stay authoritative, and `docker/` is
  updated by hand when something changes.

Either way, do not change a script that runs on their host from here. Their host, their pen.

## Producer expectations worth preserving

- Publish by `tmp/<id>.json` then `rename()` into `new/<id>.json`; never write `new/` directly.
- Slugs are canonicalized before publish (`dev-update` == `dev_update`); an unknown `task_type` is
  refused *before* it burns a retry budget on the consumer.
- `kind: task` is reserved for things that need a reasoning session.
- Asks go out as `task_request` only. A `dev_update` is a notification about work already done —
  an ask smuggled into one gets logged and closed by the loop with nobody acting on it.
- Never mutate a published body; attempts live in `state/<message_id>.attempt`.
