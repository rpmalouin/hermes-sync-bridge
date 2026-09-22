#!/usr/bin/env python3
"""kanban-archive-roll.py — move the board's archived cards into the vault's archive.

Why: the Kanban plugin keeps its archive *inside* the board file, and the plugin never renders it
(its own settings text says archived cards "can be viewed in markdown mode"; nothing maps over
`data.archive`). So archiving a card made it leave the board and land somewhere invisible. The
vault's convention is one `05 Archive/` per domain, and the board is a Hermes artifact — so the
record belongs in `Hermes/05 Archive/`.

Division of labour (the operator's policy, 2026-09-22):
  * the plugin's archive = staging (whatever he archived with the board's own action)
  * `Hermes/05 Archive/Hermes Kanban Archive <year>.md` = the record
  * one note per year: cards roll into the current year's note until the year ends, then the next
    year's note starts itself. **The year comes from the card's own archive stamp, not the run
    date** — a card archived on 31 December and rolled on 2 January belongs in the December year's
    note, and routing on the run date would misfile it.
  * the operator deletes these notes whenever he likes. A missing note is never an error: the next roll
    recreates it, so a deletion is a deliberate prune rather than a breakage.

The plugin writes no archive section at all when it is empty, so a fully-rolled board returns to the
plugin's canonical shape — that is why the `***` + `## Archive` block is removed, not emptied.

Invariants (a violation means nothing is written):
  * cards leaving the board == cards arriving in the notes (plus any skipped as duplicates)
  * no card text is altered beyond stripping the `— YYYY-MM-DD` archive stamp
  * every touched file is backed up (.bak) and re-read after writing, with rollback on mismatch

Usage:
  kanban-archive-roll.py [--dry-run] [--vault DIR] [--year YYYY]
"""
from __future__ import annotations

import argparse
import datetime
import re
import shutil
import sys
from pathlib import Path

DEFAULT_VAULT = Path("/Volumes/Data/vault")
HEAD_RX = re.compile(r"^##\s+(.+?)\s*$")
CARD_RX = re.compile(r"^[ \t]*- \[[ x]\][ \t]*(.*\S)?[ \t]*$")
# the plugin appends its stamp to the end of the card's CONTENT (updateItemContent rewrites the raw
# block), so it is looked for on the last line first and on the title only as a fallback
STAMP_RX = re.compile(r"[ \t]*—[ \t]*(\d{4})-(\d{2})-(\d{2})[ \t]*$")
SETTINGS_MARKER = "%% kanban:settings"


# ---------------------------------------------------------------- board parsing
def split_board(text: str):
    sections, head, buf = [], None, []
    for ln in text.split("\n"):
        m = HEAD_RX.match(ln)
        if m:
            sections.append((head, buf))
            head, buf = m.group(1), []
        else:
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


def card_blocks(body):
    res, i = [], 0
    while i < len(body):
        if CARD_RX.match(body[i]):
            j = i + 1
            while j < len(body) and body[j].strip() and not CARD_RX.match(body[j]):
                j += 1
            res.append((i, j, body[i:j]))
            i = j
        else:
            i += 1
    return res


def title_of(block) -> str:
    m = CARD_RX.match(block[0])
    return (m.group(1) or "").strip() if m else ""


def count_cards(text: str) -> int:
    return len([ln for ln in text.split("\n") if CARD_RX.match(ln)])


def note_header(year: str) -> str:
    return (
        f"---\ntags:\n  - archive\n  - kanban\ndate: {year}-01-01\n---\n\n"
        f"# 🗄️ Hermes Kanban Archive {year}\n\n"
        f"Cards archived off [[Hermes/Hermes Kanban|the Activity Kanban]], rolled out of the board's own\n"
        f"archive section by `~/.hermes/scripts/kanban-archive-roll.py`. The board keeps in-flight work and a\n"
        f"recent `Done`; this note is the record. Card fields are preserved verbatim.\n\n"
        f"**Policy:** one note per year — cards land here as they are archived, and each January the next\n"
        f"year's note starts itself. The year comes from the card's archive stamp, so a December card rolled\n"
        f"in January still files under its own year. **Deleting this note is fine** — nothing depends on it,\n"
        f"and a later roll recreates it.\n\n"
        f"← [[Hermes/Hermes|Home]] · [[Hermes/05 Archive/_Archive|Archive index]]\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vault", default=str(DEFAULT_VAULT), help="vault root (for sandbox tests)")
    ap.add_argument("--year", default=None, help="year for undated cards (default: this year)")
    a = ap.parse_args()

    vault = Path(a.vault)
    board = vault / "Hermes/Hermes Kanban.md"
    archive_dir = vault / "Hermes/05 Archive"
    this_year = a.year or datetime.date.today().strftime("%Y")

    board_text = board.read_text()
    # the plugin writes `%% kanban:settings` *after* the archive, so it sits inside the archive
    # section's body; split it off and reattach verbatim, or the roll drops the board's settings.
    marker = board_text.find(SETTINGS_MARKER)
    board_part, tail = (board_text[:marker], board_text[marker:]) if marker != -1 else (board_text, "")
    sections = split_board(board_part)

    idx = next((i for i, (h, _b) in enumerate(sections) if h == "Archive"), None)
    if idx is None:
        print("board has no archive section — nothing to roll")
        return 0
    body = sections[idx][1]
    cards = card_blocks(body)
    if not cards:
        print("board archive is empty — nothing to roll")
        return 0

    covered = set()
    for cs, ce, _blk in cards:
        covered.update(range(cs, ce))
    leftover = [ln for i, ln in enumerate(body) if ln.strip() and i not in covered and ln.strip() != "***"]
    if leftover:
        print(f"!! refusing: archive section holds content I do not recognise: {leftover[:2]}")
        return 2

    items = []
    for _s, _e, blk in cards:
        blk = list(blk)
        year, day = None, None
        for i in (len(blk) - 1, 0):
            m = STAMP_RX.search(blk[i])
            if m:
                year, day = m.group(1), f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                blk[i] = STAMP_RX.sub("", blk[i])
                break
        items.append({"title": title_of(blk), "year": year or this_year,
                      "date": day or "undated", "block": blk})

    print(f"{len(items)} archived card(s) to roll out of {board.name}:")
    for it in items:
        print(f"   [{it['date']}] {it['title']}")

    by_year: dict[str, dict[str, list]] = {}
    for it in items:
        by_year.setdefault(it["year"], {}).setdefault(it["date"], []).append(it)

    # plan each year's note; skip titles already present (the operator may prune or pre-seed)
    plans, fresh_total, skipped_total = {}, 0, 0
    for year, groups in sorted(by_year.items()):
        path = archive_dir / f"Hermes Kanban Archive {year}.md"
        existing = path.read_text() if path.exists() else ""
        already = {title_of([ln]) for ln in existing.split("\n") if CARD_RX.match(ln)}
        note = existing.rstrip("\n") + "\n" if existing else note_header(year)
        added = 0
        for date in sorted(groups, reverse=True):
            rows = [it for it in groups[date] if it["title"] not in already]
            skipped_total += len(groups[date]) - len(rows)
            if not rows:
                continue
            note += f"\n## Archived {date}\n"
            for it in rows:
                note += "\n" + "\n".join(it["block"]) + "\n"
            added += len(rows)
        if added:
            plans[year] = {"path": path, "existing": existing, "text": note, "added": added}
            fresh_total += added
    if not plans:
        print("nothing new to write — every card is already in the year notes")
        return 0
    for year in sorted(plans):
        p = plans[year]
        print(f"   -> {p['path'].relative_to(vault)}: +{p['added']} card(s)")

    if a.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    # board: drop the archive section. The `***` the plugin wrote before it lives in the PREVIOUS
    # section's body, so strip it from there or the board keeps an orphaned thematic break.
    prev_head, prev_body = sections[idx - 1] if idx else (None, [])
    while prev_body and not prev_body[-1].strip():
        prev_body.pop()
    if prev_body and prev_body[-1].strip() == "***":
        prev_body.pop()
        while prev_body and not prev_body[-1].strip():
            prev_body.pop()
    prev_body += ["", ""]
    sections[idx - 1] = (prev_head, prev_body)
    del sections[idx]
    new_board = rebuild(sections) + tail

    # invariants before writing anything
    b_before, b_after = count_cards(board_text), count_cards(new_board)
    if b_before - b_after != fresh_total:
        print(f"!! refusing: board {b_before} -> {b_after} but {fresh_total} card(s) planned")
        return 2
    if SETTINGS_MARKER not in new_board:
        print("!! refusing: the rebuilt board lost its kanban:settings block")
        return 2
    for year, p in plans.items():
        if count_cards(p["text"]) - count_cards(p["existing"]) != p["added"]:
            print(f"!! refusing: {p['path'].name} would gain "
                  f"{count_cards(p['text']) - count_cards(p['existing'])} cards, not +{p['added']}")
            return 2

    backups = {board: str(board) + ".bak"}
    shutil.copy2(board, backups[board])
    for year, p in plans.items():
        if p["path"].exists():
            backups[p["path"]] = str(p["path"]) + ".bak"
            shutil.copy2(p["path"], backups[p["path"]])
    board.write_text(new_board)
    for year, p in plans.items():
        p["path"].parent.mkdir(parents=True, exist_ok=True)
        p["path"].write_text(p["text"])
    if board.read_text() != new_board or any(p["path"].read_text() != p["text"] for p in plans.values()):
        for path, bak in backups.items():
            shutil.copy2(bak, path)
        print("!! verification failed after write — rolled back")
        return 2

    summary = ", ".join(f"{p['path'].name} +{p['added']}" for _y, p in sorted(plans.items()))
    print(f"\nrolled {fresh_total} card(s) | board {b_before} -> {b_after} | {summary}")
    if skipped_total:
        print(f"skipped {skipped_total} already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
