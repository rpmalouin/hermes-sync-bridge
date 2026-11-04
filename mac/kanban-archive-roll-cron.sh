#!/usr/bin/env bash
# kanban-archive-roll-cron.sh — the scheduled half of the filing rule.
#
# Ticked cards in the board's `Done` column ARE the definition of "finished", and this files them
# into the year note with no human action (operator, 2026-09-23: "let you do the archiving, not manually
# by me"). It is deliberately SILENT when there is nothing to do: cron output is delivered verbatim,
# so a nightly "nothing to file" is noise, and the watchdog contract in this homelab is empty
# stdout = say nothing.
#
# A dry run runs FIRST and the roll only happens when it found something. That keeps a no-op night
# from rewriting either file, and the reason is not only tidiness: any external write to a note that
# is open in Obsidian gets annotated by the live-coedit plugin, so a nightly no-op rewrite would keep
# re-tinting the year note for nothing.
#
# The entry point itself must live OUTSIDE /Volumes/Data — that volume can unmount, and a scheduled
# job whose script vanishes with it stops consuming silently. The roller it calls is the deployed
# copy beside it (installed by mac/deploy.sh; the repo is the source, never the live copy).
#
# Always exits 0, including on refusal and on tool failure: the MESSAGE is the signal, delivered
# verbatim, so a bad night is never masked — and never double-reported as a job error either. The
# roller refuses (rc=2) rather than half-writing if any invariant would break; it rolls back itself.
set -uo pipefail

# The repo ships a PLACEHOLDER vault on purpose; the real one is per-machine state and comes from
# the environment or from a key=value file outside the repo ($SYNC_BRIDGE_ENV, default
# ~/.hermes/sync-bridge.env) — the same file the Python tools read.
INSTANCE_ENV="${SYNC_BRIDGE_ENV:-$HOME/.hermes/sync-bridge.env}"
if [ -z "${VAULT_ROOT:-}" ] && [ -f "$INSTANCE_ENV" ]; then
  VAULT_ROOT="$(sed -n 's/^[[:space:]]*VAULT_ROOT[[:space:]]*=[[:space:]]*//p' "$INSTANCE_ENV" \
    | tail -1 | sed 's/[[:space:]]*$//' | tr -d '"' | tr -d "'")"
  export VAULT_ROOT
fi
VAULT="${KANBAN_VAULT:-${VAULT_ROOT:-/Volumes/Data/vault}}"
ROLL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/kanban-archive-roll.py"
BOARD="$VAULT/Hermes/Hermes Kanban.md"

if [ ! -f "$BOARD" ]; then
  echo "Kanban archive roll: SKIPPED — $BOARD is not readable (is /Volumes/Data mounted?). Nothing was filed."
  exit 0
fi
if [ ! -f "$ROLL" ]; then
  echo "Kanban archive roll: SKIPPED — $ROLL is missing. Deploy the repo: mac/deploy.sh."
  exit 0
fi

plan="$(python3 "$ROLL" --vault "$VAULT" --dry-run --source both 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "Kanban archive roll: the dry run refused (rc=$rc) — nothing written:"
  printf '%s\n' "$plan"
  exit 0
fi
# nothing to file, or everything already present: say nothing at all
case "$plan" in
  *"nothing to file"*|*"nothing new to write"*) exit 0 ;;
esac

out="$(python3 "$ROLL" --vault "$VAULT" --source both 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "Kanban archive roll: FAILED (rc=$rc) — the tool refuses rather than half-write, and backs both files up first:"
  printf '%s\n' "$out"
  exit 0
fi
echo "Kanban archive roll (scheduled, no human action needed):"
printf '%s\n' "$out"
