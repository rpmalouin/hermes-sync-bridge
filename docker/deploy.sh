#!/usr/bin/env bash
# deploy.sh — install the docker/ scripts from this repo over the DockerVM's live paths.
#
# Repo = source of truth for docker/ (adopted 2026-09-22). The live paths do NOT move and are
# NOT symlinks: the 1-minute inbox cron (job cron-job-id) and the queue's own `generate.py`
# must keep working when this clone is missing, stale or unmounted, so what runs live is a
# copy. That copy is made here — tested first, one file at a time, atomically, with rollback.
#
#   ./docker/deploy.sh --check      # report drift between repo and live installs (exit 1 on drift)
#   ./docker/deploy.sh --dry-run    # show what would be installed, write nothing
#   ./docker/deploy.sh              # run the gates, install, verify, roll back if it fails
#
# Where the files land:
#   docker/hermes-docker-inbox.py         -> $HOME/.hermes/scripts/hermes-docker-inbox.py
#   docker/hermes-docker-inbox-cron.sh    -> $HOME/.hermes/scripts/hermes-docker-inbox-cron.sh
#   docker/hermes-docker-request-close.py -> $HOME/.hermes/scripts/hermes-docker-request-close.py
#   docker/docker-kanban-card.py          -> $HOME/.hermes/scripts/docker-kanban-card.py
#   docker/generate.py                    -> $QUEUE_DIR/generate.py  (the queue itself: the
#                                            contract says this script lives in the queue)
#
# Overridable for testing, never needed in normal use:
#   HERMES_TARGET_HOME (default: /root)      QUEUE_DIR (default: /appdata/hermes-Mac-Updates)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_HOME="${HERMES_TARGET_HOME:-/root}"
QUEUE_DIR="${QUEUE_DIR:-/appdata/hermes-Mac-Updates}"
SCRIPTS="$TARGET_HOME/.hermes/scripts"

CHECK=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --check)   CHECK=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "usage: deploy.sh [--check|--dry-run]" >&2; exit 2 ;;
  esac
done

# ---- gates: refuse to deploy something that cannot land where it is needed ------------------
if [ ! -d "$SCRIPTS" ]; then
  echo "FATAL: $SCRIPTS does not exist — nothing to install into." >&2
  exit 1
fi
if [ ! -d "$QUEUE_DIR" ]; then
  echo "FATAL: the mailbox queue $QUEUE_DIR is not there. generate.py lives in that directory;" >&2
  echo "       a deploy without it would leave a live producer that nothing can update." >&2
  exit 1
fi
for f in hermes-docker-inbox.py hermes-docker-inbox-cron.sh hermes-docker-request-close.py \
         docker-kanban-card.py generate.py; do
  if [ ! -f "$REPO/docker/$f" ]; then
    echo "FATAL: $REPO/docker/$f is missing — the repo is incomplete, refuse to guess." >&2
    exit 1
  fi
done

pairs() {
  echo "$REPO/docker/hermes-docker-inbox.py|$SCRIPTS/hermes-docker-inbox.py"
  echo "$REPO/docker/hermes-docker-inbox-cron.sh|$SCRIPTS/hermes-docker-inbox-cron.sh"
  echo "$REPO/docker/hermes-docker-request-close.py|$SCRIPTS/hermes-docker-request-close.py"
  echo "$REPO/docker/docker-kanban-card.py|$SCRIPTS/docker-kanban-card.py"
  echo "$REPO/docker/generate.py|$QUEUE_DIR/generate.py"
}

sha() { sha256sum "$1" | awk '{print $1}'; }
show() { echo "${1/#$TARGET_HOME/~}"; }

# ---- check mode: no writes, exit non-zero on drift ------------------------------------------
if [ "$CHECK" = 1 ]; then
  drift=0
  while IFS='|' read -r src dst; do
    if [ ! -f "$dst" ]; then
      echo "MISSING  $(show "$dst")"; drift=1
    elif [ "$(sha "$src")" = "$(sha "$dst")" ]; then
      echo "in sync  $(show "$dst")"
    else
      echo "DRIFT    $(show "$dst")   repo=$(sha "$src" | cut -c1-8)  live=$(sha "$dst" | cut -c1-8)"
      drift=1
    fi
  done < <(pairs)
  exit "$drift"
fi

# ---- gates on the code itself, before anything is installed ---------------------------------
pycache="$(mktemp -d)"
echo "gates:"
for f in hermes-docker-inbox.py hermes-docker-request-close.py docker-kanban-card.py generate.py; do
  PYTHONPYCACHEPREFIX="$pycache" python3 -m py_compile "$REPO/docker/$f"
  echo "  compiles  docker/$f"
done
rm -rf "$pycache"
bash -n "$REPO/docker/hermes-docker-inbox-cron.sh"
echo "  parses    docker/hermes-docker-inbox-cron.sh"

if [ "$DRY" = 1 ]; then
  echo "dry run — would install:"
  while IFS='|' read -r src dst; do
    echo "  $(sha "$src" | cut -c1-8)  $src -> $(show "$dst")"
  done < <(pairs)
  exit 0
fi

# ---- install: per file, atomic, with a backup kept for rollback -----------------------------
backup="$(mktemp -d)"
installed=""
rollback() {
  echo "ROLLBACK: restoring from $backup" >&2
  for dst in $installed; do
    base="$(basename "$dst")"
    [ -f "$backup/$base" ] && cp -p "$backup/$base" "$dst" && echo "  restored $(show "$dst")" >&2
  done
  exit 1
}
trap 'rollback' ERR

while IFS='|' read -r src dst; do
  [ -f "$dst" ] && cp -p "$dst" "$backup/$(basename "$dst")"
  staged="$(dirname "$dst")/.$(basename "$dst").deploy"
  cp -p "$src" "$staged"
  chmod --reference="$dst" "$staged" 2>/dev/null || true
  mv -f "$staged" "$dst"
  installed="$installed $dst"
  echo "installed $(show "$dst")  $(sha "$dst" | cut -c1-8)"
done < <(pairs)

# ---- verify: byte-identical, and the consumer still behaves (dry run touches nothing) -------
while IFS='|' read -r src dst; do
  [ "$(sha "$src")" = "$(sha "$dst")" ] || { echo "verify failed: $(show "$dst")" >&2; false; }
done < <(pairs)
echo "verify: every file byte-identical to the repo"

python3 "$SCRIPTS/hermes-docker-inbox.py" --dry-run >/dev/null \
  || { echo "verify failed: the installed consumer refuses to run" >&2; false; }
echo "verify: installed consumer runs (--dry-run, no writes)"

trap - ERR
rm -rf "$backup"
echo "ok"
