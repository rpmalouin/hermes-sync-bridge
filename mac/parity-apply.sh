#!/usr/bin/env bash
# parity-apply.sh — move BOTH hosts onto the pin, in one window. MANUAL ONLY.
#
# Why one window: code-review-graph's graph schema is forward-only. A host left on 2.3.x cannot
# read a graph.db the other host has already migrated (schema 15), so a half-applied pin breaks
# the host that was NOT changed. This script therefore preflights both hosts before touching
# either, and aborts loudly (exit 3) if the second host fails after the first succeeded.
#
# It installs code-review-graph into each host's OWN pinned venv (never a system interpreter) and
# moves each harness checkout to dsh.sha with `git fetch <remote> && git reset --hard <sha>`,
# re-checking the tree is clean immediately before the reset. It does NOT rebuild a graph, does
# NOT restart a gateway, and does NOT reset a dirty tree — those are printed as instructions.
#
#   ./mac/parity-apply.sh --dry-run    # preflight + plan only; mutates nothing (no TTY needed)
#   ./mac/parity-apply.sh              # apply, both hosts or abort (needs a TTY)
#   ./mac/parity-apply.sh --force      # apply without a TTY (deliberate; cron must never do this)
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN="$REPO/config/toolchain-pin.yaml"
DRY=0
FORCE=0

usage() {
  cat <<'EOF'
usage: parity-apply.sh [--dry-run] [--force]

  --dry-run   preflight both hosts and print the plan; change nothing
  --force     allow a real apply without a TTY (the default refuses; cron cannot reach this)

The pin is config/toolchain-pin.yaml in this repo. No graph is rebuilt and no gateway restarted.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "parity-apply: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

# ---- manual only: a real apply cannot start without a human at a TTY -----------------------
# --dry-run is read-only, so it is exempt: a pipe may inspect the plan, but a pipe may never
# mutate two hosts.
if [ "$DRY" != 1 ] && [ "$FORCE" != 1 ] && [ ! -t 0 ]; then
  echo "REFUSED: parity-apply.sh changes BOTH hosts and must be run by a human at a terminal." >&2
  echo "         stdin is not a TTY (cron or a pipe). Use --dry-run to inspect the plan, or" >&2
  echo "         pass --force deliberately. Nothing was changed." >&2
  exit 2
fi

# ---- an interpreter that can read the pin (same resolution as parity-check.sh) -------------
PYTHONS=()
[ -n "${PARITY_PY:-}" ] && PYTHONS+=("$PARITY_PY")
PYTHONS+=(
  "$HOME/.local/venvs/crg/bin/python3"
  "/opt/homebrew/bin/python3"
  "/usr/bin/python3"
)
PY=""
tried=()
for cand in "${PYTHONS[@]}"; do
  tried+=("$cand")
  if [ -x "$cand" ] && "$cand" -c 'import yaml' >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "FATAL: no interpreter with PyYAML — tried: $(printf '%s ' "${tried[@]}" | sed 's/ $//'). Nothing was changed." >&2
  exit 2
fi

# ---- read every value from the pin; refuse if it does not parse ----------------------------
PIN_VARS="$("$PY" - "$PIN" <<'PY'
import os, shlex, sys

try:
    import yaml
except ImportError as exc:
    sys.exit("PyYAML is not importable: %s" % exc)

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        pin = yaml.safe_load(fh)
except Exception as exc:
    sys.exit("cannot read or parse %s: %s" % (path, exc))
if not isinstance(pin, dict):
    sys.exit("%s is not a mapping" % path)


def field(dotted):
    node = pin
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise SystemExit("pin has no %s" % dotted)
        node = node[part]
    return node


def local(value):
    return os.path.expanduser(os.path.expandvars(str(value)))


values = {
    "CRG_VERSION": str(field("crg.version")),
    "GRAPH_SCHEMA": str(field("crg.graph_schema")),
    "SHA": str(field("dsh.sha")),
    "DSH_VERSION": str(field("dsh.version")),
    "DSH_REPO": str(field("dsh.repo")),
    "MAC_PY": local(field("hosts.mac.crg_python")),
    "MAC_CHECKOUT": local(field("hosts.mac.checkout")),
    "VM_PY": str(field("hosts.vm.crg_python")),
    "VM_CHECKOUT": str(field("hosts.vm.checkout")),
    "VM_SSH": str(field("hosts.vm.ssh")),
    "EMBED_PROVIDER": str(field("embeddings.provider")),
    "EMBED_MODEL": str(field("embeddings.model")),
}
for key, value in values.items():
    print("%s=%s" % (key, shlex.quote(value)))
PY
)" || {
  echo "FATAL: refusing to apply — the pin at $PIN could not be parsed (see the error above)." >&2
  echo "       Nothing was changed on either host." >&2
  exit 2
}
eval "$PIN_VARS"

# ---- command plumbing: the same commands run locally or through a bare ssh -----------------
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

run_on() {
  local host="$1"
  shift
  if [ "$host" = mac ]; then
    "$@"
  else
    ssh "${SSH_OPTS[@]}" "$VM_SSH" "$(printf '%q ' "$@")"
  fi
}

capture_on() {
  local host="$1"
  shift
  if [ "$host" = mac ]; then
    "$@"
  else
    ssh "${SSH_OPTS[@]}" "$VM_SSH" "$(printf '%q ' "$@")"
  fi
}

git_status_of() {
  capture_on "$1" env GIT_OPTIONAL_LOCKS=0 git -C "$2" status --porcelain
}

head_of() {
  capture_on "$1" env GIT_OPTIONAL_LOCKS=0 git -C "$2" rev-parse HEAD
}

crg_version_of() {
  capture_on "$1" "$2" -m code_review_graph --version 2>/dev/null | awk 'NF { print $NF; exit }'
}

choose_remote() { # host checkout -> the remote whose URL carries dsh.repo
  local host="$1" checkout="$2" names name url first
  names="$(capture_on "$host" git -C "$checkout" remote)" || return 1
  [ -n "$names" ] || return 1
  first="${names%%$'\n'*}"
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    url="$(capture_on "$host" git -C "$checkout" remote get-url "$name" 2>/dev/null)" || continue
    case "$url" in
      *"$DSH_REPO"*) printf '%s\n' "$name"; return 0 ;;
    esac
  done <<< "$names"
  # No URL matched the pinned repo; origin is the conventional fallback only if it exists.
  if printf '%s\n' "$names" | grep -qx origin; then
    printf 'origin\n'
    return 0
  fi
  printf '%s\n' "$first"
}

# ---- follow-up instructions (printed, never run) -------------------------------------------
print_followups() {
  cat <<EOF

== follow-up (this script does NEITHER: no graph is rebuilt, no gateway is restarted) ==
  rebuild the harness graph, mac:
    cd $MAC_CHECKOUT && $MAC_PY -m code_review_graph build
  rebuild the harness graph, dockerVM:
    ssh $VM_SSH 'cd $VM_CHECKOUT && $VM_PY -m code_review_graph build'
  then prove it again:
    ./mac/parity-check.sh          # expects graph schema $GRAPH_SCHEMA on both hosts
  if a rebuild changes the node set, re-embed with the pinned model on that host:
    <that host's pinned python> -m code_review_graph embed --provider $EMBED_PROVIDER --model $EMBED_MODEL
  bringing the new versions into service on BOTH hosts, from a shell outside the agent process
  (it terminates its own children, so this must never be issued from inside one):
    mac: its launchd job, by the documented routine in the hermes-agent skill
    dockerVM: that host's supervisor by hand - it has no service unit
  if the harness HEAD moved, dsh must be rebuilt on BOTH hosts before its version matches:
    cd <checkout> && pnpm install && pnpm run build && dsh --version   # must print $DSH_VERSION
EOF
}

# ---- preflight: measure both hosts before anything is changed -------------------------------
echo "== preflight =="
echo "   pin: $PIN (code-review-graph $CRG_VERSION, dsh $SHA / $DSH_VERSION)"

if ! ssh "${SSH_OPTS[@]}" "$VM_SSH" true 2>/dev/null; then
  echo "FATAL: $VM_SSH is unreachable — refusing to apply a pin to one host only." >&2
  echo "       Nothing was changed." >&2
  exit 1
fi
echo "   dockerVM: reachable over ssh"

if [ ! -x "$MAC_PY" ]; then
  echo "FATAL: the pinned mac interpreter $MAC_PY does not exist or is not executable." >&2
  echo "       Nothing was changed." >&2
  exit 1
fi
if ! run_on mac "$MAC_PY" -c 'import sys' >/dev/null 2>&1; then
  echo "FATAL: the pinned mac interpreter $MAC_PY cannot run." >&2
  echo "       Nothing was changed." >&2
  exit 1
fi
if ! run_on dockerVM "$VM_PY" -c 'import sys' >/dev/null 2>&1; then
  echo "FATAL: the pinned dockerVM interpreter $VM_PY cannot run." >&2
  echo "       Nothing was changed." >&2
  exit 1
fi

for spec in "mac|$MAC_CHECKOUT" "dockerVM|$VM_CHECKOUT"; do
  host="${spec%%|*}"
  checkout="${spec#*|}"
  label="$host"
  status="$(git_status_of "$host" "$checkout")" || {
    echo "FATAL: cannot read the $label harness tree at $checkout (git failed)." >&2
    echo "       Nothing was changed." >&2
    exit 1
  }
  if [ -n "$status" ]; then
    echo "REFUSED: the $label harness tree is dirty — a dirty tree is reported, never reset:" >&2
    printf '%s\n' "$status" | sed 's/^/         /' >&2
    echo "       Commit, stash or remove those paths, then re-run. Nothing was changed." >&2
    exit 1
  fi
  current_head="$(head_of "$host" "$checkout")" || current_head=""
  if [ -z "$current_head" ]; then
    echo "FATAL: cannot read HEAD in the $label checkout $checkout." >&2
    echo "       Nothing was changed." >&2
    exit 1
  fi
  if [ "$host" = mac ]; then
    current_crg="$(crg_version_of mac "$MAC_PY")" || current_crg=""
    current_dsh="$(capture_on mac dsh --version 2>/dev/null | awk 'NF { print $NF; exit }')" || current_dsh=""
  else
    current_crg="$(crg_version_of dockerVM "$VM_PY")" || current_crg=""
    current_dsh="$(capture_on dockerVM dsh --version 2>/dev/null | awk 'NF { print $NF; exit }')" || current_dsh=""
  fi
  echo "   $label: clean tree, HEAD ${current_head:0:12}, code-review-graph ${current_crg:-unreadable}, dsh ${current_dsh:-unreadable}"
done

# ---- plan ----------------------------------------------------------------------------------
echo
echo "== plan =="
MAC_REMOTE="$(choose_remote mac "$MAC_CHECKOUT")" || MAC_REMOTE=""
VM_REMOTE="$(choose_remote dockerVM "$VM_CHECKOUT")" || VM_REMOTE=""
if [ -z "$MAC_REMOTE" ] || [ -z "$VM_REMOTE" ]; then
  echo "FATAL: cannot find a git remote carrying $DSH_REPO in one of the harness checkouts." >&2
  echo "       mac='$MAC_REMOTE' dockerVM='$VM_REMOTE'. Nothing was changed." >&2
  exit 1
fi
for spec in "mac|$MAC_CHECKOUT|$MAC_PY|$MAC_REMOTE" "dockerVM|$VM_CHECKOUT|$VM_PY|$VM_REMOTE"; do
  host="${spec%%|*}"
  rest="${spec#*|}"
  checkout="${rest%%|*}"
  rest="${rest#*|}"
  py="${rest%%|*}"
  remote="${rest#*|}"
  echo "   $host"
  echo "     pip:   $py -m pip install --upgrade code-review-graph==$CRG_VERSION"
  echo "     git:   git -C $checkout fetch $remote && git -C $checkout reset --hard $SHA"
  echo "     guard: tree re-checked clean immediately before the reset"
done

if [ "$DRY" = 1 ]; then
  echo
  echo "== dry run: nothing above was executed, nothing was changed =="
  print_followups
  exit 0
fi

# ---- apply, both hosts or abort ------------------------------------------------------------
MUTATED=""        # the last host this run actually changed

apply_host() {
  local host="$1" label="$2" py="$3" checkout="$4" remote="$5"
  echo
  echo "== $label: code-review-graph $CRG_VERSION =="
  if ! run_on "$host" "$py" -m pip install --upgrade "code-review-graph==$CRG_VERSION"; then
    echo "FAILED: pip install on $label." >&2
    return 1
  fi
  MUTATED="$host"

  # The brief's ordering: re-check clean immediately before moving the tree. A path that turned
  # dirty between preflight and here means something else is writing; refuse.
  local status
  status="$(git_status_of "$host" "$checkout")" || {
    echo "FAILED: cannot check the $label tree before reset." >&2
    return 1
  }
  if [ -n "$status" ]; then
    echo "REFUSED: the $label tree became dirty before the reset:" >&2
    printf '%s\n' "$status" | sed 's/^/         /' >&2
    return 1
  fi

  echo "== $label: harness -> $SHA =="
  if ! run_on "$host" env GIT_OPTIONAL_LOCKS=0 git -C "$checkout" fetch "$remote"; then
    echo "FAILED: git fetch on $label." >&2
    return 1
  fi
  if ! run_on "$host" env GIT_OPTIONAL_LOCKS=0 git -C "$checkout" reset --hard "$SHA"; then
    echo "FAILED: git reset --hard $SHA on $label." >&2
    return 1
  fi

  local got_crg got_head
  got_crg="$(crg_version_of "$host" "$py")" || got_crg=""
  case "$got_crg" in
    *"$CRG_VERSION"*) echo "   verified: $label runs code-review-graph $CRG_VERSION" ;;
    *) echo "FAILED: $label reports '$got_crg' after the install." >&2; return 1 ;;
  esac
  got_head="$(head_of "$host" "$checkout")" || got_head=""
  case "$got_head" in
    "$SHA"*) echo "   verified: $label harness HEAD ${got_head:0:12}" ;;
    *) echo "FAILED: $label HEAD is '$got_head', not $SHA." >&2; return 1 ;;
  esac
  return 0
}

partial_abort() { # ahead_host detail
  cat >&2 <<EOF

###############################################################################################
#  PARTIAL APPLY — $1 IS AHEAD. THIS IS NOT SUCCESS; DO NOT WALK AWAY.
#
#  $2
#
#  $1 has code-review-graph $CRG_VERSION and/or the harness at $SHA; the other host may still be
#  on the old pair. Graph schema $GRAPH_SCHEMA is forward-only: an old binary cannot read a graph
#  the upgraded host has opened. Finish BOTH hosts in this window, or roll $1 back by hand.
#  Re-run ./mac/parity-apply.sh once the cause is fixed — its preflight re-checks both hosts.
###############################################################################################
EOF
  exit 3
}

if ! apply_host mac "mac" "$MAC_PY" "$MAC_CHECKOUT" "$MAC_REMOTE"; then
  if [ "$MUTATED" = mac ]; then
    partial_abort "mac" "the mac step failed after it had already changed mac; dockerVM was never touched."
  fi
  echo "FATAL: the mac step failed before anything changed. dockerVM was not touched." >&2
  exit 1
fi

if ! apply_host dockerVM "dockerVM" "$VM_PY" "$VM_CHECKOUT" "$VM_REMOTE"; then
  partial_abort "mac" "the dockerVM step failed after mac was fully applied, so mac is ahead of dockerVM."
fi
echo
echo "== applied: both hosts are on code-review-graph $CRG_VERSION and dsh $SHA =="
print_followups
