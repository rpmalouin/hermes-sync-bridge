#!/usr/bin/env python3
"""kanban-archive-roll.py — move finished cards off the board into the vault's archive.

Why: the Kanban plugin keeps its archive *inside* the board file, and the plugin never renders it
(its own settings text says archived cards "can be viewed in markdown mode"; nothing maps over
`data.archive`). So archiving a card by hand made it leave the board and land somewhere invisible.
The vault's convention is one `05 Archive/` per domain, and the board is a Hermes artifact — so the
record belongs in `Hermes/05 Archive/`.

Two sources, one destination:

  * `archive` — the plugin's in-file archive section: whatever was archived with the board's own
    action. Those cards carry the plugin's `— YYYY-MM-DD` stamp, which is what dates them.
  * `done` — **ticked cards in `## Done`** (`- [x]`), filed straight from the column. This is the
    path that needs no plugin action at all, so a card can go from finished to filed without anyone
    pressing Archive. A done-sourced card was never stamped by the plugin, so it takes the day this
    runs. An UNticked card in `Done` is left alone (the box is the plugin's definition of complete)
    and reported.
  * `both` (default) — drain both. The default is deliberately the maximal one: the failure this
    tool exists to end is a card left in a middle state, and a bare run should not create one.

Division of labour (the operator's policy, 2026-09-22):
  * the plugin's archive = staging (whatever he archived with the board's own action)
  * `Hermes/05 Archive/Hermes Kanban Archive <year>.md` = the record
  * one note per year: cards roll into the current year's note until the year ends, then the next
    year's note starts itself. **The year comes from the card's own archive stamp, not the run
    date** — a card archived on 31 December and rolled on 2 January belongs in the December year's
    note, and routing on the run date would misfile it. (A done-sourced card has no stamp, so it is
    dated by the run.)
  * the operator deletes these notes whenever he likes. A missing note is never an error: the next roll
    recreates it, so a deletion is a deliberate prune rather than a breakage.
  * **newest first** (the operator, 2026-09-23): sections are emitted newest-date-first with `## Archived
    undated` last. He reads the note top-down and expects the cards he has just filed to be at the
    top; appending them under older sections buries them. Cards arriving for a date whose section
    already exists are appended INTO that section rather than starting a second heading with the
    same name — two passes on one day used to produce two `## Archived <same date>` headings.
    Re-emission re-sorts the note, so a note built by an older version (which appended) becomes
    newest-first on the first run of this one; the script says so when it happens.

The plugin writes no archive section at all when it is empty, so a fully-rolled board returns to the
plugin's canonical shape — that is why the `***` + `## Archive` block is removed, not emptied. The
thematic break lives in the body of the section BEFORE the archive, so it is stripped from there —
and only when the archive section itself is going away, because the plugin's parser needs the `***`
and the heading to appear together. A `--source done` pass that leaves the archive in place must
therefore leave the break alone.

Invariants (a violation means nothing is written):
  * cards leaving the board == cards arriving in the notes (plus any skipped as duplicates)
  * no card text is altered beyond stripping the `— YYYY-MM-DD` archive stamp
  * a rewritten note may not lose a single non-blank line it had before
  * every touched file is backed up (.bak) and re-read after writing, with rollback on mismatch

Usage:
  kanban-archive-roll.py [--dry-run] [--source archive|done|both] [--vault DIR] [--year YYYY]
"""
from __future__ import annotations

import argparse
import datetime
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

DEFAULT_VAULT = Path("/Volumes/Data/vault")
HEAD_RX = re.compile(r"^##\s+(.+?)\s*$")
SECTION_RX = re.compile(r"^##\s+Archived\s+(.+?)\s*$")
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
        f"Cards filed off [[Hermes/Hermes Kanban|the Activity Kanban]] by\n"
        f"`~/.hermes/scripts/kanban-archive-roll.py` — from the board's own archive section\n"
        f"(`--source archive`) and from ticked cards in `Done` (`--source done`). The board holds\n"
        f"in-flight work and a recent `Done`; this note is the record. **Newest first** — each roll\n"
        f"inserts its section above the older ones. Card fields are preserved verbatim.\n\n"
        f"**Policy:** one note per year — cards land here as they are archived, and each January the next\n"
        f"year's note starts itself. The year comes from the card's archive stamp, so a December card rolled\n"
        f"in January still files under its own year. **Deleting this note is fine** — nothing depends on it,\n"
        f"and a later roll recreates it.\n\n"
        f"← [[Hermes/Hermes|Home]] · [[Hermes/05 Archive/_Archive|Archive index]]\n"
    )


def trim(lines):
    """Drop leading and trailing blank lines."""
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def parse_note(text: str):
    """(header, [(date, [body lines])]) for a year note. Refuses stray '## ' headings it cannot file."""
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if SECTION_RX.match(ln)), None)
    header = lines if first is None else lines[:first]
    order, bodies, cur = [], {}, None
    for ln in lines[first:] if first is not None else []:
        m = SECTION_RX.match(ln)
        if m:
            cur = m.group(1).strip()
            order.append(cur)
            bodies.setdefault(cur, [])
        elif ln.startswith("## "):
            return None                  # a heading that is not '## Archived <date>' — be loud
        elif cur is not None:
            bodies[cur].append(ln)
    return header, [(d, bodies[d]) for d in order]


def date_sort_key(date: str):
    """Newest first; 'undated' is unknown, so it sorts last rather than lexically first."""
    return (0 if date == "undated" else 1, date)


# ---------------------------------------------------------------- card collection
def items_from_archive(sections, this_year):
    """Drain the plugin's archive section. Returns (items, idx); items is None if it must refuse."""
    idx = next((i for i, (h, _b) in enumerate(sections) if h == "Archive"), None)
    if idx is None:
        return [], None
    body = sections[idx][1]
    cards = card_blocks(body)
    if not cards:
        return [], idx

    covered = set()
    for cs, ce, _blk in cards:
        covered.update(range(cs, ce))
    leftover = [ln for i, ln in enumerate(body) if ln.strip() and i not in covered and ln.strip() != "***"]
    if leftover:
        print(f"!! refusing: archive section holds content I do not recognise: {leftover[:2]}")
        return None, idx

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
                      "date": day or "undated", "block": blk, "source": "archive"})
    return items, idx


def items_from_done(sections, today):
    """Ticked cards in `Done`, removed from the column. Returns (items, idx, skipped_titles)."""
    idx = next((i for i, (h, _b) in enumerate(sections) if h == "Done"), None)
    if idx is None:
        return [], None, []
    body = sections[idx][1]
    moved, skipped, drop = [], [], set()
    for cs, ce, blk in card_blocks(body):
        if blk[0].lstrip().startswith("- [x]"):
            moved.append(blk)
            drop.update(range(cs, ce))
        else:
            skipped.append(title_of(blk))
    if not moved:
        return [], idx, skipped

    kept = trim([ln for i, ln in enumerate(body) if i not in drop])
    # the '***' that precedes the archive section lives at the end of THIS body: keep it exactly
    # once. Drop it and re-add (never append a second one).
    tail = [""]
    if kept and kept[-1].strip() == "***":
        kept = trim(kept[:-1])
        tail = ["", "***", ""]
    sections[idx] = ("Done", kept + tail)
    items = [{"title": title_of(blk), "year": today.strftime("%Y"),
              "date": today.strftime("%Y-%m-%d"), "block": list(blk), "source": "done"} for blk in moved]
    return items, idx, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", choices=("archive", "done", "both"), default="both",
                    help="where finished cards come from (default: both)")
    ap.add_argument("--vault", default=str(DEFAULT_VAULT), help="vault root (for sandbox tests)")
    ap.add_argument("--year", default=None, help="year for undated cards (default: this year)")
    a = ap.parse_args()

    vault = Path(a.vault)
    board = vault / "Hermes/Hermes Kanban.md"
    archive_dir = vault / "Hermes/05 Archive"
    today = datetime.date.today()
    this_year = a.year or today.strftime("%Y")

    board_text = board.read_text()
    # the plugin writes `%% kanban:settings` *after* the archive, so it sits inside the archive
    # section's body; split it off and reattach verbatim, or the roll drops the board's settings.
    marker = board_text.find(SETTINGS_MARKER)
    board_part, tail = (board_text[:marker], board_text[marker:]) if marker != -1 else (board_text, "")
    sections = split_board(board_part)

    items, archive_idx, skipped_done = [], None, []
    if a.source in ("archive", "both"):
        got, archive_idx = items_from_archive(sections, this_year)
        if got is None:
            return 2
        items += got
    if a.source in ("done", "both"):
        got, _done_idx, skipped_done = items_from_done(sections, today)
        items += got

    if not items:
        print("nothing to file — no staged archive cards, no ticked cards in Done")
        for t in skipped_done:
            print(f"   left in Done (box not ticked): {t}")
        return 0

    print(f"{len(items)} card(s) to file out of {board.name}:")
    for it in items:
        print(f"   [{it['date']}] ({it['source']}) {it['title']}")
    for t in skipped_done:
        print(f"   left in Done (box not ticked): {t}")

    by_year: dict[str, dict[str, list]] = {}
    for it in items:
        by_year.setdefault(it["year"], {}).setdefault(it["date"], []).append(it)

    # plan each year's note; skip titles already present (the operator may prune or pre-seed)
    plans, fresh_total, skipped_total = {}, 0, 0
    for year, groups in sorted(by_year.items()):
        path = archive_dir / f"Hermes Kanban Archive {year}.md"
        existing = path.read_text() if path.exists() else ""
        parsed = parse_note(existing) if existing else (note_header(year).split("\n"), [])
        if parsed is None:
            print(f"!! refusing: {path.name} carries a '## ' heading I cannot file")
            return 2
        header, secs = parsed
        already = {title_of([ln]) for ln in existing.split("\n") if CARD_RX.match(ln)}

        merged, added = {d: list(b) for d, b in secs}, 0
        for date in sorted(groups, reverse=True):
            rows = [it for it in groups[date] if it["title"] not in already]
            skipped_total += len(groups[date]) - len(rows)
            if not rows:
                continue
            incoming = []
            for it in rows:
                incoming += it["block"] + [""]
            if date in merged:                      # same date already has a section: extend it
                merged[date] = trim(merged[date]) + [""] + trim(incoming)
            else:
                merged[date] = trim(incoming)
            added += len(rows)
        if not added:
            continue

        order = sorted(merged, key=date_sort_key, reverse=True)
        was = [d for d, _b in secs]
        out = trim(header) + [""]
        for d in order:
            out += [f"## Archived {d}", ""] + trim(merged[d]) + [""]
        text = "\n".join(out).rstrip("\n") + "\n"

        if existing:
            lost = Counter(l for l in existing.split("\n") if l.strip()) - \
                Counter(l for l in text.split("\n") if l.strip())
            if lost:
                print(f"!! refusing: {path.name} would lose lines: {list(lost)[:2]}")
                return 2
        if was and order != was:
            print(f"   note {path.name}: re-sorted newest-first ({' > '.join(was)} -> {' > '.join(order)})")
        plans[year] = {"path": path, "existing": existing, "text": text, "added": added}
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

    # board: drop the archive section, and the `***` the plugin wrote before it, which lives in the
    # PREVIOUS section's body. Only when that section is actually going away — the plugin's parser
    # needs the break and the heading together.
    if archive_idx is not None:
        prev_head, prev_body = sections[archive_idx - 1] if archive_idx else (None, [])
        while prev_body and not prev_body[-1].strip():
            prev_body.pop()
        if prev_body and prev_body[-1].strip() == "***":
            prev_body.pop()
            while prev_body and not prev_body[-1].strip():
                prev_body.pop()
        prev_body += ["", ""]
        sections[archive_idx - 1] = (prev_head, prev_body)
        del sections[archive_idx]
    new_board = rebuild(sections) + tail

    # invariants before writing anything
    b_before, b_after = count_cards(board_text), count_cards(new_board)
    if b_before - b_after != fresh_total:
        print(f"!! refusing: board {b_before} -> {b_after} but {fresh_total} card(s) planned")
        return 2
    if SETTINGS_MARKER not in new_board:
        print("!! refusing: the rebuilt board lost its kanban:settings block")
        return 2
    if archive_idx is not None and any(h == "Archive" for h, _b in split_board(new_board)):
        print("!! refusing: the rebuilt board still carries an archive heading")
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
    print(f"\nfiled {fresh_total} card(s) | board {b_before} -> {b_after} | {summary}")
    if skipped_total:
        print(f"skipped {skipped_total} already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
