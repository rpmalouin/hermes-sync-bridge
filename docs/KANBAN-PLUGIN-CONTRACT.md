# The Obsidian Kanban contract (external, and easy to break)

`Hermes/Hermes Kanban.md` is a markdown file that **a third-party plugin owns**. Everything here
was read out of the plugin bundle, not guessed, and it is the part of this system most likely to
break without anyone touching our code — a plugin update can change any of it.

Describe it as: *we are careful guests in someone else's file format.*

## The shape the plugin reads and writes

| element | rule |
|---|---|
| columns | `## <name>` headings. Cards are list items under them |
| card | `- [ ] <title>` (or `- [x]`), then **tab-indented** fields: `**Task:**`, `**Node:**`, `**Context:**`, `**Status:**`, `**Notes:**` |
| completion | the **checkbox**. `Status:` is *ours*, for humans; the plugin never reads it |
| archive | the plugin writes `***`, a blank line, then `## Archive` — and its parser only accepts an archive where a heading named `Archive` is **preceded by a thematic break** |
| settings | a `%% kanban:settings` block holding JSON, written **after** the archive |
| frontmatter | `kanban-plugin: board`, `tags`, `card_template` |

## Four consequences that have already cost us time

1. **Archiving a card removes it from the board and hides it.** The plugin stores archived cards in
   its own state and never renders them — its own settings text says archived cards "can be viewed
   in markdown mode". There is no `archive.map(` anywhere in the bundle. So "archived" means
   *invisible*: `mac/kanban-archive-roll.py` moves them into `Hermes/05 Archive/Hermes Kanban
   Archive <year>.md`, which is where they are actually readable.
2. **A done card must be a checked card.** `archiveCompletedCards()` filters on `c.data.checked`,
   so 38 cards sitting in `Done` with `Status: done` but an empty box were unarchivable — enabling
   archiving would have archived *nothing*, silently. Box and `Status:` must agree; `kanban-card.py
   check` enforces it.
3. **The archive stamp goes at the end of the card's content, not in the title.** The plugin
   rewrites the raw block, so `— YYYY-MM-DD` lands on the last line. The archive roll reads it
   there, and that stamp is what decides which *year's* note a card files under.
4. **Three writers, one file, unstable whitespace.** The Mac, the VM's Hermes and the plugin all
   rewrite this file. A card inserted by one of them reflows the blank line under a heading, so a
   move anchored on `"## Done\n\n"` silently deletes a card when the anchor stops matching. Use
   `mac/kanban-card.py` (regex parsing, placement check, card count invariant) instead of
   hand-rolled edits.

## Settings we set, and the keys the plugin's validator accepts

Validated against the plugin's own `new Set([...])` — a key outside it is silently ignored:

```json
{"kanban-plugin":"board","archive-with-date":true,"archive-date-format":"YYYY-MM-DD",
 "archive-date-separator":"—","append-archive-date":true,"show-archive-all":true,
 "show-checkboxes":true,"show-search":true,"date-format":"YYYY-MM-DD","date-display-format":"YYYY-MM-DD"}
```

`archive-with-date` + `append-archive-date` produce `Title — 2026-09-22`. There is **no**
`limit-archived-cards` key (the "Maximum number of archived cards" setting is `max-archive-size`,
default `-1` = unlimited). Setting it to a number makes the plugin *delete* old archived cards once
the limit is reached — the one setting here that destroys data.

## Board conventions we maintain on top of the plugin

- `Done` means the box is ticked and `Status: done`; nothing else counts.
- Joint work reads `Node: Mac + dockerVM` rather than a coin flip.
- The plugin's archive is **staging**; `Hermes/05 Archive/` is the **record** (one note per year,
  a card's year taken from its own archive stamp, and deleting a year's note is harmless — the next
  roll recreates it).
- A card's `Context` is a real wikilink: it must resolve, or the vault audit counts it as a broken
  link.

**Written against**: the `obsidian-kanban` version installed 2026-08-29
(`.obsidian/plugins/obsidian-kanban/main.js`, 990,767 B). If the plugin is updated, re-read the
bundle before trusting any of the above — the four consequences in this document came from exactly
that exercise.
