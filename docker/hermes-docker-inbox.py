#!/usr/bin/env python3
"""hermes-docker inbox — the DockerVM half of the hermes-Mac-Updates mailbox.

The Mac side runs `ingest.py` for mail addressed `hermes-mac`. This is the same
shape for mail addressed `hermes-docker` (or `any`), which until now sat unclaimed
in `new/` by design.

It is a CONSUMER, not an agent: it claims, routes and records, and it never
executes anything a message asks for. A `task_request` (or any `kind: task`
message) is parked in `requests/` with a `state/<id>.request.json` record and
reported as NEEDS ACTION — closing it is a reasoning session's job, not this
script's.

What it does, per message in `new/` addressed to this consumer:
  claim   atomic rename new/<f> -> processing/hermes-docker/<f>   (the contract's claim)
  record  state/<id>.done            for dev_update / ping, archived to processed/YYYY-MM/
          state/<id>.request.json    for an ask, parked in requests/ (status: pending)
          state/<id>.attempt + back to new/ on a malformed envelope, failed/<f> + .reason
                                     after 3 attempts, matching the contract's lifecycle
  report  one stdout line per action (the cron job's output), and an append-only digest at
          ~/.hermes/state/hermes-docker-inbox.log so a later session can see what arrived

Never: writes the vault, moves mail addressed to another consumer, sweeps `tmp/`,
executes a request, or deletes a directory. State files it writes are namespaced to
this consumer (`hermes-docker-inbox.state.json`, plus the per-message `.done` /
`.attempt` / `.request.json` records the contract defines).

Robustness rules learned the hard way (2026-09-22): a dotfile stray in `new/` (a macOS
`._<name>.json` AppleDouble sidecar) is NOT a message — it is counted and skipped, never
read, because it is binary and parsing one killed the whole run, costing the real mail
queued behind it. One unreadable message never ends the run either: it is skipped, or
handed back to `new/` if it was already claimed. And because this consumer only ever
globs `new/`, a run killed mid-loop would leave claims nobody revisits — so every start
reaps its own claims older than 15 minutes back into `new/`.

Usage:  hermes-docker-inbox.py [--dry-run] [--quiet]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

Q = Path("/appdata/hermes-Mac-Updates")
NEW = Q / "new"
TMP = Q / "tmp"
PROCESSING = Q / "processing" / "hermes-docker"
REQUESTS = Q / "requests"
PROCESSED = Q / "processed"
FAILED = Q / "failed"
STATE = Q / "state"

ME = "hermes-docker"
CLAIMABLE = (ME, "any")
KNOWN_TASK_TYPES = ("dev_update", "ping", "task_request")
SCHEMA = 1
MAX_ATTEMPTS = 3

STATE_FILE = STATE / "hermes-docker-inbox.state.json"
LOCK_FILE = STATE / ".hermes-docker-inbox.lock"
DIGEST = Path("/root/.hermes/state/hermes-docker-inbox.log")
STALE_LOCK_SECONDS = 600
STALE_CLAIM_SECONDS = 900


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def local_stamp() -> str:
    return now().astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def digest(line: str) -> None:
    DIGEST.parent.mkdir(parents=True, exist_ok=True)
    with open(DIGEST, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp()} {line}\n")


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    tmp = STATE / ".hermes-docker-inbox.state.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp, STATE_FILE)


def write_json(path: Path, doc: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp, path)


def attempt_count(message_id: str) -> int:
    path = STATE / f"{message_id}.attempt"
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def set_attempt(message_id: str, count: int) -> None:
    path = STATE / f"{message_id}.attempt"
    tmp = STATE / f".{message_id}.attempt.tmp"
    tmp.write_text(f"{count}\n", encoding="utf-8")
    os.rename(tmp, path)


def archive(claimed: Path, month: str) -> Path:
    target_dir = PROCESSED / month
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / claimed.name
    os.rename(claimed, target)
    return target


def reap_stale_claims(counted: dict, lines: list) -> None:
    """Return our own stale claims to new/ before looking for work.

    A run killed mid-loop leaves messages claimed in processing/hermes-docker/ that this consumer
    never revisits, because it only ever globs new/. Without this, one crash costs mail silently.
    Mirrors the contract's 15-minute reaper: only claims, never `new/`, and never another
    consumer's directory.
    """
    for claimed in sorted(PROCESSING.glob("*.json")):
        if claimed.name.startswith("."):
            counted["strays"] += 1
            continue
        try:
            age = int(now().timestamp() - claimed.stat().st_mtime)
        except OSError:
            continue
        if age < STALE_CLAIM_SECONDS:
            continue
        try:
            os.rename(claimed, NEW / claimed.name)
        except OSError:
            continue
        counted["reaped"] += 1
        lines.append(f"REAP {claimed.stem}: stale claim ({age}s old) returned to new/")
        digest(f"REAP {claimed.stem}: stale claim ({age}s old) returned to new/")


def apply_message(claimed: Path, message_id: str, message: dict, counted: dict,
                  lines: list) -> None:
    """Reject, park or archive one claimed message. Mutates `counted` and appends to `lines`."""
    author = message.get("author")
    task_type = message.get("task_type")
    malformed = message.get("schema") != SCHEMA or task_type not in KNOWN_TASK_TYPES

    if malformed:
        tries = attempt_count(message_id) + 1
        set_attempt(message_id, tries)
        reason = ("schema is not 1" if message.get("schema") != SCHEMA
                  else f"unknown task_type {task_type!r}")
        write_json(STATE / f"{message_id}.status",
                   {"phase": "rejected", "attempts": tries, "last_reason": reason,
                    "author": author, "at": stamp()})
        if tries >= MAX_ATTEMPTS:
            FAILED.mkdir(parents=True, exist_ok=True)
            os.rename(claimed, FAILED / claimed.name)
            (FAILED / f"{claimed.name}.reason").write_text(
                f"{stamp()} rejected after {tries} attempts: {reason}\n", encoding="utf-8")
            counted["failed"] += 1
            lines.append(f"FAILED {message_id}: {reason} (attempts={tries})")
            digest(f"FAILED {message_id} from {author}: {reason} (attempts={tries})")
        else:
            os.rename(claimed, NEW / claimed.name)
            counted["requeued"] += 1
            lines.append(f"REQUEUE {message_id}: {reason} (attempts={tries})")
            digest(f"REQUEUE {message_id} from {author}: {reason} (attempts={tries})")
        return

    payload = message.get("payload") or {}
    in_reply_to = message.get("in_reply_to")

    if task_type == "task_request" or message.get("kind") == "task":
        parked = REQUESTS / claimed.name
        os.rename(claimed, parked)
        write_json(STATE / f"{message_id}.request.json", {
            "message_id": message_id, "author": author, "consumer": message.get("consumer"),
            "status": "pending", "parked_at": local_stamp(), "created_at": message.get("created_at"),
            "request": payload.get("request"), "target": payload.get("target"),
            "requires": payload.get("requires"), "acceptance": payload.get("acceptance"),
            "source": payload.get("source"), "expires_at": payload.get("expires_at") or "",
            "reply_expected": payload.get("reply_expected"),
            "file": claimed.name,
        })
        counted["parked"] += 1
        counted["needs_action"] += 1
        lines.append(f"NEEDS ACTION {message_id} from {author}: {payload.get('request')}")
        digest(f"PARKED {message_id} from {author}: {payload.get('request')} "
               f"[acceptance: {payload.get('acceptance')}]")
        return

    summary = payload.get("summary") or payload.get("dev_item") or "(no summary)"
    archive(claimed, (message.get("created_at") or stamp())[:7])
    (STATE / f"{message_id}.done").write_text(
        f"{local_stamp()} {task_type} from {author}\n", encoding="utf-8")
    counted["consumed"] += 1
    reply_note = f" [receipt for {in_reply_to}]" if in_reply_to else ""
    if task_type == "dev_update":
        lines.append(f"READ {message_id} from {author}{reply_note}: {summary}")
        digest(f"READ {message_id} ({task_type}) from {author}{reply_note}: {summary}")
        for detail in payload.get("details") or []:
            digest(f"    - {detail}")
    else:
        lines.append(f"READ {message_id} from {author}{reply_note}: {task_type}")
        digest(f"READ {message_id} ({task_type}) from {author}{reply_note}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Consume mail addressed to hermes-docker")
    parser.add_argument("--dry-run", action="store_true", help="report planned actions, change nothing")
    parser.add_argument("--quiet", action="store_true", help="print nothing when there is no work")
    args = parser.parse_args()

    if not NEW.is_dir():
        print(f"inbox {Q} is not there (Hatchdoor is down and so is its queue?)", file=sys.stderr)
        return 1
    for d in (PROCESSING, REQUESTS, PROCESSED, STATE):
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)

    hold = None
    if not args.dry_run:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            hold = fd
        except FileExistsError:
            age = now().timestamp() - LOCK_FILE.stat().st_mtime
            if age < STALE_LOCK_SECONDS:
                if not args.quiet:
                    print(f"busy: another run holds the lock ({int(age)}s old)")
                return 0
            LOCK_FILE.unlink(missing_ok=True)
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            hold = fd

    counted = {"consumed": 0, "parked": 0, "requeued": 0, "failed": 0, "skipped": 0,
               "needs_action": 0, "reaped": 0, "strays": 0}
    lines = []
    try:
        if not args.dry_run:
            reap_stale_claims(counted, lines)
        for path in sorted(NEW.glob("*.json")):
            if path.name.startswith("."):
                # An AppleDouble sidecar (._<name>.json) matches *.json and is binary. It is not
                # a message (SCHEMA.md), and reading one used to crash the run outright.
                counted["strays"] += 1
                continue
            try:
                message = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                counted["skipped"] += 1
                lines.append(f"SKIP {path.name}: unreadable ({type(exc).__name__}: {exc})")
                continue

            message_id = str(message.get("message_id") or path.stem)
            consumer = message.get("consumer")
            if consumer not in CLAIMABLE:
                counted["skipped"] += 1
                continue  # not ours: leave it exactly where it is

            if args.dry_run:
                kind = message.get("task_type")
                if kind == "task_request" or message.get("kind") == "task":
                    lines.append(f"WOULD PARK {message_id} ({kind})")
                    counted["needs_action"] += 1
                elif kind in ("dev_update", "ping"):
                    lines.append(f"WOULD READ+ARCHIVE {message_id} ({kind})")
                    counted["consumed"] += 1
                else:
                    lines.append(f"WOULD REJECT {message_id} (unknown task_type {kind!r})")
                    counted["failed"] += 1
                continue

            claimed = PROCESSING / path.name
            try:
                os.rename(path, claimed)
            except OSError:
                counted["skipped"] += 1
                continue

            try:
                apply_message(claimed, message_id, message, counted, lines)
            except Exception as exc:  # noqa: BLE001
                # One message must not cost the whole run, and it must not strand the claim
                # either: hand it back to new/ so the next tick tries it again.
                try:
                    os.rename(claimed, NEW / claimed.name)
                except OSError:
                    pass
                counted["requeued"] += 1
                lines.append(f"REQUEUE {message_id}: unexpected error "
                             f"({type(exc).__name__}: {exc})")
                digest(f"REQUEUE {message_id}: unexpected error ({type(exc).__name__}: {exc})")
    finally:
        if hold is not None:
            os.close(hold)
            LOCK_FILE.unlink(missing_ok=True)

    if not args.dry_run:
        state = read_state()
        state.update({
            "last_run": local_stamp(),
            "last_result": " ".join(f"{k}={v}" for k, v in counted.items()),
            "addressed_to": ME,
        })
        write_state(state)

    if lines:
        print("\n".join(lines))
    elif not args.quiet:
        print(f"idle: nothing addressed to {ME} "
              f"(new/ holds {len(list(NEW.glob('*.json')))} file(s) for other consumers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
