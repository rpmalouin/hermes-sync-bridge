#!/usr/bin/env bash
# deploy.sh — install this repo's code over the live paths.
#
# Repo = source of truth. The live paths do NOT move and are NOT symlinks: /Volumes/Data is an
# external volume that can unmount, and a 1-minute cron whose entry point lives behind it would
# stop consuming silently. So this installs copies, tests first, one file at a time, atomically,
# with rollback.
#
#   ./mac/deploy.sh --check      # report drift between the repo and the live installs (exit 1 if any)
#   ./mac/deploy.sh --dry-run    # show what would be installed
#   ./mac/deploy.sh              # run the gates, then install
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_HOME="${HERMES_TARGET_HOME:-$HOME}"
CHECK=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --check)   CHECK=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "usage: deploy.sh [--check|--dry-run]" >&2; exit 2 ;;
  esac
done

# ---- the external volume gate ------------------------------------------------------------
if ! mount | grep -q " on /Volumes/Data "; then
  echo "FATAL: /Volumes/Data is not mounted — this repo isn't there, so there is nothing safe to deploy." >&2
  exit 1
fi

# ---- repo file -> live path --------------------------------------------------------------
pairs() {
  local sk1="$TARGET_HOME/.hermes/skills/homelab/hermes-mac-ingest"
  local sk2="$TARGET_HOME/.hermes/profiles/<profile>/skills/homelab/hermes-mac-ingest"
  echo "$REPO/mac/ingest.py|$sk1/scripts/ingest.py"
  echo "$REPO/mac/acceptance_tests.py|$sk1/scripts/acceptance_tests.py"
  echo "$REPO/mac/ingest.py|$sk2/scripts/ingest.py"
  echo "$REPO/mac/acceptance_tests.py|$sk2/scripts/acceptance_tests.py"
  echo "$REPO/mac/kanban-card.py|$TARGET_HOME/.hermes/scripts/kanban-card.py"
  echo "$REPO/mac/kanban-archive-roll.py|$TARGET_HOME/.hermes/scripts/kanban-archive-roll.py"
  echo "$REPO/mac/kanban-archive-roll-cron.sh|$TARGET_HOME/.hermes/scripts/kanban-archive-roll-cron.sh"
}

sha() { shasum -a 256 "$1" | awk '{print $1}'; }
show() { echo "${1/#$TARGET_HOME/~}"; }

# ---- check mode: no writes, exit non-zero on drift ---------------------------------------
if [ "$CHECK" = 1 ]; then
  drift=0
  while IFS='|' read -r src dst; do
    if [ ! -d "$(dirname "$dst")" ]; then
      # same rule as the install loop: a destination whose directory is not on this host is not
      # drift, it is a target this machine does not have (e.g. a profile that was removed).
      echo "n/a      $(show "$dst")   (no such directory on this host)"
    elif [ ! -f "$dst" ]; then
      echo "MISSING  $(show "$dst")"; drift=1
    elif [ "$(sha "$src")" = "$(sha "$dst")" ]; then
      echo "in sync  $(show "$dst")"
    else
      echo "DRIFT    $(show "$dst")   repo=$(sha "$src" | cut -c1-8)  live=$(sha "$dst" | cut -c1-8)"; drift=1
    fi
  done < <(pairs)
  exit "$drift"
fi

# ---- gates: the contract and the end-to-end suite must pass before anything is installed ---
echo "== gates =="
python3 "$REPO/shared/test_protocol.py" >/dev/null
echo "   contract tests: pass"
python3 "$REPO/mac/acceptance_tests.py" >/dev/null
echo "   acceptance suite: pass"

if [ "$DRY" = 1 ]; then
  echo "== would install =="
  while IFS='|' read -r src dst; do echo "   $(show "$dst")"; done < <(pairs)
  exit 0
fi

# ---- install: stage beside the destination, then rename (atomic within one filesystem) -----
installed=()
rollback() {
  local rc=$?
  for dst in "${installed[@]}"; do
    if [ -f "$dst.deploy-bak" ]; then cp -p "$dst.deploy-bak" "$dst"; echo "   rolled back $(show "$dst")" >&2; fi
  done
  # The staging temp lives BESIDE THE DESTINATION — that is what makes the rename atomic — so
  # cleaning a repo path left a `.name.new-$$` file behind in the live directory on every failure.
  while IFS='|' read -r _src _dst; do rm -f "$(dirname "$_dst")/.$(basename "$_dst").new-$$"; done < <(pairs)
  exit "$rc"
}
trap rollback ERR

echo "== install =="
while IFS='|' read -r src dst; do
  dir="$(dirname "$dst")"
  if [ ! -d "$dir" ]; then
    echo "   skip $(show "$dst")  (no such directory on this host)"
    continue
  fi
  tmp="$dir/.$(basename "$dst").new-$$"
  cp -p "$src" "$tmp"
  # syntax-check BY TYPE: this loop installs more than Python (the board tooling now includes a
  # shell entry point for cron), and py_compile on a shell script raises SyntaxError, which takes
  # the whole deploy down with it — every target rolled back, gates having passed.
  case "$src" in
    *.py) python3 -m py_compile "$tmp" ;;
    *.sh) bash -n "$tmp" ;;
  esac
  [ -f "$dst" ] && cp -p "$dst" "$dst.deploy-bak"
  # BSD chmod has no --reference; copy the mode explicitly (falls back to 755)
  chmod "$(stat -f '%Lp' "$dst" 2>/dev/null || echo 755)" "$tmp"
  mv -f "$tmp" "$dst"                       # atomic within the directory
  [ "$(sha "$src")" = "$(sha "$dst")" ] || { echo "   verification failed for $(show "$dst")" >&2; false; }
  installed+=("$dst")
  echo "   installed $(show "$dst")"
done < <(pairs)

# ---- verify: byte-identical, and the installed copies still RUN ---------------------------
# Borrowed from the DockerVM's deploy.sh after their push: they verify by executing the installed
# consumer with --dry-run, which catches what a compile cannot — an import that resolves only in
# the repo, a stale bytecode path, a script that starts and dies. Writes nothing.
while IFS='|' read -r src dst; do
  # the install loop above SKIPS a destination whose directory is absent; this loop must skip the
  # same ones or it shas a file that was never installed, fails, and rolls the whole deploy back.
  [ -d "$(dirname "$dst")" ] || continue
  [ "$(sha "$src")" = "$(sha "$dst")" ] || { echo "   verify failed: $(show "$dst")" >&2; false; }
done < <(pairs)

consumer="$TARGET_HOME/.hermes/skills/homelab/hermes-mac-ingest/scripts/ingest.py"
if [ -f "$consumer" ]; then
  python3 "$consumer" --dry-run >/dev/null \
    || { echo "   verify failed: the installed consumer refuses to run" >&2; false; }
  echo "   verify: installed consumer runs (--dry-run, no writes)"
fi

board_tool="$TARGET_HOME/.hermes/scripts/kanban-card.py"
if [ -f "$board_tool" ]; then
  # `list` (read-only), deliberately not `check`: check's exit code reports the *board's* state, not
  # the install, and a deploy must not roll back because another writer is mid-edit.
  python3 "$board_tool" list >/dev/null \
    || { echo "   verify failed: the installed board tool refuses to run" >&2; false; }
  echo "   verify: installed board tool runs (list, read-only)"
fi

trap - ERR
echo "== live loops keep running; nothing was moved, nothing under /Volumes/Data is a cron target =="
