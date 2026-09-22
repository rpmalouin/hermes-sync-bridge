# Invariants

Properties that must hold for this system to be safe. Each one is here with the reason it exists,
because the reason is what stops the next person from simplifying it away.

## Messaging

1. **A published message body is immutable.** `state/<id>.attempt` is the authoritative counter and
   `state/<id>.status` the authoritative verdict. Adding `status` or `attempt_count` to the envelope
   is refused by `shared/protocol.py` — the claim-by-rename protocol is only lock-free because
   nothing rewrites a body.
2. **Publish by `rename()`; claim by `rename()`.** Those two renames are the entire locking
   mechanism. Replacing either with a copy, a read-then-write, or a "check if it exists" is a
   regression, not a simplification.
3. **An ask is never applied by a loop.** `kind: task` is a promise: it reaches a reasoning session
   or it sits visibly in `requests/`. An unclaimed ask is not a failure; an invisible one is.
4. **Nothing is deleted.** Applied mail is archived; rejected mail is quarantined with a reason;
   board cards are archived to the vault. Deletion is a human decision, made deliberately, by the operator.

## Writing files that someone else also writes

5. **Back up, write, read back, verify.** Every script that mutates a shared file writes a `.bak`,
   re-reads what it wrote, and rolls back on a mismatch. A successful `write()` is not evidence.
6. **Assert a count that must not change.** Moving a card must not change the number of cards;
   rolling the archive must move exactly as many cards as it removes. These arithmetic invariants
   are the tripwires for "removed from the source, failed to insert at the destination" — the bug
   that silently deleted a card while reporting success.
7. **Anchor on structure, never on whitespace.** Parse headings and cards with regex; never match
   `"## Done\n\n"`. Three writers reflow this file, so a literal anchor is a delayed deletion.
8. **Fail loudly.** A verifier that crashes and prints nothing is worse than no verifier — silence
   reads as success. Checks exit non-zero, and `deploy.sh` refuses when `/Volumes/Data` is absent.

## Paths and process

9. **No cron entry point lives on `/Volumes/Data`.** That volume can unmount; a loop whose script is
   behind it would stop consuming silently. Repo = source, live paths = installed copies.
10. **The contract lives where both hosts can read it** — `SCHEMA.md` in the queue, not in this
    repo. A copy that nothing checks is a copy that drifts; where a copy must exist (protocol.py),
    a test compares it.
11. **One pen per file.** The board, the scripts and the docs each have an owner. Two writers on one
    artifact is how the same work gets carded twice, or a card gets closed twice.
12. **Corrections are recorded, not rewritten.** A wrong timestamp in a log gets an annotation
    naming the error; a wrong Co-edit line gets a new line. The history is the product.
