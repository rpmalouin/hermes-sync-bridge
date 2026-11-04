#!/usr/bin/env python3
"""Hermes-Mac mailbox consumer (Maildir-style dispatch queue).

Drains the DockerVM-shared mailbox at /Volumes/appdata/hermes-Mac-Updates and
applies each message to the Obsidian vault (dockerVM log + Activity Kanban),
rotating the local rolling "recent updates" memory.

QUEUE LAYOUT (producer-owned except where noted)
    tmp/                     in-flight writes; producer publishes from here
    new/                     published, unclaimed messages
    processing/hermes-mac/   claims held by this consumer   (this consumer owns)
    processing/hermes-docker/  claims held by the other consumer
    processed/YYYY-MM/       archived after success
    failed/                  poison-pill quarantine (+ <name>.reason)
    state/                   attempt counters + done markers (this consumer owns)

MESSAGE ENVELOPE (JSON only; schema 1)
    message_id   "<YYYYMMDDThhmmssZ>-<8 hex>"   also the filename stem
    author       "hermes-mac" | "hermes-docker"
    consumer     "hermes-mac" | "hermes-docker" | "any"
    created_at   ISO 8601 UTC
    task_type    short slug; unknown slugs are rejected into failed/
    kind         "notification" | "task"
    schema       1
    attempt      advisory copy only - the authoritative count lives in state/
    in_reply_to  null | <message_id>   (reserved; no reply path yet)
    payload      object, schema defined per task_type

ATOMIC PROTOCOL - every state change is a rename, never an in-place edit
    publish   tmp/<f> -> new/<f>                       (producer)
    claim     new/<f> -> processing/hermes-mac/<f>     (consumer, atomic)
    success                   -> processed/YYYY-MM/<f>
    failure, attempts left    -> back to new/<f>
    failure, attempts spent   -> failed/<f> + failed/<f>.reason

INTENTIONAL GUARDS (do not remove):
  * Never read a message in place from new/ - always claim by rename first.
  * Never mutate a message body. Attempt counting lives in state/<id>.attempt.
  * A message already marked state/<id>.done is never applied to the vault
    twice; it is archived and counted as a duplicate (the reaper guarantees
    at-least-once, so the apply must be idempotent).
  * Dotfiles (.DS_Store, ._*) and the producer's own files (generate.py,
    recent-updates.json) are never treated as messages.
  * The legacy root-level dev-update-*.json path is RETIRED (2026-09-22): a file
    appearing there is reported as a stray and never consumed, never archived.
  * Kanban edits use the plugin-safe card format, only on a board that parses
    cleanly, with a .bak written first; a structural surprise skips the kanban
    step (never corrupts) while log/memory steps still run.
  * stdout is silent unless something changed. The mailman does not announce
    that there was no mail.

Usage:
    python3 ingest.py [--watch DIR] [--vault DIR] [--dry-run]
                      [--provision] [--lease-minutes N] [--max-attempts N]
                      [--prune] [--status]
                      [--enqueue FILE] [--task-type SLUG] [--author NAME]
    Env overrides: MAILBOX_ROOT, VAULT_ROOT, MAILBOX_LEASE_MINUTES,
                   MAILBOX_MAX_ATTEMPTS, HERMES_HOME
    Instance file: VAULT_ROOT may also come from $SYNC_BRIDGE_ENV
                   (default ~/.hermes/sync-bridge.env) — the repo's own defaults are placeholders.
"""
import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def read_text_fresh(path: Path) -> str:
    """Read a share-backed file with the local page cache bypassed.

    macOS caches CIFS reads in the unified buffer cache, so a pathname the
    other host just reused can serve the PREVIOUS file's bytes -- correct
    length, wrong content, NUL padded. That parses as "Extra data" and
    quarantines a perfectly valid message. F_NOCACHE sends the read to the
    server, and the read refreshes the cached copy for every later open.
    """
    try:
        import fcntl

        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
            chunks = []
            while True:
                block = os.read(fd, 1 << 20)
                if not block:
                    break
                chunks.append(block)
        finally:
            os.close(fd)
        return b"".join(chunks).decode("utf-8")
    except (ImportError, AttributeError, OSError):
        return path.read_text()


# ---- instance paths -----------------------------------------------------------------------
# This repo ships PLACEHOLDER paths on purpose: the real vault location is per-machine state and
# belongs in neither git nor the published artefact. The instance supplies it in one of two ways,
# in this order: the process environment (VAULT_ROOT), or a key=value file outside the repo
# ($SYNC_BRIDGE_ENV, default ~/.hermes/sync-bridge.env). An explicit --vault beats both.
# The same helper is duplicated in mac/kanban-card.py and mac/kanban-archive-roll.py: the three
# files are installed into two different directories, so they must stay standalone.
INSTANCE_ENV = Path(os.environ.get("SYNC_BRIDGE_ENV") or (Path.home() / ".hermes/sync-bridge.env"))


def instance_var(key: str) -> str | None:
    """Value of KEY from the instance file, or None. A missing file is not an error."""
    try:
        text = INSTANCE_ENV.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'") or None
    return None


def vault_root_default() -> Path:
    return Path(os.environ.get("VAULT_ROOT") or instance_var("VAULT_ROOT") or "/Volumes/Data/vault")


WATCH_DEFAULT = Path(os.environ.get("MAILBOX_ROOT") or "/Volumes/appdata/hermes-Mac-Updates")
VAULT_DEFAULT = vault_root_default()
KANBAN_REL = Path("Hermes/Hermes Kanban.md")
LOGS_REL = Path("Hermes/Hermes Logs/dockerVM")
COEDIT_REL = Path("Co-edit log.md")
MEMORY_CAP = 20
SCHEMA = 1

CONSUMER_ID = "hermes-mac"
OTHER_CONSUMER_ID = "hermes-docker"

LEASE_MINUTES = int(os.environ.get("MAILBOX_LEASE_MINUTES") or 15)
MAX_ATTEMPTS = int(os.environ.get("MAILBOX_MAX_ATTEMPTS") or 3)
TMP_MAX_AGE_MIN = 60
PROCESSED_KEEP_MONTHS = 12
DONE_KEEP_DAYS = 90

# task_type -> handler. Unknown slugs go to failed/ rather than being guessed at.
TASK_TYPES = ("dev_update", "ping", "task_request")
# canonical-slug aliases (canonical_task_type folds case/hyphen/space; this folds words)
TASK_TYPE_ALIASES = {"request": "task_request", "ask": "task_request", "task_request": "task_request"}
# a task_request payload must carry this; the rest are optional-but-documented
REQUEST_FIELDS = ("request", "target", "requires", "acceptance", "source", "expires_at", "reply_expected")
REQUEST_REQUIRED = ("request",)

# change types that touch the kanban board
KANBAN_TYPES = {"new", "completed"}
COLUMN_FOR = {"new": "## Hermes-dockerVM (Active)", "completed": "## Done"}
STATUS_FOR = {"new": "active", "completed": "done"}


def hermes_home() -> Path:
    h = os.environ.get("HERMES_HOME")
    return Path(h) if h else Path.home() / ".hermes"


def canonical_task_type(v) -> str:
    """Slug canonicalization is wire format, not semantics.

    `dev-update` and `dev_update` are the same intent, and a hyphen/underscore
    split is not worth a three-attempt quarantine — reject on *meaning*, never on
    punctuation. A short alias table folds the word choice too. Genuinely unknown
    slugs are still rejected.
    """
    s = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
    return TASK_TYPE_ALIASES.get(s, s)


def safe_id(mid: str) -> str:
    """State filenames derive from message_id; sanitize rather than reject.

    An id carrying `+`, `@` or a space is still a perfectly good id, and refusing
    it would be the same handshake footgun the slug normalization just removed.
    """
    return re.sub(r"[^A-Za-z0-9_.:-]", "-", str(mid or "")).strip(".-") or "unnamed"


# --------------------------------------------------------------------------
# listing helpers
#
# pathlib's glob() does NOT skip dotfiles the way shell glob does, so "*.json"
# happily matches macOS AppleDouble stubs ("._20260922T001252Z-07f611ea.json")
# that this Mac creates beside every file it writes to the SMB share. Every
# listing of the queue goes through here so a stub can never be claimed,
# parsed, counted, or archived as a message.
# --------------------------------------------------------------------------
def is_visible(p: Path) -> bool:
    return not p.name.startswith(".")


def list_msgs(d: Path, pattern: str = "*.json") -> list:
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob(pattern) if is_visible(p))


def list_dirs(d: Path) -> list:
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir() and is_visible(p))


def _sweep_dir(d: Path, dry_run: bool, require_sibling: bool) -> int:
    """Remove metadata litter in one directory. Returns the count removed."""
    if not d.is_dir():
        return 0
    n = 0
    for p in d.iterdir():
        if not p.is_file():
            continue
        if p.name == ".DS_Store":
            n += 1
            if not dry_run:
                p.unlink()
            continue
        if not p.name.startswith("._"):
            continue
        # In the producer's own directories (mailbox root, processing/, processed/)
        # only remove a stub whose real sibling is present, so nothing that might
        # be meaningful over there is ever touched.
        if require_sibling and not (d / p.name[2:]).exists():
            continue
        n += 1
        if not dry_run:
            p.unlink()
    return n


def drop_stub(p: Path) -> int:
    """Silently remove the AppleDouble stub macOS just created for p.

    Used for files this consumer writes itself (the state file): the stub is our
    own litter, so it is cleaned without being counted or reported - otherwise
    every idle pass would announce `stubs=1` and a one-minute cron would talk
    forever about nothing.
    """
    stub = p.parent / f"._{p.name}"
    if stub.exists():
        try:
            stub.unlink()
            return 1
        except OSError:
            return 0
    return 0


def sweep_stubs(root: Path, dry_run: bool) -> int:
    """Remove macOS metadata litter (._* / .DS_Store) from the queue.

    These are 4 KB AppleDouble stubs, not messages - one is created beside every
    file AND directory this Mac writes to the SMB share. Inside the consumer's own
    directories they are always litter; in the producer's directories
    (mailbox root, processing/, processed/) a stub is only removed when its real
    sibling exists. Left alone they accumulate on the dockerVM volume forever.
    """
    L = layout(root)
    n = 0
    for d in (L["tmp"], L["new"], L["failed"], L["state"], L["proc"], L["proc_other"], L["requests"]):
        n += _sweep_dir(d, dry_run, require_sibling=False)
    for d in list_dirs(L["processing"]) + list_dirs(L["processed"]):
        n += _sweep_dir(d, dry_run, require_sibling=False)
    for d in (L["root"], L["processing"], L["processed"]):
        n += _sweep_dir(d, dry_run, require_sibling=True)
    return n


# --------------------------------------------------------------------------
# emit helpers
# --------------------------------------------------------------------------
def out(msg: str = "") -> None:
    if msg:
        print(msg)


def warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr)


def error(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# layout + bookkeeping
# --------------------------------------------------------------------------
def layout(root: Path) -> dict:
    return {
        "root": root,
        "tmp": root / "tmp",
        "new": root / "new",
        "processing": root / "processing",
        "proc": root / "processing" / CONSUMER_ID,
        "proc_other": root / "processing" / OTHER_CONSUMER_ID,
        "requests": root / "requests",
        "processed": root / "processed",
        "failed": root / "failed",
        "state": root / "state",
    }


def provision(root: Path, apply: bool = True) -> list:
    """Create the full layout idempotently. Never deletes. Returns created paths."""
    made = []
    L = layout(root)
    for key in ("tmp", "new", "proc", "proc_other", "requests", "processed", "failed", "state"):
        p = L[key]
        if not p.is_dir():
            made.append(str(p))
            if apply:
                p.mkdir(parents=True, exist_ok=True)
    return made


def state_file(root: Path) -> Path:
    return root / "state" / "hermes-mac-ingest.state.json"


def load_state(root: Path) -> dict:
    p = state_file(root)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(root: Path, data: dict) -> None:
    p = state_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def attempts_path(root: Path, message_id: str) -> Path:
    return root / "state" / f"{safe_id(message_id)}.attempt"


def read_attempts(root: Path, message_id: str) -> int:
    p = attempts_path(root, message_id)
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return 0


def write_attempts(root: Path, message_id: str, n: int) -> None:
    p = attempts_path(root, message_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{n}\n")


def done_path(root: Path, message_id: str) -> Path:
    return root / "state" / f"{safe_id(message_id)}.done"


def status_path(root: Path, message_id: str) -> Path:
    return root / "state" / f"{safe_id(message_id)}.status"


def write_status(root: Path, message_id: str, attempts: int, reason: str, phase: str, author: str = "") -> None:
    """Leave a machine-readable verdict the producer can read over the share.

    This exists because the producer cannot read this Mac's skill, logs or stdout.
    When a message is rejected, the *reason* has to be visible from the other side
    of the wire, or the next person spends an afternoon guessing which field was
    wrong. Cleared on success.
    """
    p = status_path(root, message_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "message_id": message_id,
        "consumer": CONSUMER_ID,
        "phase": phase,                # rejected | apply_failed | applied
        "attempts": attempts,
        "last_reason": reason,
        "author": author,
        "at": stamp_now(),
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    drop_stub(p)


def clear_status(root: Path, message_id: str) -> None:
    p = status_path(root, message_id)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    drop_stub(p)


# --------------------------------------------------------------------------
# normalization (legacy payload shape, kept verbatim from the old ingester)
# --------------------------------------------------------------------------
def normalize_stack(v):
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "null", "none", "n/a", "na"):
            return None
        return v.strip()
    return str(v)


def normalize_details(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)]


def normalize_item(raw: dict, fallback_mtime: float) -> dict:
    """Return a normalized dev_update item or raise ValueError."""
    name = (raw.get("dev_item") or raw.get("name") or "").strip()
    ctype = (raw.get("change_type") or "").strip().lower()
    summary = (raw.get("summary") or "").strip()
    if not name or not ctype or not summary:
        raise ValueError("missing dev_item/change_type/summary")
    if ctype not in ("new", "modified", "completed"):
        ctype = "modified"  # log anyway; never kanban
    ts = raw.get("generated_at") or raw.get("timestamp") or ""
    if not ts:
        ts = datetime.fromtimestamp(fallback_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "name": name,
        "change_type": ctype,
        "stack": normalize_stack(raw.get("affected_container_or_stack") or raw.get("stack")),
        "summary": summary,
        "details": normalize_details(raw.get("details")),
        "timestamp": ts,
        # Carding is opt-in. The vault-side writer holds the pen: a producer that can write the
        # board itself announces with change_type=modified (logged, never carded), and only a
        # producer that *cannot* write asks for a card explicitly. Inferring a card from
        # change_type alone double-cards any task whose producer also wrote its own card — the
        # identity check in upsert_card() matches the card's **Task:** text, which need not equal
        # dev_item (their `dockervm-board-discipline` vs the card's longer title).
        "card": raw.get("card") is True or str(raw.get("card") or "").strip().lower() in ("true", "yes", "1"),
    }


# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------
REQUIRED_FIELDS = ("message_id", "author", "created_at", "task_type")


def read_envelope(path: Path) -> dict:
    """Load + validate an envelope. Raises ValueError with a reason."""
    try:
        raw = json.loads(read_text_fresh(path))
    except json.JSONDecodeError as e:
        raise ValueError(f"unparseable JSON: {e}")
    except OSError as e:
        raise ValueError(f"unreadable: {e}")
    if not isinstance(raw, dict):
        raise ValueError("envelope is not a JSON object")
    missing = [k for k in REQUIRED_FIELDS if not str(raw.get(k) or "").strip()]
    if missing:
        raise ValueError(f"missing envelope field(s): {', '.join(missing)}")
    mid = str(raw["message_id"]).strip()
    if not mid or "/" in mid or "\\" in mid or ".." in mid or len(mid) > 200:
        raise ValueError(f"unusable message_id: {mid!r}")
    schema = raw.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise ValueError(f"unsupported schema: {schema} (this consumer speaks {SCHEMA})")
    declared = str(raw["task_type"]).strip()
    canonical = canonical_task_type(declared)
    if canonical not in TASK_TYPES:
        raise ValueError(f"unknown task_type: {declared!r} (known task_type values: {', '.join(TASK_TYPES)})")
    raw["task_type"] = canonical
    raw["_task_type_coerced"] = canonical != declared
    raw["_task_type_declared"] = declared
    payload = raw.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    raw.setdefault("payload", {})
    if canonical == "task_request":
        validate_request_payload(raw["payload"])
    return raw


def validate_request_payload(payload: dict) -> dict:
    """A request must say what is being asked; everything else is optional.

    Rejecting a *request* for a missing optional field would be the same
    handshake footgun as rejecting `dev-update` for a hyphen, so only `request`
    is mandatory. `kind` is forced to "task" because that word carries the
    contract: must reach a reasoning session, never applied by the loop.
    """
    missing = [f for f in REQUEST_REQUIRED if not str(payload.get(f) or "").strip()]
    if missing:
        raise ValueError(f"task_request payload missing: {', '.join(missing)} "
                         f"(expected fields: {', '.join(REQUEST_FIELDS)})")
    reqs = payload.get("requires")
    if reqs is None:
        payload["requires"] = []
    elif isinstance(reqs, str):
        payload["requires"] = [reqs] if reqs.strip() else []
    elif isinstance(reqs, list):
        payload["requires"] = [str(x) for x in reqs if str(x).strip()]
    else:
        raise ValueError("task_request payload 'requires' must be a list of strings")
    payload["reply_expected"] = bool(payload.get("reply_expected", True))
    exp = payload.get("expires_at")
    if exp:
        try:
            datetime.strptime(str(exp).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            raise ValueError(f"task_request payload 'expires_at' is not ISO-8601 UTC: {exp!r}")
    return payload


def make_envelope(payload: dict, author: str, task_type: str, kind: str = "notification",
                  consumer: str = None) -> dict:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    slug = canonical_task_type(task_type)
    if kind == "notification" and slug == "task_request":
        kind = "task"          # the word carries the contract: park, never apply
    return {
        "message_id": f"{stamp}-{uuid.uuid4().hex[:8]}",
        "author": author,
        "consumer": consumer or CONSUMER_ID,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_type": slug,
        "kind": kind,
        "schema": SCHEMA,
        "attempt": 0,
        "in_reply_to": None,
        "payload": payload,
    }


def publish(root: Path, envelope: dict) -> Path:
    """Atomic publish: write to tmp/, then rename into new/. Never a partial in new/."""
    L = layout(root)
    name = f"{envelope['message_id']}.json"
    tmp = L["tmp"] / name
    dst = L["new"] / name
    for p in (L["tmp"], L["new"]):
        p.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, dst)
    # macOS creates an AppleDouble `._<name>` stub beside every file it puts on an SMB share, and a
    # POSIX rename does not carry the stub along — so one is left behind in tmp/ and one can appear
    # beside the published file. Inside the queue they are litter, but not harmless litter: on
    # 2026-09-22 a stub in new/ raised UnicodeDecodeError in the *other* host's consumer and killed
    # its run, leaving the mail queued behind it unseen for three minutes. Ours, so it is cleaned
    # without being counted (same rule as the state file).
    drop_stub(tmp)
    drop_stub(dst)
    return dst


# --------------------------------------------------------------------------
# rolling "memory" (last 20)
# --------------------------------------------------------------------------
def load_recent(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def push_recent(path: Path, item: dict) -> None:
    recent = [item] + [x for x in load_recent(path) if x.get("name") != item["name"]]
    recent = recent[:MEMORY_CAP]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recent, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Obsidian log (daily consolidated note, newest-first)
# --------------------------------------------------------------------------
def esc_links(text) -> str:
    """A message payload must never become a vault link.

    Dev-update details routinely quote example markup - the DockerVM's drift-job
    prompt contains `[[wikilink]]` and `[[X]]` as illustrations - and writing those
    verbatim into a note creates unresolved links in the vault graph. Two did, on
    2026-09-22. Escape them into code spans at render time.
    """
    return re.sub(r"(!?\[\[[^\]\n]*\]\])", lambda m: f"`{m.group(1)}`", str(text))


def entry_text(item: dict) -> str:
    stack = item["stack"] or "(none)"
    lines = [
        f"## {item['name']} — {item['change_type']}",
        "",
        f"- **Timestamp:** {item['timestamp']}",
        f"- **Stack:** {stack}",
        f"- **Summary:** {esc_links(item['summary'])}",
        "- **Details:**",
    ] + [f"  - {esc_links(d)}" for d in item["details"]]
    return "\n".join(lines) + "\n"


def note_header(day: str) -> str:
    return (
        "---\n"
        "tags: [hermes-log, dev-updates, dockerVM]\n"
        "---\n"
        "\n"
        f"# Dev Updates — {day}\n"
    )


def log_append(logs_dir: Path, item: dict) -> Path:
    day = item["timestamp"][:10] if len(item["timestamp"]) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note = logs_dir / f"Dev Updates {day}.md"
    header = note_header(day)
    if note.exists():
        text = note.read_text()
        m = re.match(r"^(?P<head>---\n.*?\n---\n\n# Dev Updates[^\n]*\n)", text, re.DOTALL)
        if m:
            note.write_text(m.group("head") + "\n" + entry_text(item) + text[m.end():])
        else:
            # header shape diverged (externally edited) - prepend, never splice
            note.write_text(header + "\n" + entry_text(item) + "\n" + text)
    else:
        note.write_text(header + "\n" + entry_text(item) + "\n")
    return note


# --------------------------------------------------------------------------
# Co-edit log (attribution: whoever applied the message writes the entry)
# --------------------------------------------------------------------------
def coedit_append(vault: Path, message: dict, note: Path, changed: list) -> None:
    p = vault / COEDIT_REL
    if not p.exists():
        warn(f"co-edit log missing: {p}")
        return
    entry = (
        f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} — **{CONSUMER_ID}** (mailbox "
        f"`{message['message_id']}` from {message['author']}) → "
        f"`{note.relative_to(vault)}`"
        + (" · `Hermes/Hermes Kanban.md`" if changed else "")
        + f" — **{message['task_type']}**: {message['payload'].get('summary', '')[:160]}\n"
    )
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(entry)


# --------------------------------------------------------------------------
# Kanban board editing (plugin-safe, conservative) - unchanged from v1
# --------------------------------------------------------------------------
def parse_sections(board: str):
    parts = re.split(r"^(## .+)$", board, flags=re.MULTILINE)
    sections = [("pre", parts[0])]
    i = 1
    while i < len(parts):
        sections.append(("heading", parts[i]))
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append(("body", body))
        i += 2
    return sections


def rebuild(sections) -> str:
    return "".join(v for _k, v in sections)


def body_blocks(body: str):
    blocks, cur = [], []
    for ln in body.splitlines():
        if not ln.strip():
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def is_card(blk: list) -> bool:
    return bool(blk) and bool(re.match(r"^\s*- \[[ x]\]", blk[0]))


def block_task(blk: list) -> str:
    for ln in blk:
        m = re.match(r"^\s{4}-\s\*\*Task:\*\*\s*(.*)$", ln)
        if m:
            return m.group(1).strip()
    return blk[0].strip()[len("- [ ] "):] if blk else ""


def make_card(item: dict, log_link: str) -> list:
    task = item["name"]
    status = STATUS_FOR.get(item["change_type"], "active")
    # The plugin derives "completed" from the CHECKBOX, not from our Status field, and its
    # Archive action only moves checked cards — so a done card must be written checked.
    box = "x" if status == "done" else " "
    return [
        f"- [{box}] {task}",
        f"    - **Task:** {task}",
        "    - **Node:** dockerVM",
        f"    - **Context:** [[{log_link}]]",
        f"    - **Status:** {status}",
        f"    - **Notes:** {esc_links(item['summary'])}",
    ]


def upsert_card(board: str, item: dict, log_link: str) -> str:
    target_col = COLUMN_FOR.get(item["change_type"])
    if target_col is None:
        return board
    sections = parse_sections(board)

    found_tail = None
    for idx, (kind, _val) in enumerate(sections):
        if kind != "body":
            continue
        blocks = body_blocks(_val)
        kept, removed = [], []
        for blk in blocks:
            if is_card(blk) and block_task(blk) == item["name"]:
                removed.append(blk)
            else:
                kept.append(blk)
        if removed:
            sections[idx] = ("body", _join_blocks(kept, _val))
            found_tail = removed[-1]

    card = found_tail
    new_card = make_card(item, log_link) if card is None else _apply_fields(card, item, log_link)

    inserted = False
    for idx, (kind, val) in enumerate(sections):
        if kind == "heading" and val == target_col:
            body_val = sections[idx + 1][1] if idx + 1 < len(sections) else ""
            block_text = "\n".join(new_card)
            sections[idx + 1] = ("body", "\n" + block_text + "\n\n" + body_val.lstrip("\n"))
            inserted = True
            break
    if not inserted:
        raise ValueError(f"kanban column {target_col!r} not found; leaving board untouched")

    return rebuild(sections)


def _join_blocks(blocks: list, original_body: str) -> str:
    if not blocks:
        return "\n"
    return "\n".join("\n".join(b) for b in blocks) + "\n\n\n"


def _apply_fields(card: list, item: dict, log_link: str) -> list:
    lines = list(card)
    status = STATUS_FOR.get(item["change_type"], "active")
    out_lines = []
    for ln in lines:
        m = re.match(r"^(\s*- \[)[ x](\] .*)$", ln)
        if m:
            out_lines.append(f"{m.group(1)}{'x' if status == 'done' else ' '}{m.group(2)}")
        elif re.match(r"^\s{4}-\s\*\*Status:\*\*", ln):
            out_lines.append(f"    - **Status:** {status}")
        elif re.match(r"^\s{4}-\s\*\*Context:\*\*", ln):
            out_lines.append(f"    - **Context:** [[{log_link}]]")
        elif re.match(r"^\s{4}-\s\*\*Notes:\*\*", ln):
            out_lines.append(f"    - **Notes:** {esc_links(item['summary'])}")
        else:
            out_lines.append(ln)
    return out_lines


def update_kanban(kanban_path: Path, item: dict, log_link: str) -> None:
    if not kanban_path.exists():
        raise ValueError(f"kanban file missing: {kanban_path}")
    text = kanban_path.read_text()
    kanban_path.with_suffix(".md.bak").write_text(text)
    marker = text.find("%% kanban:settings")
    board, tail = (text[:marker], text[marker:]) if marker != -1 else (text, "")
    kanban_path.write_text(upsert_card(board, item, log_link) + tail)


# --------------------------------------------------------------------------
# apply one message to the vault
# --------------------------------------------------------------------------
def apply_message(message: dict, vault: Path, dry_run: bool) -> list:
    """Apply a validated envelope. Returns the list of vault artifacts changed."""
    task_type = message["task_type"]
    if task_type == "ping":
        return []  # health check: exercises the queue, touches nothing
    if task_type != "dev_update":
        raise ValueError(f"no handler for task_type {task_type!r}")

    item = normalize_item(message["payload"], 0.0)
    kanban_path = vault / KANBAN_REL
    logs_dir = vault / LOGS_REL
    memory_path = hermes_home() / "state" / "hermes-mac-ingest-recent.json"

    if dry_run:
        out(f"[dry-run] would apply dev_update {item['name']} ({item['change_type']})")
        return []

    log_note = log_append(logs_dir, item)
    log_link = f"Hermes/Hermes Logs/dockerVM/{log_note.stem}"
    changed = [log_note]
    if item.get("card") and item["change_type"] in KANBAN_TYPES:
        try:
            update_kanban(kanban_path, item, log_link)
            changed.append(kanban_path)
        except (OSError, ValueError) as e:
            warn(f"kanban update for {item['name']!r} skipped: {e}")
    push_recent(memory_path, item)
    coedit_append(vault, message, log_note, kanban_path in changed)
    return changed


# --------------------------------------------------------------------------
# queue mechanics
# --------------------------------------------------------------------------
def archive(root: Path, path: Path) -> Path:
    bucket = root / "processed" / datetime.now().strftime("%Y-%m")
    bucket.mkdir(parents=True, exist_ok=True)
    dst = bucket / path.name
    if dst.exists():
        dst = bucket / f"{path.stem}.{uuid.uuid4().hex[:6]}{path.suffix}"
    os.rename(path, dst)
    return dst


def fail(root: Path, path: Path, reason: str) -> Path:
    dst = root / "failed" / path.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{dst}.reason", "w", encoding="utf-8") as fh:
        fh.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M')} attempts={read_attempts(root, path.stem)} "
                 f"reason={reason}\n")
    if dst.exists():
        dst = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex[:6]}{dst.suffix}")
    os.rename(path, dst)
    return dst


def requeue(root: Path, path: Path) -> Path:
    dst = root / "new" / path.name
    if dst.exists():
        dst = root / "new" / f"{dst.stem}.{uuid.uuid4().hex[:6]}{dst.suffix}"
    os.rename(path, dst)
    return dst


def claim(root: Path, path: Path) -> tuple:
    """Atomic claim. Returns (claimed_path, note).

    note is None on success, "lost" when another consumer already holds it, or
    "error: ..." when the rename itself failed. A failed claim used to be silent,
    which made "the consumer ran and did nothing" indistinguishable from "the
    consumer could not touch the file" - the exact ambiguity that cost an
    afternoon of guessing across the wire.
    """
    L = layout(root)
    L["proc"].mkdir(parents=True, exist_ok=True)
    dst = L["proc"] / path.name
    if dst.exists():
        return None, "lost"
    try:
        os.rename(path, dst)
    except OSError as e:
        return None, f"error: {e}"
    return dst, None


def probe_identity(path: Path) -> tuple:
    """Best-effort (message_id, author) for a message that may not validate."""
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict):
            return str(raw.get("message_id") or path.stem), str(raw.get("author") or "")
    except (json.JSONDecodeError, OSError):
        pass
    return path.stem, ""


# --------------------------------------------------------------------------
# requests (task_request) — parked for a reasoning session, never executed here
#
# The whole point of this type: an ask is not work the loop may perform, and it
# is not mail that may be swallowed. It is parked where both hosts and the operator can
# see it, it is never failed for going unclaimed, and its pending count is
# reported on every run — the miss that motivated all three rules was a real
# request that got applied as a log line, archived, and counted as applied=1.
# --------------------------------------------------------------------------
REQUEST_NOTE_REL = Path("Hermes/Mailbox Requests.md")
REQUEST_NOTICE_MINUTES = 60


def request_state_path(root: Path, message_id: str) -> Path:
    return root / "state" / f"{safe_id(message_id)}.request.json"


def is_request(message: dict) -> bool:
    """`task_request`, or any non-ping message whose `kind` says task.

    The second clause is the cheap contract applied anyway: `kind: task` means
    "must reach a reasoning session, never applied by the loop", so even a
    producer that types an ask under the wrong slug cannot have it auto-applied.
    """
    if message.get("task_type") == "task_request":
        return True
    if message.get("task_type") == "ping":
        return False
    return str(message.get("kind") or "").strip().lower() == "task"


def claimable(raw: dict) -> bool:
    """Routing, as agreed with the producer: claim iff consumer is me or 'any'."""
    c = str(raw.get("consumer") or "").strip().lower()
    return c in ("", "any", CONSUMER_ID.lower())


def park(root: Path, path: Path, message: dict) -> tuple:
    """Move a claimed request into requests/ and record it. Never executes it."""
    L = layout(root)
    L["requests"].mkdir(parents=True, exist_ok=True)
    dst = L["requests"] / path.name
    if dst.exists():
        dst = L["requests"] / f"{path.stem}.{uuid.uuid4().hex[:6]}{path.suffix}"
    os.rename(path, dst)
    pl = message.get("payload") or {}
    typed = message.get("task_type") == "task_request"
    st = {
        "message_id": message["message_id"],
        "status": "pending",
        "consumer": message.get("consumer"),
        "parked_at": stamp_now(),
        "author": message.get("author"),
        "created_at": message.get("created_at"),
        "typed": typed,
        "file": dst.name,
        "request": str(pl.get("request") or (pl.get("summary") or "")).strip(),
        "target": str(pl.get("target") or "").strip(),
        "requires": pl.get("requires") or [],
        "acceptance": str(pl.get("acceptance") or "").strip(),
        "source": str(pl.get("source") or "").strip(),
        "expires_at": str(pl.get("expires_at") or "").strip(),
        "reply_expected": bool(pl.get("reply_expected", True)),
    }
    p = request_state_path(root, message["message_id"])
    p.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
    drop_stub(p)
    return dst, st


def load_requests(root: Path, only_mine: bool = True) -> list:
    """Parked asks — by default only the ones addressed to THIS consumer.

    Both consumers park into the same shared `requests/` + `state/<id>.request.json` namespace,
    so without this filter an ask the Mac sends the DockerVM shows up here as pending: a false
    "unactioned" alarm, and the mirror note would list somebody else's mail as yours to action.
    The address is read from the parked message itself (authoritative), with the state file's own
    `consumer` as a fallback; an unreadable file counts as mine, because hiding an ask is worse
    than double-showing one.
    """
    out = []
    req_dir = root / "requests"
    for p in list_msgs(root / "state", "*.request.json"):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, dict):
            continue
        if only_mine:
            addr = d.get("consumer")
            msg_file = req_dir / str(d.get("file") or "")
            if msg_file.is_file():
                try:
                    addr = json.loads(msg_file.read_text()).get("consumer", addr)
                except (json.JSONDecodeError, OSError):
                    pass
            if not claimable({"consumer": addr}):
                continue
        out.append(d)
    return sorted(out, key=lambda d: str(d.get("parked_at") or ""))


def stamp_now() -> str:
    """A local state stamp WITH its UTC offset.

    datetime.now() is naive, and strftime("%z") is empty on a naive value - so every
    stamp this module wrote before 2026-10-10 landed with no offset, could not be
    parsed back by age_hours(), and made each fallback return 0.0: every parked ask
    read as seconds old and oldest_pending was meaningless. One helper, one format,
    one place to fix.
    """
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def age_hours(stamp: str) -> float:
    """Hours since a state stamp, tolerant of the offset-less records already on disk."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            t = datetime.strptime(str(stamp), fmt)
        except (ValueError, TypeError):
            continue
        if t.tzinfo is None:            # written before stamp_now(): local time, no offset
            t = t.astimezone()
        return (datetime.now(t.tzinfo) - t).total_seconds() / 3600.0
    return 0.0


def request_counts(root: Path) -> dict:
    reqs = load_requests(root)
    # Requests addressed to the *other* consumer share this state directory. They are not this
    # side's work, but counting them separately is what lets a reader reconcile the two sides
    # without knowing the filter exists — and NOT counting them as ours is what stopped a parked
    # ask looking like this side's pending work (a false "unactioned" alarm, 2026-09-22).
    mine_ids = {r.get("message_id") for r in reqs}
    theirs = [r for r in load_requests(root, only_mine=False) if r.get("message_id") not in mine_ids]
    pending = [r for r in reqs if r.get("status") == "pending"]
    closed = [r for r in reqs if r.get("status") != "pending"]
    oldest = max((age_hours(r.get("parked_at", "")) for r in pending), default=0.0)
    return {"pending": len(pending), "closed": len(closed), "oldest_h": oldest,
            "pending_list": pending, "closed_list": closed,
            "theirs_pending": len([r for r in theirs if r.get("status") == "pending"]),
            "theirs_closed": len([r for r in theirs if r.get("status") != "pending"])}


def archive_request_file(root: Path, d: dict) -> Path | None:
    src = layout(root)["requests"] / str(d.get("file") or "")
    if not src.exists():
        return None
    return archive(root, src)


def close_request(root: Path, message_id: str, how: str, summary: str = "", details: list = None) -> dict:
    """Close a parked request: done (receipt published), expired or declined."""
    p = request_state_path(root, message_id)
    if not p.exists():
        raise ValueError(f"no parked request with id {message_id!r}")
    d = json.loads(p.read_text())
    archived = archive_request_file(root, d)
    d["status"] = how
    d["closed_at"] = stamp_now()
    if summary:
        d["close_summary"] = summary
    receipt = None
    if how == "done" and d.get("reply_expected"):
        receipt = publish_receipt(root, d, summary, details or [])
        d["receipt_message_id"] = receipt.stem
    p.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    drop_stub(p)
    return {"request": d, "archived": str(archived) if archived else None,
            "receipt": str(receipt) if receipt else None}


def publish_receipt(root: Path, d: dict, summary: str, details: list) -> Path:
    """Receipt = the existing dev_update/modified path, with in_reply_to filled in.

    No new receipt type: `change_type: modified` is logged and never cards the
    board, and message_id dedupe already covers a receipt sent twice.
    """
    rid = str(d.get("message_id"))
    payload = {
        "dev_item": f"request-receipt:{rid[-8:]}",
        "change_type": "modified",
        "affected_container_or_stack": None,
        "summary": summary or f"Request {rid} closed as done.",
        "details": details or [f"request: {d.get('request', '')}", f"source: {d.get('source', '')}"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    env = make_envelope(payload, CONSUMER_ID, "dev_update", kind="notification")
    env["consumer"] = str(d.get("author") or OTHER_CONSUMER_ID) or OTHER_CONSUMER_ID
    env["in_reply_to"] = rid
    dst = publish(root, env)
    dst_state = root / "state" / f"{safe_id(env['message_id'])}.receipt"
    dst_state.write_text(
        f"receipt for {rid}\npublished {datetime.now().strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        f"addressed to {env['consumer']} (unclaimed until that consumer exists)\n")
    drop_stub(dst_state)
    return dst


def expire_requests(root: Path, dry_run: bool, notices: list) -> int:
    """Close, do not execute, a request whose expires_at has passed."""
    n = 0
    now = datetime.now(timezone.utc)
    for r in load_requests(root):
        if r.get("status") != "pending" or not r.get("expires_at"):
            continue
        try:
            exp = datetime.strptime(str(r["expires_at"]).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if exp > now:
            continue
        n += 1
        msg = f"request expired unactioned: {r['message_id']} (expired {r['expires_at']})"
        notices.append(msg)
        if dry_run:
            out(f"[dry-run] would close expired request {r['message_id']}")
            continue
        close_request(root, r["message_id"], "expired",
                      summary=f"Closed unexecuted: expired_at {r['expires_at']}")
        out(msg)
    return n


def write_request_note(root: Path, vault: Path, dry_run: bool) -> Path | None:
    """Mirror the parked requests into the vault, which is the surface the operator reads.

    The queue's requests/ directory is machine-visible; this note is human-visible.
    A request that only exists in a queue directory is still effectively invisible.
    """
    rc = request_counts(root)
    if not rc["pending"] and not rc["closed_list"]:
        return None
    rows = []
    for r in rc["pending_list"]:
        age = age_hours(r.get("parked_at", ""))
        req = str(r.get("request", "")).replace("|", "\\|")[:150]
        tgt = str(r.get("target", "")).replace("|", "\\|")[:80]
        acc = str(r.get("acceptance", "")).replace("|", "\\|")[:100]
        rows.append(f"| {age:.1f} h | {req} | {tgt or '—'} | {acc or '—'} | `{r.get('source') or '—'}` | "
                    f"{('**' + str(r['expires_at']) + '**') if r.get('expires_at') else '—'} | "
                    f"`{r['message_id']}` |")
    closed = []
    for r in sorted(rc["closed_list"], key=lambda d: str(d.get("closed_at") or ""), reverse=True)[:5]:
        closed.append(f"| {str(r.get('closed_at') or '')[:16]} | **{r.get('status')}** | "
                      f"{str(r.get('request', '')).replace('|', chr(92) + '|')[:110]} | `{r['message_id']}` |")
    body = (
        "---\n"
        "tags: [hermes, mailbox, requests, dockerVM]\n"
        "status: active\n"
        f"updated: {datetime.now().strftime('%Y-%m-%d')}\n"
        "---\n"
        "\n"
        "# Mailbox Requests\n"
        "\n"
        "Asks **this side received** (`consumer: hermes-mac`) on the DockerVM ↔ Mac mailbox\n"
        "(`task_type: task_request`, or `kind: task`) and are waiting for a **reasoning session** to\n"
        "action them. An automated consumer never\n"
        "executes a request: it parks it here and in `requests/` in the queue, and only a session\n"
        "closes it — with a receipt (`dev_update` / `change_type: modified` / `in_reply_to` = the\n"
        "request id) or as declined/expired. An unclaimed request is never failed for going\n"
        "unclaimed, and its pending count is reported on every consumer run.\n"
        "\n"
        "> [!warning] Written by `ingest.py` — do not hand-edit the tables\n"
        "> The consumer rewrites this note from `state/*.request.json` in the mailbox. Add notes\n"
        "> below the tables instead: they survive, the tables do not.\n"
        "\n"
        f"## Pending ({len(rc['pending_list'])})\n"
        "\n"
        "| Age | Request | Target | Acceptance | Source | Expires | id |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        + ("\n".join(rows) if rows else "| — | _nothing waiting_ | | | | | |") + "\n"
        "\n"
        "## Recently closed\n"
        "\n"
        "| Closed | How | Request | id |\n"
        "| --- | --- | --- | --- |\n"
        + ("\n".join(closed) if closed else "| — | — | _none yet_ | |") + "\n"
        "\n"
        "## Scope\n"
        "\n"
        "This note lists requests **addressed to this side** (`hermes-mac`). Requests this side *sent*\n"
        "are closed by the other host and are deliberately **not** mirrored here — mirroring them is what\n"
        "once made the other side's parked mail read as this side's pending work.\n"
        "\n"
        f"Right now: **{rc['pending']} pending here**, {rc['closed']} closed here; "
        f"{rc['theirs_pending']} pending and {rc['theirs_closed']} closed on the other side (not this side's work).\n"
        "\n"
        "---\n"
        "\n"
        "Queue mechanics: `Hermes/04 Resources/Hermes Mac Ingest Skill`. Close a request with\n"
        "`ingest.py --request-done <id> --summary \"...\"`; list them with `ingest.py --requests`.\n"
    )
    p = vault / REQUEST_NOTE_REL
    existing = p.read_text() if p.exists() else ""
    if existing.strip() == body.strip():
        return None
    if dry_run:
        out(f"[dry-run] would update {REQUEST_NOTE_REL} ({len(rc['pending_list'])} pending)")
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def coedit_request(vault: Path, message_id: str, author: str, st: dict, phase: str, extra: str = "") -> None:
    """Attribution for an ask: parked, done, expired or declined all hit the log."""
    p = vault / COEDIT_REL
    if not p.exists():
        warn(f"co-edit log missing: {p}")
        return
    line = (f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} — **{CONSUMER_ID}** (mailbox "
            f"`{message_id}` from {author or 'unknown'}) → `{REQUEST_NOTE_REL.as_posix()}` — "
            f"**request {phase}**")
    if phase == "parked":
        line += " (NOT executed by the loop — waiting for a reasoning session)"
    if st.get("request"):
        line += f": {str(st['request'])[:160]}"
    if st.get("acceptance"):
        line += f" | acceptance: {str(st['acceptance'])[:120]}"
    if st.get("expires_at"):
        line += f" | expires: {st['expires_at']}"
    if extra:
        line += f" | {extra}"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def best_effort_message(path: Path) -> dict:
    """Read a message without validating it (for routing/reaper decisions)."""
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    raw.setdefault("payload", {})
    raw["message_id"] = str(raw.get("message_id") or path.stem)
    raw["task_type"] = canonical_task_type(raw.get("task_type"))
    return raw


def reaper(root: Path, dry_run: bool, notices: list) -> int:
    """Return dead claims to new/ (or requests/, or failed/ when attempts are spent)."""
    L = layout(root)
    if not L["proc"].is_dir():
        return 0
    now = datetime.now().timestamp()
    reaped = 0
    for path in list_msgs(L["proc"]):
        age_min = (now - path.stat().st_mtime) / 60.0
        if age_min < LEASE_MINUTES:
            continue
        # A request is mail waiting for a session, not a broken message: it goes
        # back to requests/ and never accumulates an attempt toward failed/.
        msg = best_effort_message(path)
        if is_request(msg):
            if dry_run:
                out(f"[dry-run] would re-park stale request {path.name} into requests/")
                reaped += 1
                continue
            dst, _st = park(root, path, msg)
            notices.append(f"request re-parked {path.name}")
            out(f"re-parked request {path.name} after {age_min:.0f}m idle -> requests/ "
                f"(never failed for going unclaimed)")
            reaped += 1
            continue
        n = read_attempts(root, path.stem) + 1
        if dry_run:
            out(f"[dry-run] would reap {path.name} (claimed {age_min:.0f}m ago, attempt {n})")
            reaped += 1
            continue
        write_attempts(root, path.stem, n)
        if n >= MAX_ATTEMPTS:
            dst = fail(root, path, f"dead claim reaped after {age_min:.0f}m (attempt {n}/{MAX_ATTEMPTS})")
            notices.append(f"reaped->failed {path.name}")
        else:
            dst = requeue(root, path)
            notices.append(f"reaped->new {path.name}")
        out(f"reaped {path.name} after {age_min:.0f}m idle -> {dst.parent.name} (attempt {n}/{MAX_ATTEMPTS})")
        reaped += 1
    return reaped


def drain_queue(root: Path, vault: Path, args, notices: list) -> dict:
    """Claim, apply or park every message in new/ that is addressed to us.

    Returns counters for the run: applied, duplicates, failed, requeued, parked,
    claim_lost, claim_errors, coerced, new_for_others. `requeued`, the claim
    counters and `parked` exist because a run that rejected or parked everything
    used to look identical to an empty queue from the state file - which is what
    made a one-character envelope mismatch look like "the consumer skipped our
    messages" and let a real request be filed as a log line and closed.
    """
    if not vault.is_dir():
        raise SystemExit(
            f"FATAL: vault root {vault} is not a directory — set VAULT_ROOT, put it in "
            f"{INSTANCE_ENV}, or pass --vault.\n"
            "       A missing vault must not read as an empty queue."
        )
    L = layout(root)
    c = dict(applied=0, duplicates=0, failed=0, requeued=0, claim_lost=0, claim_errors=0,
             coerced=0, parked=0, new_for_others=0)
    for path in list_msgs(L["new"]):
        if path.name.startswith("."):
            continue  # .DS_Store / AppleDouble litter
        # Routing, as agreed with the producer: claim iff consumer is me or 'any'.
        # Mail addressed to the other side waits in new/ - unclaimed, uncounted as
        # an error, and never reaped, because it is not ours to touch.
        peek = best_effort_message(path)
        if peek and not claimable(peek):
            c["new_for_others"] += 1
            continue
        if args.dry_run:
            out(f"[dry-run] would claim {path.name}")
        else:
            claimed, note = claim(root, path)
            if claimed is None:
                if note and note.startswith("error"):
                    c["claim_errors"] += 1
                    notices.append(f"claim failed for {path.name}: {note}")
                    out(f"could not claim {path.name}: {note}")
                else:
                    c["claim_lost"] += 1
                continue
            path = claimed
        try:
            message = read_envelope(path)
        except ValueError as e:
            n = read_attempts(root, path.stem) + 1
            mid, author = probe_identity(path)
            if args.dry_run:
                out(f"[dry-run] invalid {path.name}: {e} (attempt {n})")
                continue
            if path.parent.name == CONSUMER_ID:  # only claimed files can be moved
                write_attempts(root, path.stem, n)
                if n >= MAX_ATTEMPTS:
                    fail(root, path, f"invalid envelope: {e}")
                    write_status(root, mid, n, str(e), "rejected", author)
                    notices.append(f"failed {path.name}: {e}")
                    c["failed"] += 1
                    out(f"failed {path.name} after {n} attempts: {e}")
                else:
                    requeue(root, path)
                    write_status(root, mid, n, str(e), "rejected", author)
                    c["requeued"] += 1
                    out(f"requeued {path.name} (attempt {n}/{MAX_ATTEMPTS}): {e}")
            continue

        mid = message["message_id"]
        if message.get("_task_type_coerced"):
            c["coerced"] += 1
            notices.append(f"coerced task_type for {path.name}")
            out(f"note: {path.name} declared task_type {message.get('_task_type_declared')!r}; "
                f"accepted as {message['task_type']!r}")
        if is_request(message):
            # NEVER execute a request. Park it where both hosts and the operator can see it
            # and let a reasoning session close it. Unclaimed is not broken, so no
            # attempt counter is touched and the reaper will never fail it.
            pl = message.get("payload") or {}
            if args.dry_run:
                out(f"[dry-run] would park request {path.name}: {str(pl.get('request') or '')[:100]}")
                c["parked"] += 1
                continue
            dst, st = park(root, path, message)
            c["parked"] += 1
            how = ("task_request" if message["task_type"] == "task_request"
                   else f"kind=task under {message['task_type']!r}")
            notices.append(f"request parked: {mid}")
            out(f"PARKED {path.name} -> requests/ ({how}) — not executed, waiting for a session: "
                f"{str(st['request'])[:140]}")
            coedit_request(vault, mid, message.get("author", ""), st, "parked")
            continue
        if done_path(root, mid).exists():
            c["duplicates"] += 1
            if not args.dry_run:
                archive(root, path)
            out(f"duplicate {path.name} (already applied) -> archived")
            continue

        try:
            changed = apply_message(message, vault, args.dry_run)
        except (ValueError, OSError) as e:
            n = read_attempts(root, mid) + 1
            if args.dry_run:
                out(f"[dry-run] would fail {path.name}: {e}")
                continue
            write_attempts(root, mid, n)
            if n >= MAX_ATTEMPTS:
                fail(root, path, f"apply failed: {e}")
                write_status(root, mid, n, str(e), "apply_failed", message.get("author", ""))
                notices.append(f"failed {path.name}: {e}")
                c["failed"] += 1
                out(f"failed {path.name} after {n} attempts: {e}")
            else:
                requeue(root, path)
                write_status(root, mid, n, str(e), "apply_failed", message.get("author", ""))
                c["requeued"] += 1
                out(f"requeued {path.name} (attempt {n}/{MAX_ATTEMPTS}): {e}")
            continue

        if args.dry_run:
            c["applied"] += 1
            continue
        done_path(root, mid).write_text(
            f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S%z')} {message['task_type']} "
            f"from {message['author']}\n"
        )
        clear_status(root, mid)
        archive(root, path)
        c["applied"] += 1
        note = changed[0].name if changed else "-"
        out(f"applied {path.name}: {message['task_type']} from {message['author']} -> {note}")
    return c


def requeue_failed(root: Path, dry_run: bool) -> int:
    """Operator move: send quarantined messages back to new/ for another try.

    The legitimate use is exactly this incident - a consumer-side parsing bug
    rejected good messages, and after fixing the consumer the quarantined mail
    deserves a clean run. Attempt counters are reset, the .reason sidecar and any
    pending verdict are cleared, so the retry is not already half-spent.
    """
    n = 0
    for path in list_msgs(root / "failed"):
        n += 1
        if dry_run:
            out(f"[dry-run] would requeue failed/{path.name}")
            continue
        mid, _author = probe_identity(path)
        dst = root / "new" / path.name
        if dst.exists():
            dst = root / "new" / f"{path.stem}.{uuid.uuid4().hex[:6]}{path.suffix}"
        os.rename(path, dst)
        for side in (Path(f"{path}.reason"), attempts_path(root, mid), status_path(root, mid)):
            if side.exists():
                try:
                    side.unlink()
                except OSError:
                    pass
        out(f"requeued failed/{path.name} -> new/ (attempts reset)")
    return n


def prune(root: Path, dry_run: bool, notices: list) -> list:
    """Housekeeping: crashed writers in tmp/, old processed buckets, old done markers."""
    removed = []
    L = layout(root)
    now = datetime.now().timestamp()

    if L["tmp"].is_dir():
        for p in L["tmp"].glob("*"):
            if (now - p.stat().st_mtime) / 60.0 > TMP_MAX_AGE_MIN:
                removed.append(f"tmp/{p.name}")
                if not dry_run:
                    p.unlink()
                notices.append(f"pruned stale tmp/{p.name}")

    cutoff = datetime.now().strftime("%Y-%m")
    keep = []
    for i in range(PROCESSED_KEEP_MONTHS):
        y, m = int(cutoff[:4]), int(cutoff[5:7]) - i
        while m <= 0:
            y, m = y - 1, m + 12
        keep.append(f"{y:04d}-{m:02d}")
    if L["processed"].is_dir():
        for bucket in list_dirs(L["processed"]):
            if bucket.name not in keep:
                removed.append(f"processed/{bucket.name}/")
                if not dry_run:
                    for f in bucket.iterdir():
                        f.unlink()
                    bucket.rmdir()

    if L["state"].is_dir():
        for p in list_msgs(L["state"], "*.done"):
            if (now - p.stat().st_mtime) / 86400.0 > DONE_KEEP_DAYS:
                removed.append(f"state/{p.name}")
                if not dry_run:
                    p.unlink()
    return removed


def status(root: Path) -> int:
    L = layout(root)
    if not root.is_dir():
        out(f"mailbox {root}: NOT MOUNTED")
        return 0
    if not L["new"].is_dir():
        out(f"mailbox {root}: mounted, queue not provisioned (no new/)")
        return 0
    counts = {k: len(list_msgs(L[k])) if L[k].is_dir() else 0
              for k in ("new", "proc", "proc_other", "failed")}
    buckets = [p.name for p in list_dirs(L["processed"])]
    new_all = list_msgs(L["new"])
    mine = [p for p in new_all if claimable(best_effort_message(p) or {"consumer": "any"})]
    others = [p for p in new_all if p not in mine]
    reqs = request_counts(root)
    out(f"mailbox {root}")
    out(f"  new={len(new_all)} (claimable here: {len(mine)}; addressed to another consumer: {len(others)})")
    out(f"  processing/hermes-mac={counts['proc']} processing/hermes-docker={counts['proc_other']} "
        f"failed={counts['failed']}")
    out(f"  requests/ parked: pending={reqs['pending']} closed={reqs['closed']}"
        + (f" oldest_pending={reqs['oldest_h']:.1f}h" if reqs["pending"] else ""))
    for r in reqs["pending_list"]:
        out(f"    UNSERVED {r['message_id']} ({age_hours(r.get('parked_at', '')):.1f} h, "
            f"from {r.get('author')}) — {str(r.get('request'))[:110]}")
    out(f"  processed buckets: {', '.join(buckets) or '(none)'}")
    out(f"  state markers: {len(list_msgs(L['state'], '*.done'))} done, "
        f"{len(list_msgs(L['state'], '*.attempt'))} attempt")
    out(f"  lease={LEASE_MINUTES}m max_attempts={MAX_ATTEMPTS} consumer={CONSUMER_ID}")
    st = load_state(root)
    out(f"  last run: {st.get('last_run', 'never')} last_result: {st.get('last_result', '-')}")
    verdicts = list_msgs(L["state"], "*.status")
    if verdicts:
        # Rejections are left where the producer can read them: this is the
        # one-stop answer to "why was my message not applied?" from either side.
        out(f"  pending verdicts: {len(verdicts)}")
        for v in verdicts[-5:]:
            try:
                d = json.loads(v.read_text())
                out(f"    {v.name}: {d.get('phase')} attempts={d.get('attempts')} "
                    f"author={d.get('author') or '-'} reason={d.get('last_reason')}")
            except (json.JSONDecodeError, OSError):
                out(f"    {v.name}: (unreadable)")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    global LEASE_MINUTES, MAX_ATTEMPTS

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=Path, default=WATCH_DEFAULT)
    ap.add_argument("--vault", type=Path, default=VAULT_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="parse/plan but never write, move, or delete")
    ap.add_argument("--provision", action="store_true", help="create the queue layout, then exit")
    ap.add_argument("--lease-minutes", type=int, default=LEASE_MINUTES)
    ap.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    ap.add_argument("--reap-only", action="store_true",
                    help="run the dead-claim reaper and exit (no drain)")
    ap.add_argument("--requeue-failed", action="store_true",
                    help="move quarantined failed/ messages back to new/ and exit")
    ap.add_argument("--requests", action="store_true",
                    help="list parked requests (pending first) and exit")
    ap.add_argument("--request-done", metavar="MESSAGE_ID",
                    help="close a parked request as done and publish the receipt, then exit")
    ap.add_argument("--request-close", metavar="MESSAGE_ID",
                    help="close a parked request without executing it (declined)")
    ap.add_argument("--summary", default="", help="close summary / receipt text")
    ap.add_argument("--reason", default="", help="reason for --request-close")
    ap.add_argument("--consumer", default=None,
                    help="envelope consumer for --enqueue (default: this consumer)")
    ap.add_argument("--in-reply-to", default=None,
                    help="set in_reply_to on a published message (receipts)")
    ap.add_argument("--prune", action="store_true", help="housekeeping pass (tmp/processed/state)")
    ap.add_argument("--status", action="store_true", help="print queue status and exit")
    ap.add_argument("--enqueue", type=Path, help="publish a payload as a message, then exit")
    ap.add_argument("--task-type", default="dev_update", choices=TASK_TYPES)
    ap.add_argument("--author", default=CONSUMER_ID)
    args = ap.parse_args(argv)

    LEASE_MINUTES, MAX_ATTEMPTS = args.lease_minutes, args.max_attempts
    root = args.watch

    if args.status:
        return status(root)

    if root.is_dir() and not layout(root)["new"].is_dir():
        made = provision(root, apply=True)
        if made:
            out(f"provisioned mailbox: {', '.join(Path(m).name for m in made)}")
            return 0

    if args.provision:
        made = provision(root, apply=not args.dry_run)
        out(f"mailbox {root}: " + (f"created {', '.join(Path(m).name for m in made)}" if made
                                   else "layout already complete"))
        return 0

    if args.enqueue:
        payload = json.loads(args.enqueue.read_text()) if args.enqueue.exists() else json.loads(sys.stdin.read())
        if canonical_task_type(args.task_type) == "task_request":
            validate_request_payload(payload)      # fail here, not three minutes later
        env = make_envelope(payload, args.author, args.task_type,
                            kind="task" if canonical_task_type(args.task_type) != "dev_update" else "notification",
                            consumer=args.consumer)
        if args.in_reply_to:
            env["in_reply_to"] = args.in_reply_to
        if args.dry_run:
            out(f"[dry-run] would publish {env['message_id']}")
            return 0
        dst = publish(root, env)
        out(f"published {dst.name} -> {dst.parent} (task_type={env['task_type']} kind={env['kind']} "
            f"consumer={env['consumer']})")
        return 0

    if args.requests:
        rc = request_counts(root)
        other = (f"; {rc['theirs_pending']} pending and {rc['theirs_closed']} closed on the other side"
                 f" (addressed to the other consumer — not this side's work)")
        out(f"parked requests: {rc['pending']} pending, {rc['closed']} closed"
            + (other if (rc["theirs_pending"] or rc["theirs_closed"]) else ""))
        for r in rc["pending_list"]:
            out(f"\n  PENDING  {r['message_id']}  ({age_hours(r.get('parked_at','')):.1f} h, "
                f"from {r.get('author')}{'' if r.get('typed') else ', UNTYPED kind=task'})")
            out(f"    request:    {r.get('request')}")
            if r.get("target"):
                out(f"    target:     {r.get('target')}")
            if r.get("acceptance"):
                out(f"    acceptance: {r.get('acceptance')}")
            if r.get("requires"):
                out(f"    requires:   {', '.join(r['requires'])}")
            if r.get("source"):
                out(f"    source:     {r.get('source')}")
            if r.get("expires_at"):
                out(f"    expires_at: {r.get('expires_at')}")
            out(f"    reply_expected: {r.get('reply_expected')}")
        for r in sorted(rc["closed_list"], key=lambda d: str(d.get("closed_at") or ""), reverse=True)[:10]:
            out(f"  {str(r.get('status')).upper():8s} {r['message_id']}  closed {str(r.get('closed_at'))[:16]}"
                f"  {str(r.get('request'))[:80]}")
        return 0

    if args.request_done or args.request_close:
        mid = args.request_done or args.request_close
        how = "done" if args.request_done else "declined"
        st_req = next((r for r in load_requests(root) if r["message_id"] == mid), None)
        if st_req is None:
            return error(f"no parked request with id {mid!r} (see --requests)")
        if args.dry_run:
            out(f"[dry-run] would close {mid} as {how}"
                + (" and publish a receipt" if how == "done" and st_req.get("reply_expected") else ""))
            return 0
        res = close_request(root, mid, how,
                            summary=args.summary or f"Closed as {how} by a Mac-side session.",
                            details=[x for x in (args.reason,) if x])
        out(f"closed request {mid} as {how}"
            + (f"; message archived to {Path(res['archived']).parent.name}/" if res["archived"] else "")
            + (f"; receipt published: {Path(res['receipt']).name} (in_reply_to={mid})" if res["receipt"] else ""))
        coedit_request(args.vault, mid, st_req.get("author", ""), st_req, how,
                       extra=(f"receipt {Path(res['receipt']).stem} published" if res["receipt"] else args.reason))
        note = write_request_note(root, args.vault, False)
        if note:
            out(f"updated {REQUEST_NOTE_REL.as_posix()}")
        return 0

    if not root.is_dir():
        # Mount not up. Silent: this is the known-idle case, not an error.
        return 0

    L = layout(root)
    notices = []
    st = load_state(root)

    if not L["new"].is_dir():
        # Queue not provisioned yet (producer half pending). Speak once, not every minute.
        if st.get("provisioned") is not False:
            out(f"mailbox {root} mounted but queue not provisioned (no new/); waiting for producer side")
            st["provisioned"] = False
            if not args.dry_run:
                save_state(root, st)
        return 0
    if st.get("provisioned") is not True:
        notices.append("queue provisioned and reachable")
        st["provisioned"] = True
        out(f"mailbox {root} queue provisioned and reachable")

    if args.requeue_failed:
        n = requeue_failed(root, args.dry_run)
        out(f"requeue-failed: {n} message(s) returned to new/"
            + (" (dry run)" if args.dry_run else ""))
        return 0

    reaped = reaper(root, args.dry_run, notices)
    if args.reap_only:
        out(f"reaper: {reaped} dead claim(s) handled")
        return 0
    counts = drain_queue(root, args.vault, args, notices)
    # The root-level dev-update-*.json path is RETIRED (2026-09-22): the producer has not written
    # there since 2026-08-29 and confirmed the code path is gone. A file appearing there now is a
    # stale producer, not mail - report it, never consume it. Silently ignoring it would hide exactly
    # the kind of drift this queue exists to surface.
    strays = list_msgs(root, "dev-update-*.json")
    if strays:
        notices.append(f"legacy root files present: {len(strays)}")
        out(f"WARNING: {len(strays)} legacy root-level dev-update-*.json file(s) present - that path is "
            f"retired and nothing will consume them; ask the producer what wrote them")
        for sp in strays[:5]:
            out(f"  - {sp.name} ({sp.stat().st_size} B, mtime {sp.stat().st_mtime:.0f})")
    pruned = prune(root, args.dry_run, notices) if args.prune else []

    # Sweep before writing our own state, so the count reflects real litter left
    # by other writers (and by earlier passes), never by this one.
    stubs = sweep_stubs(root, args.dry_run)

    expired = expire_requests(root, args.dry_run, notices)
    rc = request_counts(root)
    note = write_request_note(root, args.vault, args.dry_run)

    if not args.dry_run:
        st["last_run"] = stamp_now()
        st["last_result"] = (
            f"applied={counts['applied']} dup={counts['duplicates']} failed={counts['failed']} "
            f"requeued={counts['requeued']} parked={counts['parked']} pending={rc['pending']} "
            f"claim_lost={counts['claim_lost']} claim_errors={counts['claim_errors']} "
            f"reaped={reaped} others={counts['new_for_others']} legacy_strays={len(strays)}"
        )
        st["notified"] = notices[-1] if notices else st.get("notified")
        save_state(root, st)
        drop_stub(state_file(root))   # our own write's stub, removed silently

    # An unactioned request is never invisible: the count is in every last_result,
    # listed by --status, mirrored into the vault note, and spoken here on change
    # and then at most hourly - which is the whole point, because the miss that
    # motivated this type was an ask that got applied as a log line and closed.
    pending_ids = [r["message_id"] for r in rc["pending_list"]]
    prev = (st.get("request_notice") or {}) if isinstance(st.get("request_notice"), dict) else {}
    if pending_ids and not args.dry_run and (
            pending_ids != prev.get("ids")
            or age_hours(str(prev.get("at", ""))) >= REQUEST_NOTICE_MINUTES / 60.0):
        out(f"REQUESTS PENDING: {len(pending_ids)} unactioned, oldest {rc['oldest_h']:.1f} h — "
            f"{', '.join(pending_ids)} (see {REQUEST_NOTE_REL.as_posix()} or `--requests`)")
        st["request_notice"] = {"at": stamp_now(), "ids": pending_ids}
        save_state(root, st)
        drop_stub(state_file(root))

    # claim_lost is contention, not a problem: recorded in state, never shouted.
    interesting = (counts["applied"] or counts["duplicates"] or counts["failed"] or counts["requeued"]
                   or counts["claim_errors"] or counts["coerced"] or counts["parked"]
                   or expired or note or reaped or strays or pruned or stubs)
    if interesting:
        summary = (
            f"done: applied={counts['applied']} duplicate={counts['duplicates']} "
            f"failed={counts['failed']} requeued={counts['requeued']} parked={counts['parked']} "
            f"pending={rc['pending']} claim_errors={counts['claim_errors']} reaped={reaped} "
            f"legacy_strays={len(strays)}"
            + (f" expired={expired}" if expired else "")
            + (f" coerced={counts['coerced']}" if counts["coerced"] else "")
            + (f" for_others={counts['new_for_others']}" if counts["new_for_others"] else "")
            + (f" pruned={len(pruned)}" if pruned else "")
            + (f" stubs={stubs}" if stubs else "")
        )
        out(summary)
        return 1 if counts["failed"] and not counts["applied"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
