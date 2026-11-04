#!/usr/bin/env python3
"""kanban-card.py — safe card edits on the shared Activity Kanban.

Why this exists (2026-09-22): three writers share `Hermes/Hermes Kanban.md` (this Mac,
the DockerVM's Hermes, Hatchdoor's MCP) and its whitespace is not stable. A card inserted
by another writer reflowed the blank line under a column heading, so
`text.replace("## Done\\n\\n", ...)` stopped matching — and an edit that lifted a card out
of `(Active)` and then failed to insert it into `Done` **deleted the card while reporting
success**. That is exactly what happened.

So: line-oriented parsing, no whitespace-sensitive literals, and verification by
re-reading the rebuilt text before writing. Invariants enforced:

  * every card's `**Status:**` matches its column (`active` in `(Active)`, `done` in `Done`)
  * each card lives in exactly one column
  * `move` never changes the total card count — the tripwire for "removed from source,
    failed to insert at destination", which is the silent-loss failure above

Usage:
  kanban-card.py list
  kanban-card.py check
  kanban-card.py add  "<title>" --column "Hermes-Mac (Active)" [--node Mac] [--context "[[x]]"] [--notes "..."]
  kanban-card.py move <substring> --to "Done" [--status done] [--notes-append "..."]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# Instance paths: the repo ships a PLACEHOLDER vault on purpose, so the real one comes from the
# environment or from a per-machine file outside the repo. Resolution order: VAULT_ROOT env,
# then $SYNC_BRIDGE_ENV (default ~/.hermes/sync-bridge.env), then the placeholder default.
# The same helper is duplicated in ingest.py and kanban-archive-roll.py: these files are installed
# into two different directories and must stay standalone.
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


VAULT_ROOT = Path(os.environ.get("VAULT_ROOT") or instance_var("VAULT_ROOT") or "/Volumes/Data/vault")
BOARD = VAULT_ROOT / "Hermes/Hermes Kanban.md"
HEAD_RX = re.compile(r"^##\s+(.+?)\s*$")
CARD_RX = re.compile(r"^[ \t]*- \[[ x]\][ \t]*(.*\S)?[ \t]*$")
INDENT_RX = re.compile(r"^([ \t]+|\s*$)")
STATUS_RX = re.compile(r"(\*\*Status:\*\*\s*)(\S+)")


# ---------------------------------------------------------------- parsing
def split_board(text: str):
    """-> [(heading|None, [lines])] ; line-oriented, lossless."""
    sections, head, buf = [], None, []
    for ln in text.split("\n"):
        m = HEAD_RX.match(ln)
        if m:
            sections.append((head, buf))
            head, buf = m.group(1), []
        else:
            # a bare `***` is the thematic break the plugin writes *before* the Archive heading
            # (its parser needs heading "Archive" preceded by one) — it belongs to the body, not
            # to a section of its own, or `list` invents a column called `***`.
            buf.append(ln)
    sections.append((head, buf))
    return sections


def rebuild(sections) -> str:
    out = []
    for head, body in sections:
        if head is not None:
            out.append(f"## {head}")
        out.extend(body)
    return "\n".join(out)


def blocks(body):
    """-> [(start, end, [lines])] for each card block (card line + its indented lines)."""
    res, i = [], 0
    while i < len(body):
        if CARD_RX.match(body[i]):
            j = i + 1
            while j < len(body) and body[j].strip() and INDENT_RX.match(body[j]) and not CARD_RX.match(body[j]):
                j += 1
            res.append((i, j, body[i:j]))
            i = j
        else:
            i += 1
    return res


def card_title(lines) -> str:
    m = CARD_RX.match(lines[0])
    return (m.group(1) if m and m.group(1) else "").strip()


def count_cards(text: str) -> int:
    return len([ln for ln in text.split("\n") if CARD_RX.match(ln)])


def expect_status(column: str):
    if "(Active)" in column:
        return "active"
    if column == "Done":
        return "done"
    return None


# ---------------------------------------------------------------- write path
def write_verified(new_text: str, old_text: str, must_appear: str, *, same_card_count: bool):
    if same_card_count and count_cards(new_text) != count_cards(old_text):
        sys.exit(f"refusing to write: card count would change "
                 f"({count_cards(old_text)} -> {count_cards(new_text)})")
    n = count_cards(new_text) if not must_appear else sum(
        1 for ln in new_text.split("\n") if CARD_RX.match(ln) and must_appear in ln)
    if n < 1:
        sys.exit(f"refusing to write: {must_appear!r} not present in the rebuilt board")
    shutil.copy2(BOARD, str(BOARD) + ".bak")
    BOARD.write_text(new_text)
    if BOARD.read_text() != new_text:
        shutil.copy2(str(BOARD) + ".bak", BOARD)
        sys.exit("verification failed after write — rolled back from .bak")


def verify_placement(text: str, title: str, column: str) -> None:
    """Post-condition: the card sits in the named column exactly once."""
    hits = []
    for head, body in split_board(text):
        if head is None:
            continue
        for _s, _e, blk in blocks(body):
            if card_title(blk) == title:
                hits.append(head)
    if hits != [column]:
        sys.exit(f"verification failed: {title!r} found in {hits}, expected [{column!r}] — not written")


# ---------------------------------------------------------------- commands
def cmd_list(_a):
    for head, body in split_board(BOARD.read_text()):
        if head is None:
            continue
        cards = [card_title(b[2]) for b in blocks(body)]
        print(f"## {head}  ({len(cards)})")
        for c in cards:
            print(f"   - {c}")
    print(f"total: {count_cards(BOARD.read_text())}")


def cmd_check(_a):
    bad = 0
    for head, body in split_board(BOARD.read_text()):
        if head is None:
            continue
        want = expect_status(head)
        if want is None:
            continue
        for _s, _e, blk in blocks(body):
            m = STATUS_RX.search("\n".join(blk))
            got = m.group(2) if m else "(none)"
            if got != want:
                print(f"MISMATCH  [{head}] {card_title(blk)!r} → Status: {got}, expected {want}")
                bad += 1
            # the plugin decides "completed" from the CHECKBOX, not from Status, so the two must
            # agree or its Archive action silently skips the card (this is why boxes were flipped)
            if "\n".join(blk).count("**Notes:**") > 1:
                print(f"NOTE FIELD  [{head}] {card_title(blk)!r} has more than one **Notes:** field")
                bad += 1
            boxed = blk[0].lstrip().startswith("- [x]")
            if (want == "done") != boxed:
                print(f"BOX MISMATCH  [{head}] {card_title(blk)!r} → box={'checked' if boxed else 'unchecked'}, "
                      f"Status: {got} (plugin archives on the box)")
                bad += 1
    archived = [(head, blk) for head, body in split_board(BOARD.read_text()) if head == "Archive"
                for _s, _e, blk in blocks(body)]
    unchecked = [blk for _h, blk in archived if not blk[0].lstrip().startswith("- [x]")]
    print(f"{bad} mismatch(es) | archive: {len(archived)} card(s), {len(unchecked)} unchecked")
    if unchecked:
        for blk in unchecked:
            print(f"  ARCHIVE UNCHECKED  {card_title(blk)!r} — the plugin skips it on a re-parse")
        bad += len(unchecked)
    return 1 if bad else 0


def cmd_add(a):
    text = BOARD.read_text()
    sections = split_board(text)
    status = a.status or expect_status(a.column) or "active"
    box = "x" if status == "done" else " "
    card = [f"- [{box}] {a.title}",
            f"\t- **Task:** {a.title}",
            f"\t- **Node:** {a.node}"]
    if a.context:
        card.append(f"\t- **Context:** {a.context}")
    card += [f"\t- **Status:** {status}"]
    if a.notes:
        card.append(f"\t- **Notes:** {a.notes}")
    for i, (head, body) in enumerate(sections):
        if head == a.column:
            new_lines = card + [""] + body
            sections[i] = (head, new_lines)
            break
    else:
        sys.exit(f"column not found: {a.column!r}")
    new_text = rebuild(sections)
    verify_placement(new_text, a.title, a.column)
    write_verified(new_text, text, a.title, same_card_count=False)
    print(f"added to {a.column} (Status: {status}): {a.title}")


def cmd_move(a):
    text = BOARD.read_text()
    sections = split_board(text)
    src = dest = None
    for i, (head, body) in enumerate(sections):
        if head is None:
            continue
        for s, e, blk in blocks(body):
            if a.substr.lower() in card_title(blk).lower():
                src, dest = i, blk
                del body[s:e]                      # keep the surrounding blank line shape
                sections[i] = (head, body)
                break
        if dest:
            break
    if not dest:
        sys.exit(f"no card matching {a.substr!r}")
    title = card_title(dest)
    want = a.status or expect_status(a.to) or ""
    if want:
        joined = "\n".join(dest)
        if STATUS_RX.search(joined):
            dest = STATUS_RX.sub(lambda m: m.group(1) + want, joined, count=1).split("\n")
        else:
            dest = dest + [f"\t- **Status:** {want}"]
        # keep the checkbox in step: the plugin archives on the box, not on Status
        head0 = re.match(r"^(\s*- \[)[ x](\].*)$", dest[0])
        if head0:
            dest[0] = f"{head0.group(1)}{'x' if want == 'done' else ' '}{head0.group(2)}"
    if a.notes_append:
        for k, ln in enumerate(dest):
            if "**Notes:**" in ln:
                dest = dest[:k + 1] + [f"\t  {a.notes_append}"] + dest[k + 1:]
                break
        else:
            dest = dest + [f"\t- **Notes:** {a.notes_append}"]
    for i, (head, body) in enumerate(sections):
        if head == a.to:
            sections[i] = (head, [""] + dest + [""] + body)
            break
    else:
        sys.exit(f"column not found: {a.to!r}")
    new_text = rebuild(sections)
    verify_placement(new_text, title, a.to)
    write_verified(new_text, text, title, same_card_count=True)
    print(f"moved to {a.to} (Status: {want or 'unchanged'}): {title}")


def main():
    ap = argparse.ArgumentParser(description="Safe edits to the shared Activity Kanban.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("add")
    p.add_argument("title"); p.add_argument("--column", required=True)
    p.add_argument("--node", default="Mac"); p.add_argument("--context", default="")
    p.add_argument("--status", default=None); p.add_argument("--notes", default="")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("move")
    p.add_argument("substr"); p.add_argument("--to", required=True)
    p.add_argument("--status", default=None); p.add_argument("--notes-append", default="")
    p.set_defaults(fn=cmd_move)
    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
