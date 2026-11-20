#!/usr/bin/env bash
# dsh-wrapper.sh — portable launcher for the DeepSeek Harness built CLI.
#
# Installed as `dsh` on both hosts (Mac: $HOME/.local/bin/dsh, dockerVM: /usr/local/bin/dsh).
# Owned by the toolchain pin in config/toolchain-pin.yaml; do not hand-edit on a host.
#
# Why a wrapper and not a symlink:
#   * The checkout path differs per host, so the target cannot be baked in.
#   * node is not on cron's or `env -i`'s PATH (on dockerVM it is managed by Hermes
#     under /root/.hermes/tools/), so a bare `exec node` fails with exit 127 outside
#     an interactive session.
#   * `pnpm dsh` (tsx source launch) against a tree that already has built lib/ loads a
#     second copy of @deepseek-ai/dsh-tools and every tool call dies with
#     "Cannot read properties of undefined (reading 'prepare')". Always run the built CLI.
#
# Overrides: DSH_CHECKOUT, DSH_NODE, DSH_ENV_FILE. Permission mode is left at dsh's own
# default; pass DSH_PERMISSION_MODE yourself (danger-full-access on the VM, workspace-write
# on the Mac).
set -euo pipefail

# --- profile store needs a HOME even under `env -i` -------------------------------
if [ -z "${HOME:-}" ]; then
  if [ -d /root ]; then HOME=/root; else HOME="$(cd ~ 2>/dev/null && pwd || echo /tmp)"; fi
  export HOME
fi

# --- checkout ---------------------------------------------------------------------
if [ -n "${DSH_CHECKOUT:-}" ]; then
  CHECKOUT="$DSH_CHECKOUT"
elif [ -f /appdata/deepseek-harness/apps/cli/lib/bin.js ]; then
  CHECKOUT=/appdata/deepseek-harness
elif [ -f "$HOME/Data/Development/deepseek-harness/apps/cli/lib/bin.js" ]; then
  CHECKOUT="$HOME/Data/Development/deepseek-harness"
elif [ -f "$HOME/Development/deepseek-harness/apps/cli/lib/bin.js" ]; then
  CHECKOUT="$HOME/Development/deepseek-harness"
else
  CHECKOUT=/appdata/deepseek-harness
fi
CLI="$CHECKOUT/apps/cli/lib/bin.js"

if [ ! -f "$CLI" ]; then
  echo "dsh: built CLI missing: $CLI" >&2
  echo "dsh: run 'cd $CHECKOUT && pnpm install && pnpm run build' (set DSH_CHECKOUT if the checkout lives elsewhere)" >&2
  exit 127
fi

# --- node: explicit, PATH-independent ---------------------------------------------
find_node() {
  if [ -n "${DSH_NODE:-}" ] && [ -x "$DSH_NODE" ]; then printf '%s\n' "$DSH_NODE"; return 0; fi
  local c
  # Hermes-managed toolchain first (dockerVM): highest version wins.
  for c in $(ls -d "$HOME"/.hermes/tools/node-*-linux-x64/bin/node /root/.hermes/tools/node-*-linux-x64/bin/node 2>/dev/null | sort -V -r); do
    [ -x "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  for c in /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node; do
    [ -x "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  c="$(command -v node 2>/dev/null || true)"
  [ -n "$c" ] && { printf '%s\n' "$c"; return 0; }
  return 1
}

NODE_BIN="$(find_node)" || {
  echo "dsh: no node interpreter found (set DSH_NODE=/path/to/node)" >&2
  exit 127
}

# --- native DeepSeek key, only if the caller has not supplied one -------------------
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  for f in "${DSH_ENV_FILE:-}" "$HOME/.hermes/.env" /root/.hermes/.env; do
    [ -n "$f" ] && [ -r "$f" ] || continue
    DEEPSEEK_API_KEY="$(sed -n 's/^\(export \)\{0,1\}DEEPSEEK_API_KEY=//p' "$f" | tail -1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
    [ -n "$DEEPSEEK_API_KEY" ] && export DEEPSEEK_API_KEY
    break
  done
fi

exec "$NODE_BIN" "$CLI" "$@"
