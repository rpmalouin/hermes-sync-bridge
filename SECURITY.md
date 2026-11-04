# Security

This repository is a reference implementation of a two-host filesystem queue, and it ships with
**no credentials**. `config/` holds examples only; a real `.env`, a mount credential or a key never
belongs in it.

## Deliberate placeholders

Paths, identities and ids in this repo are configuration, not a machine: `/Volumes/Data/vault`, a
queue beside it, `<cron-job-id>` where a scheduler id would go, `<profile>` for a Hermes profile, and
`HATCHDOOR_VAULT_ID` for the vault the board tooling edits. They name nothing about the deployment
this was extracted from.

## What the queue is trusted with

A Maildir on a share carries no secret of its own — it authenticates with the mount, not with the
message. Treat message bodies as operator prose: they describe work, they do not carry tokens.

## Before you push here

History is public and permanent, so a scrub at the tip cannot un-publish an older commit. The
published history was audited object by object before the repository was made public:

```sh
python3 .../history-secret-sweep.py --repo . --fetch
git grep -l '<private-term>' $(git rev-list --all)     # prove-in-history, not just HEAD
```

A clone can install the pre-push guard from the tooling that produced this repo
(`pre-push-secret-guard.sh` → `.git/hooks/pre-push`): it refuses a push whose added lines look like
a token, a password, a private key, a `Bearer` value, an RFC1918 address or a credentials path. It
is local to that clone; the deliberate override is `git push --no-verify`.

## Reporting

Report a defect in the code with a minimal repro. Never include secrets, hostnames, addresses or a
real queue message in a report.
