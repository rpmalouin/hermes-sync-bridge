#!/usr/bin/env python3
"""The mailbox message contract, in code.

`SCHEMA.md` in the queue is CANONICAL — the contract has to live where both hosts can read
it, so this module does not restate it authoritatively, it *implements* it and checks itself
against it (`test_protocol.py`, and `python3 protocol.py --check-schema`).

Two rules in here are load-bearing and easy to get wrong:

1. **A published message body is immutable.** `state/<message_id>.attempt` is the
   authoritative attempt counter and `state/<message_id>.status` is the authoritative
   verdict — neither belongs in the envelope. The whole claim-by-rename protocol exists to
   make the queue safe without a lock, and it only holds because nothing rewrites a body.
   Do not "simplify" by adding `status` or `attempt_count` to the envelope.
2. **`kind: task` is a promise.** Such a message must reach a reasoning session; a loop must
   never apply it, even when its `task_type` looks routine. (`ping` is exempt: its apply is a
   no-op.)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- paths
DEFAULT_QUEUE = Path("/Volumes/appdata/hermes-Mac-Updates")
DEFAULT_SCHEMA = DEFAULT_QUEUE / "SCHEMA.md"
SCHEMA_PATH = Path(os.environ.get("MAILBOX_SCHEMA") or DEFAULT_SCHEMA)

# ---------------------------------------------------------------- envelope
# The exact field set documented in SCHEMA.md § Envelope. `test_protocol.py` fails if these
# two ever disagree, in either direction.
ENVELOPE_FIELDS = (
    "message_id", "author", "consumer", "created_at", "task_type",
    "kind", "schema", "attempt", "in_reply_to", "payload",
)
REQUIRED_FIELDS = ("message_id", "author", "created_at", "task_type")

# Fields that must NOT appear: the envelope is immutable after publish, so anything mutable
# belongs in state/. Kept explicit so the error message can explain the why.
FORBIDDEN_FIELDS = ("status", "attempt_count", "attempts", "state", "claimed_by", "processed_at")

# Attempts are counted in state/<message_id>.attempt; the envelope copy is advisory only.
ATTEMPT_IS_ADVISORY = True
CURRENT_SCHEMA = 1

# ---------------------------------------------------------------- task types
TASK_TYPES = ("dev_update", "ping", "task_request")
# Slug canonicalization is wire-format hygiene, not semantics: reject on meaning, never on
# punctuation (`dev-update` == `dev_update`). Word aliases fold too.
TASK_TYPE_ALIASES = {"request": "task_request", "ask": "task_request"}

# ---------------------------------------------------------------- kinds
KINDS = ("notification", "task")
TASK_KIND_CONTRACT = (
    "kind=task must reach a reasoning session and is never applied by a consumer loop; "
    "it is parked even when task_type looks routine."
)

# ---------------------------------------------------------------- state verdicts
# state/<message_id>.status holds {phase, attempts, last_reason, author} for rejections;
# state/<message_id>.request.json holds a parked ask's verdict. These are the legal values.
REQUEST_STATUSES = ("pending", "done", "expired")
# Reading aid: the vault's request note renders `done` as "closed".

# ---------------------------------------------------------------- helpers
_SAFE_ID = re.compile(r"^[^/\\]+$")


def canonical_task_type(value) -> str:
    """Fold case, hyphens and spaces; map word aliases. Unknown slugs survive unchanged so the
    caller can reject them deliberately rather than have them guessed at."""
    slug = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return TASK_TYPE_ALIASES.get(slug, slug)


def is_addressed_to(message: dict, consumer_id: str) -> bool:
    """Routing: a message with no `consumer` is for anyone; `any` likewise."""
    addr = str(message.get("consumer") or "").strip()
    return addr in ("", "any", consumer_id)


def validate_envelope(message: dict) -> list:
    """Return a list of problems; empty means valid. Never mutates the message."""
    problems = []
    if not isinstance(message, dict):
        return ["envelope is not a JSON object"]

    for field in REQUIRED_FIELDS:
        if not str(message.get(field) or "").strip():
            problems.append(f"missing envelope field: {field}")

    for field in FORBIDDEN_FIELDS:
        if field in message:
            problems.append(
                f"field {field!r} does not belong in a published envelope — a body is immutable "
                f"(the authoritative value lives in state/<message_id>.status or .attempt)"
            )

    task_type = canonical_task_type(message.get("task_type"))
    if task_type and task_type not in TASK_TYPES:
        problems.append(f"unknown task_type {message.get('task_type')!r}; expected one of {', '.join(TASK_TYPES)}")

    kind = str(message.get("kind") or "").strip()
    if kind and kind not in KINDS:
        problems.append(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")

    mid = str(message.get("message_id") or "")
    if mid and not _SAFE_ID.match(mid):
        problems.append(f"message_id {mid!r} may not contain '/', '\\' or '..'")

    reply = message.get("in_reply_to")
    if reply is not None and not _SAFE_ID.match(str(reply)):
        problems.append(f"in_reply_to {reply!r} may not contain '/', '\\' or '..'")

    if "payload" in message and not isinstance(message.get("payload"), (dict, list)):
        problems.append("payload must be an object or array")

    if message.get("schema") not in (None, CURRENT_SCHEMA):
        problems.append(f"unknown schema version {message.get('schema')!r}; this code speaks {CURRENT_SCHEMA}")

    attempt = message.get("attempt")
    if attempt is not None and not isinstance(attempt, int):
        problems.append("attempt must be an integer (advisory copy; state/<id>.attempt is authoritative)")

    return problems


# ---------------------------------------------------------------- drift check vs SCHEMA.md
def documented_fields(schema_path: Path = SCHEMA_PATH) -> set:
    """Field names in SCHEMA.md's § Envelope json block. Raises if the block cannot be found."""
    text = Path(schema_path).read_text()
    idx = text.find("## Envelope")
    if idx == -1:
        raise ValueError(f"{schema_path}: no '## Envelope' section")
    block = re.search(r"```json\s*(\{.*?\})\s*```", text[idx:], re.S)
    if not block:
        raise ValueError(f"{schema_path}: no json block under '## Envelope'")
    return set(json.loads(block.group(1)).keys())


def schema_drift(schema_path: Path = SCHEMA_PATH) -> list:
    """Compare this module's envelope field set with the canonical document, both directions."""
    documented = documented_fields(schema_path)
    code = set(ENVELOPE_FIELDS)
    return [f"documented but not in code: {sorted(documented - code)}" if documented - code else None,
            f"in code but not documented: {sorted(code - documented)}" if code - documented else None]


def main() -> int:
    ap = argparse.ArgumentParser(description="Mailbox contract: validate envelopes, check schema drift.")
    ap.add_argument("--check-schema", action="store_true",
                    help="compare ENVELOPE_FIELDS with SCHEMA.md; exit 1 on drift")
    ap.add_argument("--validate", metavar="FILE", help="validate one envelope JSON file")
    ap.add_argument("--require-schema", action="store_true",
                    help="with --check-schema: treat an unreadable SCHEMA.md as a failure")
    a = ap.parse_args()

    if a.check_schema:
        try:
            drift = [d for d in schema_drift() if d]
        except (OSError, ValueError) as e:
            print(("FAIL: " if a.require_schema else "SKIPPED: ") + f"cannot read {SCHEMA_PATH} ({e})")
            return 1 if a.require_schema else 0
        if drift:
            print("SCHEMA DRIFT")
            for d in drift:
                print("  -", d)
            return 1
        print(f"in sync with {SCHEMA_PATH} ({len(ENVELOPE_FIELDS)} envelope fields)")
        return 0

    if a.validate:
        problems = validate_envelope(json.loads(Path(a.validate).read_text()))
        if problems:
            print(f"INVALID {a.validate}")
            for p in problems:
                print("  -", p)
            return 1
        print(f"valid: {a.validate}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
