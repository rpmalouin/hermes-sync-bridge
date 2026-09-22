#!/usr/bin/env python3
"""Dev Change Exporter — producer half of the hermes-Mac-Updates dispatch queue.

This directory is a Maildir-style queue. One message per file, JSON only. The
contract of record is SCHEMA.md beside this script (envelope, layout, lifecycle,
routing, ownership); this producer implements the producer half of it and nothing
else. Read the contract of record, not a summary of it, before changing behaviour.

    tmp/                        staging — write + fsync here, then rename out
    new/                        published, awaiting the consumer
    processing/<consumer>/      consumer-side: claimed by the Mac
    requests/                   consumer-side: parked asks awaiting a reasoning session
    processed/<YYYY-MM>/        consumer-side: done
    failed/                     consumer-side: attempts exhausted
    state/                      consumer-side counters/markers/verdicts

Producer rules this script obeys (the consumer owns everything else):

  * Publish is atomic and rename-only: write tmp/<message_id>.json, flush,
    fsync, close, then os.rename() into new/. Both directories are on the same
    filesystem, so the inode survives and a reader never sees a partial file
    (proven on this share — the inode survives a cross-directory rename).
  * A file that already lives in new/ is NEVER written, edited or truncated.
    The destination is checked before the rename, and the message id carries
    8 random hex digits so a collision cannot silently overwrite a message.
  * state/, processing/, requests/, processed/ and failed/ are CONSUMER-owned:
    this script reads them, never writes them. Attempt counting lives in
    state/<message_id>.attempt, maintained by the consumer's requeue and failure
    paths; the envelope's `attempt` field is an advisory copy — send 0, never
    bump it here.
  * tmp/ is not swept. A crashed writer's corpse is the consumer's to prune.
  * Directories are created idempotently and are NEVER deleted.
  * generate.py, SCHEMA.md, recent-updates.json and dotfiles are not messages
    and are never swept into the queue; nothing here enumerates this directory.
  * A rejected message is never re-published: read state/<message_id>.status for
    the reason, fix the producer fault here, and ask the Mac side to requeue.

Envelope (schema 1):
    message_id   "<YYYYMMDDThhmmssZ>-<8 hex>"   also the filename stem
    author       "hermes-docker" | "hermes-mac"
    consumer     "hermes-mac" | "hermes-docker" | "any"
    created_at   ISO 8601 UTC ("YYYY-MM-DDTHH:MM:SSZ")
    task_type    canonical slug: "dev_update" | "ping" | "task_request"
    kind         "notification" | "task"
    schema       1
    attempt      0                              advisory copy only
    in_reply_to  null | <message_id>            the request a receipt answers
    payload      object, schema defined per task_type

Two modes — and a dev_update is NEVER the one to use for an ask:

  dev_update, a notification about work already done (applying it IS the outcome):
    generate.py --item NAME --type new|modified|completed \\
                [--stack STACK] --summary "..." \\
                [--detail "ARTIFACT"] [--detail "..."] ... \\
                [--card]           ask the consumer to write/update an Activity Kanban
                                   card (opt-in since 2026-09-22: WITHOUT this flag a
                                   dev_update is logged only, whatever change_type says;
                                   with it, dev_item must equal the card title /
                                   **Task:** text exactly or a second card is created)

  task_request, an ask a consumer parks and never executes:
    generate.py --task-type task_request --request "one-line imperative ask" \\
                [--target "..."] [--requires "..."]... [--acceptance "..."] \\
                [--source <message_id|log line>] [--expires-at <ISO-8601>] \\
                [--no-reply-expected]

  generate.py --ensure-layout     create the layout, publish nothing

Prints the published message path.
"""
import argparse
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- queue layout (created idempotently, never deleted) -----------------------
TMP = HERE / "tmp"
NEW = HERE / "new"
PROCESSING = HERE / "processing"
REQUESTS = HERE / "requests"
PROCESSED = HERE / "processed"
FAILED = HERE / "failed"
STATE = HERE / "state"

AUTHOR = "hermes-docker"
DEFAULT_CONSUMER = "hermes-mac"
DEFAULT_TASK_TYPE = "dev_update"
CONSUMERS = ("hermes-mac", "hermes-docker", "any")
KINDS = ("notification", "task")
SCHEMA = 1

# Canonical task_type slugs, per SCHEMA.md. An unknown slug is rejected by the
# consumer with the known values named in the reason, so the producer refuses it
# up front instead of burning a queue round trip: agree a new slug by mail first.
KNOWN_TASK_TYPES = ("dev_update", "ping", "task_request")
# SCHEMA.md alias: a bare `request` canonicalizes to `task_request`.
SLUG_ALIASES = {"request": "task_request"}
# kind is not decoration: `task` carries the contract that the message must reach
# a reasoning session and is NEVER applied by the consumer's loop.
DEFAULT_KIND_BY_TYPE = {
    "dev_update": "notification",
    "ping": "notification",
    "task_request": "task",
}

INDEX_NAME = "recent-updates.json"
INDEX_CAP = 50
SLUG_SEP_RE = re.compile(r"[\s-]+")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_slug(value: str) -> str:
    """SCHEMA.md canonicalization: case, hyphens and spaces fold to underscores."""
    slug = SLUG_SEP_RE.sub("_", value.strip().lower())
    return SLUG_ALIASES.get(slug, slug)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 stamp, tolerating the trailing Z (python 3.10)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def valid_message_id(value: str) -> bool:
    """SCHEMA.md: any id without '/', '\\' or '..' is accepted (ours are stricter)."""
    return bool(value) and "/" not in value and "\\" not in value and ".." not in value


def layout_paths(now: datetime | None = None) -> list[Path]:
    """The queue layout: this month's processed/ dir included."""
    month = (now or utcnow()).strftime("%Y-%m")
    return [
        TMP,
        NEW,
        PROCESSING / "hermes-mac",
        PROCESSING / "hermes-docker",
        REQUESTS,
        PROCESSED / month,
        FAILED,
        STATE,
    ]


def ensure_layout(now: datetime | None = None) -> list[Path]:
    """Create the queue layout if missing. Idempotent; never deletes anything."""
    created = []
    for path in layout_paths(now):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
    return created


def fsync_dir(path: Path) -> None:
    """Persist the rename itself (the directory entry) where the fs allows it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def new_message_id(now: datetime) -> tuple[str, str]:
    """'<YYYYMMDDThhmmssZ>-<8 hex>' -> also the filename stem.

    The stamp is fixed-width UTC so lexical filename order == chronological
    order; the random suffix keeps same-second messages unique. The id is the
    idempotency key: unique per message, never reused for different content.
    """
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for _ in range(100):
        stem = f"{stamp}-{secrets.token_hex(4)}"
        if not (NEW / f"{stem}.json").exists():
            return stem, f"{stem}.json"
    raise RuntimeError(f"no free message id for {stamp} after 100 attempts")


def publish(doc: dict, filename: str) -> Path:
    """Atomic publish: tmp/<file> -> new/<file>. Never touches an existing message."""
    tmp_path = TMP / filename
    final_path = NEW / filename
    if final_path.exists():
        raise FileExistsError(f"refusing to overwrite a queued message: {final_path}")

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.rename(tmp_path, final_path)
    fsync_dir(TMP)
    fsync_dir(NEW)
    return final_path


def update_index(payload: dict) -> Path:
    """Rolling index of the last INDEX_CAP dev_update payloads, newest first.

    Requests are deliberately NOT indexed: the index is the Mac's shortcut for
    dev-work notifications, while an ask is surfaced through the consumer's
    `Hermes/Mailbox Requests.md` mirror and state/<id>.request.json.
    The index is not a message and generate.py is not a message: neither is queued.
    """
    index_path = HERE / INDEX_NAME
    recent = []
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                recent = loaded
        except (json.JSONDecodeError, OSError):
            recent = []
    recent.insert(0, payload)
    recent = recent[:INDEX_CAP]

    tmp_path = HERE / f"{INDEX_NAME}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(recent, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp_path, index_path)
    fsync_dir(HERE)
    return index_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a message to the mailbox queue")
    parser.add_argument("--item", help="dev_update: name of the development item")
    parser.add_argument("--type", dest="change_type", choices=["new", "modified", "completed"],
                        help="dev_update: what happened to that item")
    parser.add_argument("--stack", default=None, help="dev_update: affected container/stack, or omit")
    parser.add_argument("--summary", help="dev_update: what changed (one or two sentences)")
    parser.add_argument("--detail", action="append", default=[],
                        help="dev_update: repeatable detail/artifact")
    parser.add_argument("--card", action="store_true",
                        help="dev_update: opt in to a board write — the consumer writes or "
                             "updates an Activity Kanban card (new -> the producer's (Active) "
                             "column, completed -> Done). Omitted, a dev_update is logged only, "
                             "however change_type reads (SCHEMA.md, 2026-09-22). If set, dev_item "
                             "must equal the card title / **Task:** text exactly, or the consumer "
                             "adds a second card instead of updating the first.")
    parser.add_argument("--request", help="task_request: the one-line imperative ask (required)")
    parser.add_argument("--target", default=None,
                        help="task_request: the artifact(s), path plus what changes")
    parser.add_argument("--requires", action="append", default=[],
                        help="task_request: repeatable prerequisite of the executor")
    parser.add_argument("--acceptance", default=None,
                        help="task_request: how the asker judges it done, measurably")
    parser.add_argument("--source", default=None,
                        help="task_request: message_id or log line backing the ask")
    parser.add_argument("--expires-at", default=None,
                        help="task_request: ISO-8601 instant after which the ask closes unexecuted")
    parser.add_argument("--no-reply-expected", dest="reply_expected", action="store_false",
                        default=True,
                        help="task_request: suppress the receipt (default: a receipt IS wanted — "
                             "mail addressed to this host is read within 5 minutes)")
    parser.add_argument("--task-type", default=DEFAULT_TASK_TYPE,
                        help=f"canonical task_type slug (default {DEFAULT_TASK_TYPE})")
    parser.add_argument("--kind", default=None, choices=list(KINDS),
                        help="override the kind (task_request defaults to task, others notification)")
    parser.add_argument("--consumer", default=DEFAULT_CONSUMER, choices=list(CONSUMERS))
    parser.add_argument("--in-reply-to", default=None,
                        help="the request id this message answers (receipts)")
    parser.add_argument("--ensure-layout", action="store_true",
                        help="create the queue layout and exit without publishing")
    args = parser.parse_args()

    now = utcnow()
    created = ensure_layout(now)

    if args.ensure_layout:
        for path in layout_paths(now):
            print(f"{'created' if path in created else 'exists ':7} {path}")
        return 0

    task_type = canonical_slug(args.task_type)
    if task_type not in KNOWN_TASK_TYPES:
        parser.error(
            f"--task-type {args.task_type!r} canonicalizes to {task_type!r}, which is not one of "
            f"{'/'.join(KNOWN_TASK_TYPES)}. The consumer rejects unknown slugs (SCHEMA.md) — agree "
            f"the slug by mail before using it.")

    kind = args.kind or DEFAULT_KIND_BY_TYPE[task_type]
    if task_type == "task_request" and kind != "task":
        parser.error("a task_request must be kind=task: the consumer parks kind=task and never "
                     "applies it, which is the whole point of an ask")
    if task_type == "dev_update" and kind == "task":
        parser.error("refusing dev_update + kind=task: the consumer parks ANY kind=task message "
                     "instead of applying it, so this notification would never be logged or carded "
                     "— send an ask as --task-type task_request")

    if args.in_reply_to is not None and not valid_message_id(args.in_reply_to):
        parser.error(f"--in-reply-to must be an id without path separators, got {args.in_reply_to!r}")

    if args.card and task_type != "dev_update":
        parser.error(f"--card only applies to a dev_update (got --task-type {task_type}): a "
                     f"task_request is parked and never applied, and a ping's payload is ignored "
                     f"— neither can move a card.")

    if task_type == "task_request":
        if not args.request:
            parser.error("--request is required for --task-type task_request")
        if not args.acceptance:
            print("warning: no --acceptance — that is a request nobody can check (SCHEMA.md)",
                  file=sys.stderr)
        payload = {"request": args.request}
        if args.target:
            payload["target"] = args.target
        if args.requires:
            payload["requires"] = args.requires
        if args.acceptance:
            payload["acceptance"] = args.acceptance
        if args.source:
            payload["source"] = args.source
        if args.expires_at:
            if not ISO_RE.match(args.expires_at.strip()):
                parser.error(f"--expires-at must be ISO-8601, got {args.expires_at!r}")
            if parse_iso(args.expires_at) <= now:
                parser.error(f"--expires-at {args.expires_at!r} is not in the future: the consumer "
                             f"would archive the request as expired without a receipt")
            payload["expires_at"] = args.expires_at.strip()
        payload["reply_expected"] = bool(args.reply_expected)
    else:
        missing = [name for name, value in
                   (("--item", args.item), ("--type", args.change_type), ("--summary", args.summary))
                   if not value]
        if missing:
            parser.error("missing required argument(s) for a dev_update: " + ", ".join(missing))
        stack = args.stack
        if stack is not None and stack.strip().lower() in ("null", "none"):
            stack = None
        payload = {
            "dev_item": args.item,
            "change_type": args.change_type,
            "affected_container_or_stack": stack,
            "summary": args.summary,
            "details": args.detail,
            "generated_at": iso_z(now),
        }
        if args.card:
            payload["card"] = True

    stem, filename = new_message_id(now)
    doc = {
        "message_id": stem,
        "author": AUTHOR,
        "consumer": args.consumer,
        "created_at": iso_z(now),
        "task_type": task_type,
        "kind": kind,
        "schema": SCHEMA,
        "attempt": 0,
        "in_reply_to": args.in_reply_to,
        "payload": payload,
    }

    final_path = publish(doc, filename)
    if task_type == "dev_update":
        update_index(payload)

    print(str(final_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
