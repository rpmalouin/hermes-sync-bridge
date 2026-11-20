#!/usr/bin/env bash
# parity-check.sh — run the parity engine with an interpreter that can actually import PyYAML.
#
# Thin and self-locating on purpose: the engine lives beside this wrapper, and the only decision
# made here is WHICH python. $PARITY_PY wins (an override for a new venv), then the CRG venv
# (already required by the pin and already carrying PyYAML), then the two system interpreters.
# If none of them can import yaml the check has not run, so this exits 2 (unevaluable) — never 0.
#
# Read-only: this wrapper creates nothing, and the engine it execs never writes a file.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$ROOT/mac/parity_check.py"

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
    exec "$py" "$ENGINE" "$@"
  fi
done

echo "parity-check: no interpreter with PyYAML — tried: $(printf '%s ' "${tried[@]}" | sed 's/ $//'). Nothing was checked." >&2
exit 2
