#!/usr/bin/env bash
# parity-drift-cron.sh — the scheduled half of the parity check.
#
# Cron runs with a minimal PATH and no login shell, so this pins the interpreter explicitly
# (same resolution as mac/parity-check.sh) and execs the engine with no arguments. The engine's
# exit code is this script's exit code on purpose: a drift run (1) or an unmeasurable run (2)
# is a cron error, which is how the failure lands on Telegram. Silence means equal.
#
# The engine beside this script is the one mac/deploy.sh installs into ~/.hermes/scripts/; the
# entry point deliberately lives on the internal disk so an unmounted volume cannot stop cron.
# Read-only: it never writes a file and never creates state.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$HERE/parity_check.py"

# cron's PATH does not include ~/.local/bin, where the dsh wrapper lives, and the engine must be
# able to run `dsh`/`which`/`git` to measure them. Prepend the fixed locations only; nothing here
# picks a version, it just makes the pinned tools reachable from a non-login shell.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PATH

PYTHONS=()
[ -n "${PARITY_PY:-}" ] && PYTHONS+=("$PARITY_PY")
PYTHONS+=(
  "$HOME/.local/venvs/crg/bin/python3"
  "/opt/homebrew/bin/python3"
  "/usr/bin/python3"
)

tried=()
for py in "${PYTHONS[@]}"; do
  tried+=("$py")
  if [ -x "$py" ] && "$py" -c 'import yaml' >/dev/null 2>&1; then
    exec "$py" "$ENGINE"
  fi
done

echo "parity-drift: no interpreter with PyYAML — tried: $(printf '%s ' "${tried[@]}" | sed 's/ $//'). Nothing was checked." >&2
exit 2
