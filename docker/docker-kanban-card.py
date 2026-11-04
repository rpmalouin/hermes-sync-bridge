#!/usr/bin/env python3
"""docker-kanban-card.py — card our own work in the shared Activity Kanban, in one command.

Why this exists: the discipline "open the card before the first action" failed on 2026-09-22 not
because nobody agreed with it but because it depended on a session remembering it mid-task. This
turns it into a command, and the command refuses to guess:

  * the plugin's exact card shape (tab-indented **Task:** / **Node:** / **Context:** / **Status:** /
    **Notes:**) is rendered, never hand-typed;
  * every edit carries `expected_content_hash` from the read it was built on, so a concurrent writer
    (the Mac agent, the Obsidian plugin, Hatchdoor) is rejected rather than clobbered;
  * the card count is asserted before and after (a move changes a column, never the count), and the
    write is verified by re-reading the board and checking column + box + Status agree;
  * exactly one column region changes per command — everything else is copied byte for byte.

Commands:
    docker-kanban-card.py open "<title>" [--note "<vault path>"] [--notes "<line>"]... [--dry-run]
    docker-kanban-card.py open --backlog "<title>" [...]     # note it in Backlog, not started
    docker-kanban-card.py done "<title substring>" [--summary "<completion line>"] [--dry-run]
    docker-kanban-card.py backlog "<title substring>" [--summary "<why it is parked>"] [--dry-run]
    docker-kanban-card.py list
    docker-kanban-card.py check

Environment overrides (defaults are correct on this host):
    KANBAN_VAULT_ID, KANBAN_SLUG, KANBAN_HELPER (the MCP-over-HTTP shim)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# The Hatchdoor vault that holds the board. Instance-specific, so it is configuration, and it
# is read from the same environment the MCP shim beside this script uses.
VAULT_ID = os.environ.get("HATCHDOOR_VAULT_ID", "")
SLUG = "hermes-kanban"
HERE = Path(__file__).resolve().parent
HELPER_NAME = "hatchdoor-mcp-call.py"


def resolve_helper() -> Path:
    """Locate the Hatchdoor MCP shim: beside this script, then the deployed scripts dir.

    This was the absolute "/root/.hermes/scripts/hatchdoor-mcp-call.py", so the script could not run
    from a bare clone and its failure mode was a subprocess FileNotFoundError rather than a sentence.
    Mirrors mac/acceptance_tests.py:companion(). The DEPLOYED copy still resolves on the first
    candidate, because HERE *is* /root/.hermes/scripts there, so the cron path cannot regress.

    No env override is documented for this script, so none was invented; a HERMES_SCRIPTS_DIR
    override belongs here only once it is written down somewhere a caller would read.
    """
    candidates = [HERE / HELPER_NAME,
                  Path.home() / ".hermes" / "scripts" / HELPER_NAME,
                  Path("/root/.hermes/scripts") / HELPER_NAME]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"cannot find {HELPER_NAME}: looked in "
                     + ", ".join(str(c) for c in candidates))


HELPER = str(resolve_helper())


def vault_id() -> str:
    """The configured vault id, or a sentence naming the variable that is missing."""
    if not VAULT_ID:
        raise SystemExit("HATCHDOOR_VAULT_ID is not set — export the id of the Hatchdoor vault\n"
                         "that holds the Kanban board (see docker/README.md).")
    return VAULT_ID
ACTIVE = "Hermes-dockerVM (Active)"
DONE = "Done"
# Backlog is the shared "not yet" column: unchecked box, Status: backlog. It is
# where an item goes when it is real but deliberately not in flight here — a
# blocked dependency, or a decision the operator wants to make later. check() already
# exempts it from the active/done box rule, which is why nothing but this script
# needs to know it exists.
BACKLOG = "Backlog"
NODE_ME = "dockerVM"
LOG_DIR = "Hermes/Hermes Logs/dockerVM"

CARD_RE = re.compile(r"^- \[( |x|X)\] (.*)$")
FIELD_RE = re.compile(r"^\s+- \*\*(Task|Node|Context|Status):\*\*\s*(.*)$")
ANY_FIELD_RE = re.compile(r"^\s+- \*\*([A-Za-z ]+):\*\*\s*(.*)$")


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%MZ")


# --------------------------------------------------------------------------- MCP transport
def mcp(tool: str, arguments: dict) -> dict:
    out = subprocess.run([sys.executable, HELPER, tool, json.dumps(arguments)],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0 or not out.stdout.strip():
        die(f"{tool} failed: rc={out.returncode} {out.stderr.strip()[:300]}")
    reply = json.loads(out.stdout.strip().splitlines()[-1])
    if not reply.get("ok"):
        die(f"{tool} refused: {json.dumps(reply)[:300]}")
    return reply["result"]


def note_exists(note: str):
    """True if the vault resolves this note path, False if it does not, None if the check itself failed.

    A card's Context link is a promise that the note exists. `open` derives that link from the card
    title, so a card opened before its note is written used to leave a dangling wikilink that the
    weekly vault audit counts as critical (four such links by 2026-10-17). Ask the vault instead of
    assuming: a `slug` of null means no such note.
    """
    out = subprocess.run([sys.executable, HELPER, "resolve_wikilink",
                          json.dumps({"vault_id": vault_id(), "target": note})],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        reply = json.loads(out.stdout.strip().splitlines()[-1])
    except ValueError:
        return None
    if not reply.get("ok"):
        return None
    return bool((reply.get("result") or {}).get("slug"))


def read_board() -> tuple[str, str]:
    note = mcp("get_note", {"slug": SLUG, "vault_id": vault_id()})
    if "note" not in note:
        # Hatchdoor answers with a structured refusal instead of a note when the vault is
        # unreachable (e.g. its SMB share is down: vault_read_unavailable / "Host is down"). Say
        # that plainly — a KeyError traceback reads like a broken tool rather than a missing vault,
        # and this is exactly what a card-first session hits at Step 0.
        message = note.get("message") or note.get("code") or json.dumps(note)[:200]
        die(f"cannot read the board: {message}\n"
            f"       The vault is unreachable, so no card can be opened right now. That is a blocked "
            f"Step 0, not a skipped one: record the attempt and card the work when the vault is back.")
    return note["note"]["content"], note["note"]["content_hash"]


def write_board(old: str, new: str, summary: str) -> str:
    result = mcp("edit_note", {
        "slug": SLUG, "vault_id": vault_id(), "old_string": old, "new_string": new,
        "expected_content_hash": read_board()[1], "commit_summary": summary})
    return result.get("content_hash", "?")


# --------------------------------------------------------------------------- parsing
def split_regions(content: str) -> list[dict]:
    """The file as [prefix, region...]; each region is header + its exact text span."""
    marks = [m.start() for m in re.finditer(r"(?m)^## ", content)]
    parts = []
    if not marks:
        return [{"header": None, "start": 0, "end": len(content)}]
    if marks[0] > 0:
        parts.append({"header": None, "start": 0, "end": marks[0]})
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(content)
        header = content[start:content.index("\n", start)].rstrip("\n")
        parts.append({"header": header, "start": start, "end": end})
    return parts


def region_by_column(regions: list[dict], column: str) -> dict:
    """Find a column, tolerating the node-suffixed split of a shared column.

    The Mac side splits a shared column per node (`## Backlog` became
    `## Backlog Hermes-Mac` / `## Backlog Hermes-dockerVM`), so an exact match on
    the bare name silently starts failing. Prefer an exact header, then the
    variant owned by THIS node, then a lone suffixed variant; anything else is
    ambiguous and must not be guessed.
    """
    headers = [r["header"] for r in regions if r["header"]]
    for region in regions:
        if region["header"] == f"## {column}":
            return region
    prefixed = [r for r in regions if (r["header"] or "").startswith(f"## {column} ")]
    for region in prefixed:
        if NODE_ME.lower() in region["header"].lower():
            return region
    if len(prefixed) == 1:
        return prefixed[0]
    die(f"no unambiguous column '## {column}' in the board "
        f"(candidates: {headers}) — refusing to invent one")


def parse_region(content: str, region: dict) -> tuple[list[str], list[list[str]]]:
    """Return (cards as line-blocks, trailer lines) for one region."""
    lines = content[region["start"]:region["end"]].split("\n")[1:]  # drop the header line
    blocks: list[list[str]] = []
    trailer: list[str] = []
    cur: list[str] | None = None
    for line in lines:
        if CARD_RE.match(line):
            if cur:
                blocks.append(cur)
            cur = [line]
        elif cur is not None and (line.startswith(("\t", "  ")) or line.strip() == ""):
            cur.append(line)
        elif cur is not None:
            blocks.append(cur)
            cur = None
            trailer.append(line)
        else:
            trailer.append(line)
    if cur:
        blocks.append(cur)
    blocks = [trim_trailing_blanks(b) for b in blocks]
    return blocks, trim_leading_blanks(trailer)


def trim_trailing_blanks(block: list[str]) -> list[str]:
    while block and block[-1].strip() == "":
        block.pop()
    return block


def trim_leading_blanks(lines: list[str]) -> list[str]:
    while lines and lines[0].strip() == "":
        lines.pop(0)
    return lines


def render_region(header: str, blocks: list[list[str]], trailer: list[str]) -> str:
    out = header + "\n"
    out += "\n" + "\n\n".join("\n".join(b) for b in blocks) + "\n\n\n" if blocks else "\n\n\n"
    if trailer:
        out += "\n".join(trailer) + "\n"
    return out


def render_card(title: str, status: str, checked: bool, node: str, context: str,
                notes: list[str]) -> list[str]:
    block = [f"- [{'x' if checked else ' '}] {title}",
             f"\t- **Task:** {title}",
             f"\t- **Node:** {node}",
             f"\t- **Context:** {context}",
             f"\t- **Status:** {status}"]
    for i, line in enumerate(notes):
        block.append(f"\t- **Notes:** {line}" if i == 0 else f"\t  {line}")
    return block


def card_title(block: list[str]) -> str:
    m = CARD_RE.match(block[0])
    return m.group(2).strip() if m else ""


def card_field(block: list[str], field: str) -> str:
    for line in block:
        m = FIELD_RE.match(line)
        if m and m.group(1) == field:
            return m.group(2).strip()
    return ""


def all_cards(content: str) -> list[tuple[str, list[str]]]:
    regions = split_regions(content)
    found = []
    for region in regions:
        if region["header"] is None:
            continue
        blocks, _ = parse_region(content, region)
        column = region["header"][3:].strip()
        for block in blocks:
            found.append((column, block))
    return found


# --------------------------------------------------------------------------- commands
def open_card(args) -> int:
    content, _ = read_board()
    before = all_cards(content)
    if any(args.title.lower() in card_title(b).lower() for _, b in before):
        die(f"a card matching {args.title!r} already exists — nothing opened, nothing changed")
    note = args.note or f"{LOG_DIR}/{today()} {args.title}"
    exists = note_exists(note)
    dangling = exists is False
    if dangling:
        # Never publish a link the vault cannot resolve: the audit counts each one as critical.
        context = f"{note} (no note yet — plain text on purpose)"
    else:
        context = f"[[{note}]]"
    if args.done:
        notes = [f"Carded {today()} **{now_stamp()}**, after the work landed — say why in --notes."]
    elif args.backlog:
        notes = [f"Noted in Backlog {today()} **{now_stamp()}** — real but not started; "
                 f"say why it is parked in --notes."]
    else:
        notes = [f"Opened {today()} **{now_stamp()} as Step 0**, before the first action on this task."]
    notes += args.notes or []
    status = "done" if args.done else ("backlog" if args.backlog else "active")
    checked = bool(args.done)
    block = render_card(args.title, status, checked, args.node, context, notes)

    regions = split_regions(content)
    column = DONE if args.done else (BACKLOG if args.backlog else ACTIVE)
    target = region_by_column(regions, column)
    blocks, trailer = parse_region(content, target)
    new_region = render_region(target["header"], [block] + blocks, trailer)

    old_span = content[target["start"]:target["end"]]
    new_span = new_region
    new_content = content[:target["start"]] + new_span + content[target["end"]:]

    if args.dry_run:
        print(f"[dry-run] card to add to '{column}':\n" + "\n".join(block))
        print(f"[dry-run] region rewritten ({len(old_span)} -> {len(new_span)} chars), "
              f"cards before={len(before)}, after={len(all_cards(new_content))}")
        return 0

    if len(all_cards(new_content)) != len(before) + 1:
        die("refused: the rendered board does not have exactly one more card than before")
    why = "retroactive, done" if args.done else ("Backlog note" if args.backlog else "Step 0, in flight")
    write_board(old_span, new_span,
                f"Kanban: add card '{args.title}' to {column} ({why})")

    after_content, _ = read_board()
    after = all_cards(after_content)
    if len(after) != len(before) + 1:
        die(f"verify failed: card count {len(before)} -> {len(after)}", 2)
    mine = [b for col, b in after if card_title(b).lower() == args.title.lower()]
    if len(mine) != 1:
        die(f"verify failed: expected exactly one card titled {args.title!r}, found {len(mine)}", 2)
    want_status = status
    want_box = "x" if checked else " "
    if (card_field(mine[0], "Status").lower() != want_status
            or CARD_RE.match(mine[0][0]).group(1).lower() != want_box):
        die(f"verify failed: the new card is not {want_status} with box [{want_box}]", 2)
    print(f"added to {column}: {args.title}")
    print(f"  cards {len(before)} -> {len(after)}, Context {context}")
    if dangling:
        print(f"  WARNING: there is no note at {note!r}. The Context is PLAIN TEXT on purpose — a card")
        print(f"           must not contribute a broken wikilink to the vault audit. Write the note, then")
        print(f"           repoint the card's Context at it.")
    elif exists is None:
        print("  note: could not verify the Context note exists (vault check failed); linked anyway.")
    return 0


def move_card(args, target_column: str, status: str, checked: bool, summary_prefix: str) -> int:
    """Move one card into target_column, rewriting its box and Status to match.

    `done` and `backlog` are the same operation with different destinations. The
    tripwires are what make either safe on a board three writers share: the card
    count is unchanged, exactly one card answers the title substring, and the
    destination column, box and Status all agree on the re-read.
    """
    content, _ = read_board()
    before = all_cards(content)
    matches = [(col, b) for col, b in before if args.title.lower() in card_title(b).lower()]
    if not matches:
        die(f"no card matching {args.title!r} on the board")
    if len(matches) > 1:
        die(f"{len(matches)} cards match {args.title!r} — be more specific")
    column, block = matches[0]
    if column == target_column:
        die(f"that card is already in {target_column}")
    title = card_title(block)
    fields = {name: card_field(block, name) for name in ("Node", "Context")}
    notes = [line.strip() for line in block if line.startswith(("\t  ", "\t- **Notes:**"))]
    notes = [re.sub(r"^- \*\*Notes:\*\* ", "", n).strip() for n in notes]
    if args.summary:
        notes.append(f"{summary_prefix} {args.summary}")
    new_block = render_card(title, status, checked, fields["Node"] or NODE_ME,
                            fields["Context"], notes)

    regions = split_regions(content)
    src = region_by_column(regions, column)
    dst = region_by_column(regions, target_column)
    src_blocks, src_trailer = parse_region(content, src)
    src_blocks = [b for b in src_blocks if card_title(b).lower() != title.lower()]
    dst_blocks, dst_trailer = parse_region(content, dst)

    new_src = render_region(src["header"], src_blocks, src_trailer)
    new_dst = render_region(dst["header"], [new_block] + dst_blocks, dst_trailer)

    start = min(src["start"], dst["start"])
    end = max(src["end"], dst["end"])
    old_span = content[start:end]
    # Rebuild the span by walking the ORIGINAL segments in order: replacing by offset against an
    # already-modified string silently duplicates content when the first replacement changes length
    # (the bug this function had on its first dry run — the card count check caught it).
    rebuilt_parts = []
    cursor = start
    for region, replacement in sorted(((src, new_src), (dst, new_dst)),
                                      key=lambda pair: pair[0]["start"]):
        rebuilt_parts.append(content[cursor:region["start"]])
        rebuilt_parts.append(replacement)
        cursor = region["end"]
    rebuilt_parts.append(content[cursor:end])
    rebuilt = "".join(rebuilt_parts)
    new_content = content[:start] + rebuilt + content[end:]

    box = "x" if checked else " "
    if args.dry_run:
        print(f"[dry-run] card: {title}\n[dry-run] {column} -> {target_column}, "
              f"box -> [{box}], Status -> {status}")
        print(f"[dry-run] span {len(old_span)} -> {len(rebuilt)} chars; "
              f"cards before={len(before)} after={len(all_cards(new_content))}")
        print("[dry-run] old span head:\n" + old_span[:200])
        print("[dry-run] new span head:\n" + rebuilt[:260])
        return 0

    if len(all_cards(new_content)) != len(before):
        die("refused: the rendered board does not have the same card count as before")
    write_board(old_span, rebuilt, f"Kanban: move '{title}' to {target_column}")

    after_content, _ = read_board()
    after = all_cards(after_content)
    if len(after) != len(before):
        die(f"verify failed: card count {len(before)} -> {len(after)}", 2)
    moved = [(col, b) for col, b in after if card_title(b).lower() == title.lower()]
    if len(moved) != 1:
        die(f"verify failed: {len(moved)} cards titled {title!r} after the move", 2)
    col, blk = moved[0]
    got_status = card_field(blk, "Status").lower()
    got_box = CARD_RE.match(blk[0]).group(1).lower()
    if col != target_column or got_status != status or got_box != box:
        die(f"verify failed: column={col!r} Status={got_status!r} box={got_box!r}", 2)
    print(f"moved to {target_column}: {title}")
    print(f"  column={col} box=[{got_box}] Status={got_status} cards={len(after)}")
    return 0


def done_card(args) -> int:
    return move_card(args, DONE, "done", True, f"**Done {today()}.**")


def backlog_card(args) -> int:
    return move_card(args, BACKLOG, "backlog", False, f"**Parked in Backlog {today()}.**")


def list_cards(args) -> int:
    content, _ = read_board()
    cards = all_cards(content)
    current = None
    for column, block in cards:
        if column != current:
            print(f"\n[{column}]")
            current = column
        print(f"  {'[x]' if CARD_RE.match(block[0]).group(1).lower() == 'x' else '[ ]'} "
              f"{card_title(block)}")
        print(f"      Node={card_field(block, 'Node')!r} Status={card_field(block, 'Status')!r}")
    print(f"\n{len(cards)} card(s)")
    return 0


def check_cards(args) -> int:
    content, _ = read_board()
    cards = all_cards(content)
    problems = []
    for column, block in cards:
        title = card_title(block)
        box = CARD_RE.match(block[0]).group(1).lower()
        status = card_field(block, "Status").lower()
        if not card_field(block, "Node"):
            problems.append(f"no Node: {title}")
        if column == DONE and (box != "x" or status != "done"):
            problems.append(f"Done card not checked/done (box=[{box}] Status={status!r}): {title}")
        if column != DONE and not column.startswith(BACKLOG) and (box == "x" or status == "done"):
            problems.append(f"active card checked/done (box=[{box}] Status={status!r}): {title}")
    seen = {}
    for _, block in cards:
        seen[card_title(block).lower()] = seen.get(card_title(block).lower(), 0) + 1
    for title, count in seen.items():
        if count > 1:
            problems.append(f"duplicate card title x{count}: {title}")
    print(f"{len(cards)} card(s), {len(problems)} problem(s)")
    for p in problems:
        print("  " + p)
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Open and move dockerVM cards on the Hermes Kanban")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="open a card in the dockerVM column (Step 0)")
    p_open.add_argument("title")
    p_open.add_argument("--note", default=None, help="vault path of the log note for Context")
    p_open.add_argument("--notes", action="append", default=[], help="extra Notes line(s)")
    p_open.add_argument("--node", default=NODE_ME, choices=[NODE_ME, "Mac", "Mac + dockerVM"],
                        help="Node attribution: dockerVM (default), Mac, or the joint form")
    p_open.add_argument("--done", action="store_true",
                        help="retroactive card: write it straight into Done, box checked, "
                             "Status done (for work that finished before it could be carded)")
    p_open.add_argument("--backlog", action="store_true",
                        help="note the card in Backlog instead: box unchecked, Status backlog "
                             "(for a real item that is deliberately not in flight)")
    p_open.add_argument("--dry-run", action="store_true")
    p_open.set_defaults(func=open_card)

    p_done = sub.add_parser("done", help="move a card to Done, box checked, Status done")
    p_done.add_argument("title", help="a distinctive substring of the card title")
    p_done.add_argument("--summary", default=None, help="completion line appended to Notes")
    p_done.add_argument("--dry-run", action="store_true")
    p_done.set_defaults(func=done_card)

    p_backlog = sub.add_parser("backlog", help="move a card to Backlog, box unchecked, Status backlog")
    p_backlog.add_argument("title", help="a distinctive substring of the card title")
    p_backlog.add_argument("--summary", default=None,
                           help="the reason it is parked, appended to Notes")
    p_backlog.add_argument("--dry-run", action="store_true")
    p_backlog.set_defaults(func=backlog_card)

    p_list = sub.add_parser("list", help="list every card with its column, box and Status")
    p_list.set_defaults(func=list_cards)

    p_check = sub.add_parser("check", help="audit box/Status/Node agreement and duplicates")
    p_check.set_defaults(func=check_cards)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
