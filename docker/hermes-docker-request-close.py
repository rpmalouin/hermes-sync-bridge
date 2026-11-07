#!/usr/bin/env python3
"""Close a request parked in the DockerVM side of the mailbox.

The consumer parks and mirrors; only a reasoning session closes — this is the tool a
session runs. It archives the parked message, marks state/<id>.request.json done, and
publishes the contract's receipt: `dev_update` + `change_type: modified` +
`in_reply_to` = the request id, authored `hermes-docker`, addressed back to whoever
asked.

Usage:
    hermes-docker-request-close.py <message_id> --summary "what was done"
    hermes-docker-request-close.py --list
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

Q = Path("/appdata/hermes-Mac-Updates")
REQUESTS = Q / "requests"
PROCESSED = Q / "processed"
STATE = Q / "state"
DIGEST = Path("/root/.hermes/state/hermes-docker-inbox.log")
CLAIMABLE = ("hermes-docker", "any")
CONSUMERS = ("hermes-mac", "hermes-docker")


def now_local() -> str:
    """Local wall-clock stamp WITH its offset — same fix as hermes-docker-inbox.py:local_stamp().

    `closed_at` lands in the same state record the Mac's age_hours() reads, so leaving it naive
    while fixing `parked_at` would be the same defect class half-done.
    """
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(line: str) -> None:
    DIGEST.parent.mkdir(parents=True, exist_ok=True)
    with open(DIGEST, "a", encoding="utf-8") as handle:
        handle.write(f"{now_z()} {line}\n")


def write_json(path: Path, doc: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp, path)


def parked_requests() -> list[Path]:
    """Parked mail, filtered to this consumer's own asks.

    `requests/` is a SHARED namespace: the Mac side parks the asks it receives in the very same
    directory, so counting its mail as ours invents pending work for this host (the mirror of the
    bug they fixed on their side, where our parked asks read as their unactioned items). The
    parked message's OWN `consumer` field decides, not the directory. An unreadable file counts as
    ours: hiding an ask is worse than double-showing one.
    """
    mine = []
    for path in sorted(REQUESTS.glob("*.json")):
        if path.name.startswith("._"):
            continue
        try:
            message = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            mine.append(path)
            continue
        if message.get("consumer") in CLAIMABLE:
            mine.append(path)
    return mine


def main() -> int:
    parser = argparse.ArgumentParser(description="Close a parked mailbox request")
    parser.add_argument("message_id", nargs="?", help="the request's message_id (also its filename stem)")
    parser.add_argument("--summary", default=None, help="what was done — goes in the receipt")
    parser.add_argument("--list", action="store_true", help="list parked requests and exit")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen")
    args = parser.parse_args()

    if args.list or not args.message_id:
        found = 0
        for path in parked_requests():
            message = json.loads(path.read_text(encoding="utf-8"))
            record = STATE / f"{path.stem}.request.json"
            status = json.loads(record.read_text(encoding="utf-8")).get("status") if record.exists() else "?"
            print(f"{path.stem}  consumer={message.get('consumer'):13} author={message.get('author'):13} status={status}")
            print(f"    {(message.get('payload') or {}).get('request', '')[:160]}")
            found += 1
        if not found:
            print("no parked requests")
        return 0 if args.message_id else 0

    parked = REQUESTS / f"{args.message_id}.json"
    if not parked.is_file():
        print(f"no parked request {args.message_id} in {REQUESTS}", file=sys.stderr)
        return 1
    message = json.loads(parked.read_text(encoding="utf-8"))
    if message.get("consumer") not in CLAIMABLE:
        print(f"refusing to close {args.message_id}: addressed to {message.get('consumer')!r}", file=sys.stderr)
        return 1

    payload = message.get("payload") or {}
    author = message.get("author") if message.get("author") in CONSUMERS else "hermes-mac"
    summary = args.summary or f"Closed: {payload.get('request', args.message_id)}"

    if args.dry_run:
        print(f"would archive requests/{parked.name} -> processed/{message.get('created_at', '')[:7]}/")
        print(f"would publish a receipt to {author}: {summary}")
        print(f"would then mark state/{args.message_id}.request.json done with receipt_message_id = the published id")
        return 0

    # Publish FIRST, then archive and record. A receipt is the only evidence the ask was answered, so
    # the record must never say "done" before one exists on the wire; and the receipt's own id is
    # written back, so a closed ask can be traced to its receipt without scanning processed/ — the
    # shape the Mac records in mac/ingest.py:1067. A failed publish leaves the ask parked and the
    # record untouched, which makes a retry a plain retry.
    receipt = subprocess.run(
        [sys.executable, str(Q / "generate.py"),
         "--item", f"request-closed-{args.message_id}",
         "--type", "modified", "--task-type", "dev_update", "--kind", "notification",
         "--consumer", author, "--in-reply-to", args.message_id,
         "--summary", summary,
         "--detail", f"Closed by the DockerVM session. Request was: {payload.get('request', '')}",
         "--detail", f"Acceptance as set by the asker: {payload.get('acceptance', '(none given)')}",
         "--detail", f"Parked message archived to processed/{(message.get('created_at') or '')[:7]}/; "
                     f"state/{args.message_id}.request.json will read done with this receipt's id."],
        capture_output=True, text=True)
    if receipt.returncode != 0:
        print("receipt publish failed (ask left parked, record untouched):",
              receipt.stderr.strip(), file=sys.stderr)
        return 1
    receipt_path = Path(receipt.stdout.strip())
    receipt_id = receipt_path.stem

    target_dir = PROCESSED / (message.get("created_at") or now_z())[:7]
    target_dir.mkdir(parents=True, exist_ok=True)
    os.rename(parked, target_dir / parked.name)

    record_path = STATE / f"{args.message_id}.request.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    record.update({"status": "done", "closed_at": now_local(), "closed_by": "hermes-docker",
                   "close_summary": summary, "receipt_message_id": receipt_id})
    write_json(record_path, record)

    print(f"closed {args.message_id}: archived, marked done, receipt {receipt_id} -> {receipt_path}")
    digest(f"CLOSED {args.message_id} (asked by {author}): {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
