#!/usr/bin/env python3
"""Contract tests for shared/protocol.py.

Run: python3 shared/test_protocol.py [--require-schema]

The first test is the one that matters: it fails if this repo's envelope field set and the
queue's canonical SCHEMA.md disagree in either direction. That is the whole point of keeping
the code and the document apart — two copies that nothing checks are two copies that drift
(the vault's own skills drifted exactly this way and cost a night of work).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol  # noqa: E402

RESULTS = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    return ok


def valid_envelope(**over):
    e = {"message_id": "20260922T131530Z-93377141", "author": "hermes-mac", "consumer": "hermes-docker",
         "created_at": "2026-09-22T13:15:30Z", "task_type": "task_request", "kind": "task",
         "schema": 1, "attempt": 0, "in_reply_to": None, "payload": {"request": "x"}}
    e.update(over)
    return e


def test_no_schema_drift(require_schema: bool) -> None:
    print("\n1. the code and the canonical document agree")
    try:
        drift = [d for d in protocol.schema_drift() if d]
    except (OSError, ValueError) as e:
        msg = f"cannot read {protocol.SCHEMA_PATH} ({e})"
        if require_schema:
            check("SCHEMA.md readable", False, msg)
        else:
            print(f"  SKIP  SCHEMA.md not readable here — {msg}")
        return
    check("no envelope drift with SCHEMA.md", not drift, "; ".join(drift))
    check("the documented set is the set we validate", protocol.documented_fields() == set(protocol.ENVELOPE_FIELDS))


def test_envelope_rules():
    print("\n2. envelope validation")
    check("a well-formed envelope passes", protocol.validate_envelope(valid_envelope()) == [],
          str(protocol.validate_envelope(valid_envelope())))
    for f in protocol.REQUIRED_FIELDS:
        bad = valid_envelope(); bad[f] = ""
        check(f"a missing {f} is rejected", any(f in p for p in protocol.validate_envelope(bad)))
    check("unknown task_type is rejected, not guessed",
          any("unknown task_type" in p for p in protocol.validate_envelope(valid_envelope(task_type="dev_update_v2"))))
    check("unknown kind is rejected",
          any("unknown kind" in p for p in protocol.validate_envelope(valid_envelope(kind="urgent"))))
    check("a path-y message_id is rejected",
          any("message_id" in p for p in protocol.validate_envelope(valid_envelope(message_id="../etc/passwd"))))
    check("a future schema version is rejected",
          any("schema version" in p for p in protocol.validate_envelope(valid_envelope(schema=2))))
    check("validation never mutates the message",
          (lambda e: (protocol.validate_envelope(e), e == valid_envelope())[1])(valid_envelope()))


def test_body_immutability():
    print("\n3. a published body is immutable — status and counters live in state/")
    for f in ("status", "attempt_count", "attempts", "claimed_by"):
        problems = protocol.validate_envelope(valid_envelope(**{f: "anything"}))
        check(f"{f!r} in the envelope is refused", any(f in p for p in problems),
              "the guard is the only thing standing between us and a rewritten body")
    check("attempt is accepted as an advisory integer", protocol.validate_envelope(valid_envelope(attempt=2)) == [])
    check("a non-integer attempt is refused", protocol.validate_envelope(valid_envelope(attempt="2")) != [])
    check("the legal request verdicts are the ones the vault renders",
          protocol.REQUEST_STATUSES == ("pending", "done", "expired"))


def test_routing():
    print("\n4. routing")
    m = valid_envelope(consumer="hermes-mac")
    check("addressed to me", protocol.is_addressed_to(m, "hermes-mac"))
    check("not addressed to the other side", not protocol.is_addressed_to(m, "hermes-docker"))
    check("'any' reaches both", protocol.is_addressed_to(valid_envelope(consumer="any"), "hermes-docker"))
    check("an address-less message reaches both", protocol.is_addressed_to(valid_envelope(consumer=""), "hermes-mac"))


def test_slug_canonicalization():
    print("\n5. slugs fold on punctuation, never on meaning")
    for raw, want in (("dev-update", "dev_update"), ("DEV_UPDATE", "dev_update"), ("dev update", "dev_update"),
                      ("request", "task_request"), ("ask", "task_request"), ("task_request", "task_request")):
        check(f"{raw!r} -> {want!r}", protocol.canonical_task_type(raw) == want)
    check("an unknown slug survives for a deliberate rejection",
          protocol.canonical_task_type("dev_update_v2") == "dev_update_v2")


def test_drift_check_catches_a_real_edit():
    print("\n6. the drift check actually bites (negative test)")
    text = ("## Envelope\n\n```json\n"
            + json.dumps({k: "" for k in protocol.ENVELOPE_FIELDS if k != "kind"})
            + "\n```\n")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(text)
        path = Path(fh.name)
    drift = [d for d in protocol.schema_drift(path) if d]
    check("a field removed from the document is reported", any("kind" in d for d in drift), "; ".join(drift))
    path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-schema", action="store_true",
                    help="fail instead of skipping when the queue's SCHEMA.md is unreachable")
    a = ap.parse_args()
    test_no_schema_drift(a.require_schema)
    test_envelope_rules()
    test_body_immutability()
    test_routing()
    test_slug_canonicalization()
    test_drift_check_catches_a_real_edit()
    passed = sum(1 for _l, ok, _d in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
