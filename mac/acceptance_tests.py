#!/usr/bin/env python3
"""Acceptance tests for the Hermes-Mac side of the bridge: the mailbox consumer (ingest.py) and the
board tooling it shares with the vault (kanban-card.py / kanban-archive-roll.py).

Builds throwaway sandboxes under /tmp/mailbox-acceptance, one per test, each with
its own mailbox and its own copy of the real Kanban board, so nothing here can
touch the live queue or the vault. Prints raw evidence per test.

Run:  python3 acceptance_tests.py [--verbose]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INGEST = HERE / "ingest.py"
SANDBOX = Path("/tmp/mailbox-acceptance")
REAL_VAULT = Path(os.environ.get("VAULT_ROOT") or "/Volumes/Data/vault")
PY = sys.executable or "python3"

# Stand-in board for a host (or a clone) with no real vault. It carries only what the consumer
# needs — the two column headings it appends under, in board order — deliberately not a copy of
# the live board, which is per-machine data.
BOARD_FIXTURE = """---
kanban-plugin: board
---

## Hermes-Mac (Active)

## Hermes-dockerVM (Active)

## Done
"""


def companion(name: str) -> Path:
    """Locate a companion tool: beside this script (the repo), or where deploy installs it.

    The deployed layout keeps only the consumer (ingest.py + this file) next to the skill and
    puts the board tooling in ~/.hermes/scripts, so a bare HERE / name made the deployed run
    fail with rc=2 on a file that exists -- it just lives somewhere else.
    """
    for cand in (HERE / name, Path.home() / ".hermes" / "scripts" / name):
        if cand.exists():
            return cand
    raise SystemExit(f"cannot find {name}: looked in {HERE} and ~/.hermes/scripts")

RESULTS = []


def banner(t: str) -> None:
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def check(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def new_fixture(name: str):
    """Fresh mailbox + fresh vault for one test.

    The board is copied from the real vault when this host has one — that copy is what catches a
    change in the plugin's own shape — and synthesised otherwise. The fallback matters because the
    repo's default vault is a placeholder: a clone has no board to copy, and the suite must still
    run there instead of dying on a fixture the host was supposed to provide.
    """
    base = SANDBOX / name
    if base.exists():
        shutil.rmtree(base)
    mailbox = base / "mailbox"
    vault = base / "vault"
    (vault / "Hermes" / "Hermes Logs" / "dockerVM").mkdir(parents=True)
    board = REAL_VAULT / "Hermes/Hermes Kanban.md"
    if board.exists():
        shutil.copy2(board, vault / "Hermes/Hermes Kanban.md")
    else:
        (vault / "Hermes/Hermes Kanban.md").write_text(BOARD_FIXTURE)
    (vault / "Co-edit log.md").write_text("# Co-edit log\n\n")
    home = base / "home"
    (home / "state").mkdir(parents=True)
    return mailbox, vault, home


def env_for(mailbox: Path, vault: Path, home: Path, extra=None) -> dict:
    e = dict(os.environ)
    e.update({
        "MAILBOX_ROOT": str(mailbox),
        "VAULT_ROOT": str(vault),
        "HERMES_HOME": str(home),
        "MAILBOX_LEASE_MINUTES": "15",
        "MAILBOX_MAX_ATTEMPTS": "3",
    })
    if extra:
        e.update(extra)
    return e


def run_ingest(env, *args, expect_rc=(0, 1)):
    p = subprocess.run([PY, str(INGEST), *args], env=env, capture_output=True, text=True)
    print(f"  $ ingest.py {' '.join(args) or '(no args)'}  -> rc={p.returncode}")
    for line in (p.stdout.strip().splitlines() + [f"stderr: {l}" for l in p.stderr.strip().splitlines()]):
        print(f"      {line}")
    assert p.returncode in expect_rc, f"unexpected rc {p.returncode}"
    return p


def payload(name="worldmonitor-v2", ctype="new", summary="Added the worldmonitor stack documentation.", card=True):
    return {
        "dev_item": name,
        "change_type": ctype,
        # carding is opt-in: without it a dev_update is logged and never touches the board
        **({"card": True} if card else {}),
        "affected_container_or_stack": "worldmonitor",
        "summary": summary,
        "details": ["new: Homelab/02 Projects/Docker/worldmonitor/index.md", "graph rebuilt"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_payload_file(base: Path, data: dict) -> Path:
    p = base / "payload.json"
    p.write_text(json.dumps(data, indent=2))
    return p


# ---------------------------------------------------------------- tests
def test_round_trip():
    banner("TEST 1 — round trip: enqueue -> claimed -> processed (+ vault applied)")
    mailbox, vault, home = new_fixture("t1-roundtrip")
    env = env_for(mailbox, vault, home)
    base = mailbox.parent

    run_ingest(env, "--provision")
    print("\n  -- mailbox layout --")
    print("   ", sorted(p.name for p in mailbox.iterdir()))

    pf = write_payload_file(base, payload())
    run_ingest(env, "--enqueue", str(pf))
    probe = run_ingest(env, "--status")
    check("message sits in new/ before the consumer runs", "new=1" in probe.stdout)

    run_ingest(env)

    L = {k: mailbox / k for k in ("new", "processing", "processed", "failed", "state", "tmp")}
    check("new/ drained", list(L["new"].glob("*.json")) == [])
    check("processing/hermes-mac/ empty (claim released)", list((L["processing"] / "hermes-mac").glob("*.json")) == [])
    bucket = sorted(p for p in L["processed"].iterdir() if p.is_dir())
    archived = list(bucket[0].glob("*.json")) if bucket else []
    check("message archived under processed/YYYY-MM/", len(archived) == 1, str(bucket[0].name if bucket else "-"))
    done = list(L["state"].glob("*.done"))
    check("done marker written", len(done) == 1, done[0].name if done else "-")

    notes = list((vault / "Hermes/Hermes Logs/dockerVM").glob("Dev Updates *.md"))
    ok = len(notes) == 1 and "worldmonitor-v2 — new" in notes[0].read_text()
    check("vault daily note got the entry", ok, notes[0].name if notes else "no note")
    if notes:
        print("\n  -- note body --")
        for line in notes[0].read_text().strip().splitlines():
            print(f"      {line}")

    board = (vault / "Hermes/Hermes Kanban.md").read_text()
    active_idx = board.find("## Hermes-dockerVM (Active)")
    done_idx = board.find("## Done")
    card_idx = board.find("- [ ] worldmonitor-v2")
    check("kanban card landed in the Active column",
          active_idx != -1 and card_idx != -1 and active_idx < card_idx < done_idx,
          f"active@{active_idx} card@{card_idx} done@{done_idx}")
    check("kanban .bak written before mutation", (vault / "Hermes/Hermes Kanban.md.bak").exists())
    check("empty run is silent",
          run_ingest(env).stdout.strip() == "")
    coedits = (vault / "Co-edit log.md").read_text().strip().splitlines()
    check("co-edit log carries the attribution line", any("hermes-mac" in l and "dev_update" in l for l in coedits),
          coedits[-1][:110] if coedits else "-")
    return mailbox, vault, home, env


def test_poison():
    banner("TEST 2 — poison message: malformed JSON -> failed/ with reason after 3 attempts")
    mailbox, vault, home = new_fixture("t2-poison")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")
    bad = mailbox / "new" / "20260921T000000Z-deadbeef.json"
    bad.write_text("{ this is not json")

    for i in (1, 2, 3):
        print(f"\n  -- attempt {i} --")
        run_ingest(env)

    failed = list((mailbox / "failed").glob("*.json"))
    reasons = list((mailbox / "failed").glob("*.json.reason"))
    check("message quarantined in failed/", len(failed) == 1, failed[0].name if failed else "-")
    check("reason sidecar written", len(reasons) == 1,
          reasons[0].read_text().strip() if reasons else "-")
    check("attempt counter reached max (3)", (mailbox / "state" / "20260921T000000Z-deadbeef.attempt").read_text().strip() == "3")
    check("nothing left in new/", list((mailbox / "new").glob("*.json")) == [])


def test_dead_claim():
    banner("TEST 3 — dead claim: reaper returns it to new/, then fails it when attempts are spent")
    mailbox, vault, home = new_fixture("t3-reaper")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")

    # (a) fresh claim, backdated past the lease -> back to new/
    pf = write_payload_file(mailbox.parent, payload(name="reaper-probe"))
    run_ingest(env, "--enqueue", str(pf)).stdout
    msg = next((mailbox / "new").glob("*.json"))
    claimed = mailbox / "processing" / "hermes-mac" / msg.name
    os.rename(msg, claimed)
    old = time.time() - 20 * 60
    os.utime(claimed, (old, old))
    print(f"  (simulated a crashed consumer: {claimed.name} claimed, backdated 20m)")

    run_ingest(env, "--reap-only")
    back = list((mailbox / "new").glob("*.json"))
    check("reaped back into new/", len(back) == 1, back[0].name if back else "-")
    check("attempt counter incremented to 1",
          (mailbox / "state" / f"{claimed.stem}.attempt").read_text().strip() == "1")
    check("processing/ emptied by the reaper", list((mailbox / "processing" / "hermes-mac").glob("*.json")) == [])

    # (b) attempts spent -> failed with the dead-claim reason
    msg = back[0]
    claim2 = mailbox / "processing" / "hermes-mac" / msg.name
    os.rename(msg, claim2)
    (mailbox / "state" / f"{msg.stem}.attempt").write_text("2\n")
    os.utime(claim2, (old, old))
    run_ingest(env, "--reap-only")
    reasons = list((mailbox / "failed").glob("*.reason"))
    check("exhausted dead claim goes to failed/", len(reasons) == 1,
          reasons[0].read_text().strip() if reasons else "-")
    check("reason names the lease", reasons and "dead claim reaped" in reasons[0].read_text())

    # (c) the requeue path is not a dead end: a fresh claim comes back and applies
    pf2 = write_payload_file(mailbox.parent, payload(name="reaper-probe-2"))
    run_ingest(env, "--enqueue", str(pf2))
    msg = next((mailbox / "new").glob("*.json"))
    claim3 = mailbox / "processing" / "hermes-mac" / msg.name
    os.rename(msg, claim3)
    os.utime(claim3, (old, old))
    run_ingest(env)   # reaper requeues, then the same pass applies it
    check("a requeued message is applied in the same pass",
          not list((mailbox / "new").glob("*.json"))
          and len(list((mailbox / "processed").rglob("*.json"))) == 1)
    check("nothing stuck in processing/", list((mailbox / "processing" / "hermes-mac").glob("*.json")) == [])


def test_redelivery(mailbox, vault, env):
    banner("TEST 4 — redelivery: a message already applied is archived, never re-applied")
    note = next((vault / "Hermes/Hermes Logs/dockerVM").glob("Dev Updates *.md"))
    before = note.read_text().count("## worldmonitor-v2 — new")
    archived = sorted(p for p in (mailbox / "processed").iterdir() if p.is_dir())[0]
    src = next(archived.glob("*.json"))
    shutil.copy2(src, mailbox / "new" / src.name)
    print(f"  (redelivered {src.name} into new/)")

    run_ingest(env)
    after = note.read_text().count("## worldmonitor-v2 — new")
    check("vault note unchanged (no double apply)", before == after == 1, f"entries {before} -> {after}")
    check("redelivered copy archived, not re-applied", list((mailbox / "new").glob("*.json")) == [])
    check("counted as a duplicate", len(list((mailbox / "processed").iterdir())) >= 1)


def test_torn_write():
    banner("TEST 5 — torn write: no consumer run ever sees a partial file in new/")
    mailbox, vault, home = new_fixture("t5-torn")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")

    big = payload(name="torn-probe")
    big["details"] = ["x" * 200_000]  # ~200 KB payload: long enough to be caught mid-copy
    stop = threading.Event()
    published = []

    def writer():
        sys.path.insert(0, str(HERE))
        import importlib.util
        spec = importlib.util.spec_from_file_location("ingest_mod", INGEST)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for i in range(40):
            env_msg = mod.make_envelope(big, "hermes-docker", "dev_update")
            env_msg["message_id"] = f"torn-{i:03d}"
            mod.publish(mailbox, env_msg)
            published.append(env_msg["message_id"])
            time.sleep(0.01)
        stop.set()

    t = threading.Thread(target=writer)
    t.start()
    runs = 0
    bad_output = []
    # real failure signals only: a tombstone line, a requeue, a traceback, a warning.
    # NB the run summary legitimately contains "failed=0" - do not match on that.
    signals = re.compile(r"(Traceback|WARN:|requeued |failed \S+\.json)")
    while not stop.is_set() or runs < 3:
        p = subprocess.run([PY, str(INGEST)], env=env, capture_output=True, text=True)
        runs += 1
        if signals.search(p.stdout) or signals.search(p.stderr):
            bad_output.append((runs, p.stdout.strip(), p.stderr.strip()))
        if runs > 200:
            break
    t.join()

    # one final drain
    subprocess.run([PY, str(INGEST)], env=env, capture_output=True, text=True)
    left = list((mailbox / "new").glob("*.json"))
    processed = list((mailbox / "processed").rglob("*.json"))
    failed = list((mailbox / "failed").glob("*.json"))
    print(f"  published={len(published)} consumer_runs={runs} processed={len(processed)} "
          f"left_in_new={len(left)} failed={len(failed)}")
    check("no run ever reported a failure or warning", not bad_output,
          str(bad_output[:1]) if bad_output else "")
    check("every published message survived to processed/", len(processed) == len(published),
          f"{len(processed)}/{len(published)}")
    check("nothing quarantined", failed == [])
    check("tmp/ never leaked a partial into new/", len(left) == 0)


def test_legacy_retired():
    banner("TEST 6 — the legacy root path is RETIRED: a stray file is reported, never consumed")
    mailbox, vault, home = new_fixture("t6-legacy")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")
    stray = mailbox / "dev-update-2026-09-21T00-00-00Z.json"
    stray.write_text(json.dumps(payload(name="legacy-item", ctype="completed")))
    gen = mailbox / "generate.py"          # producer file: must stay untouched
    gen.write_text("#!/usr/bin/env python3\n")
    ru = mailbox / "recent-updates.json"   # producer index: must stay untouched
    ru.write_text("[]")

    p = run_ingest(env)
    check("a stray legacy file is left exactly where it is", stray.exists())
    check("it is not archived", not list((mailbox / "processed").rglob("dev-update-*.json")))
    check("no vault note was written from it",
          list((vault / "Hermes/Hermes Logs/dockerVM").glob("Dev Updates *.md")) == [])
    check("the run reports it as a stray", "legacy root-level dev-update-*.json file(s) present" in p.stdout,
          p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "")
    check("it is counted, not silently ignored",
          "legacy_strays=1" in json.loads((mailbox / "state" / "hermes-mac-ingest.state.json").read_text())["last_result"])
    check("producer files untouched", gen.exists() and ru.exists())


def test_stubs():
    banner("TEST 7 — macOS AppleDouble stubs (._*): never claimed, never parsed, swept")
    mailbox, vault, home = new_fixture("t7-stubs")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")

    # The Mac creates one of these beside every file it writes to the SMB share.
    stub_new = mailbox / "new" / "._20260922T000000Z-aaaaaaaa.json"
    stub_new.write_text("Mac OS X        \x00\x00\x00\x02\x00\x00\x00\x00")
    stub_proc = mailbox / "processing" / "hermes-mac" / "._20260922T000000Z-bbbbbbbb.json"
    stub_proc.write_text("Mac OS X        \x00\x00\x00\x02\x00\x00\x00\x00")
    old = time.time() - 30 * 60
    os.utime(stub_proc, (old, old))  # old enough to look like a dead claim to a naive reaper
    # a directory stub in the producer's namespace, whose real sibling exists -> swept
    stub_root_ok = mailbox / "._new"
    stub_root_ok.write_text("Mac OS X        \x00\x00\x00\x02\x00\x00\x00\x00")
    # a stub with no real sibling there -> must be left alone
    stub_root_keep = mailbox / "._ghost-that-does-not-exist"
    stub_root_keep.write_text("Mac OS X        \x00\x00\x00\x02\x00\x00\x00\x00")
    pf = write_payload_file(mailbox.parent, payload(name="stub-probe"))
    run_ingest(env, "--enqueue", str(pf))

    p = run_ingest(env)
    print("  (stub in new/ and an aged stub in processing/hermes-mac/ were present for this run)")

    check("stubs never reached failed/", list((mailbox / "failed").glob("*")) == [],
          str([q.name for q in (mailbox / "failed").glob("*")]))
    check("stubs were not requeued or reaped as dead claims",
          not re.search(r"(requeued|reaped) \S+\.json", p.stdout),
          p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "")
    check("stubs swept out of new/ and processing/",
          not stub_new.exists() and not stub_proc.exists())
    # requests/ is where macOS sidecars for parked asks land (the DockerVM hit this)
    stub_req = mailbox / "requests" / "._20260922T000000Z-eeeeeeee.json"
    stub_req.write_text("Mac OS X        \x00\x00\x00\x02\x00\x00\x00\x00")
    run_ingest(env)
    check("stubs swept out of requests/ as well", not stub_req.exists())
    check("directory stub swept where its real sibling exists", not stub_root_ok.exists())
    check("stub with no real sibling left alone (producer namespace)", stub_root_keep.exists())
    check("the real message still applied",
          len(list((mailbox / "processed").rglob("stub-probe*"))) == 0
          and len(list((mailbox / "processed").rglob("*.json"))) == 1)
    check("run reported the sweep", "stubs=" in p.stdout, p.stdout.strip().splitlines()[-1])
    st = run_ingest(env, "--status")
    check("status counts exclude hidden files",
          "new=0" in st.stdout and "failed=0" in st.stdout and "1 done" in st.stdout, st.stdout.split("\n")[1].strip())
    silent1 = run_ingest(env)
    silent2 = run_ingest(env)
    check("two consecutive idle runs are silent (no stub chatter)",
          silent1.stdout.strip() == "" and silent2.stdout.strip() == "",
          (silent1.stdout or silent2.stdout).strip())


def test_handshake():
    banner("TEST 8 — cross-side handshake: slug canonicalization, verdicts, requeue-failed")
    mailbox, vault, home = new_fixture("t8-handshake")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")

    def raw_message(mid, task_type, name="probe", ctype="new"):
        return {
            "message_id": mid, "author": "hermes-docker", "consumer": "hermes-mac",
            "created_at": "2026-09-22T00:24:18Z", "task_type": task_type,
            "kind": "notification", "schema": 1, "attempt": 0, "in_reply_to": None,
            "payload": payload(name=name, ctype=ctype),
        }

    # (a) the real incident: producer declared `dev-update` with a hyphen
    m1 = "20260922T010000Z-aaaa1111"
    (mailbox / "new" / f"{m1}.json").write_text(json.dumps(raw_message(m1, "dev-update", name="hyphen-probe"), indent=2))
    p = run_ingest(env)
    check("hyphenated task_type accepted, not quarantined",
          len(list((mailbox / "processed").rglob(f"{m1}.json"))) == 1
          and not (mailbox / "failed" / f"{m1}.json").exists())
    check("coercion reported in the run summary", "coerced=1" in p.stdout, p.stdout.strip().splitlines()[-1])
    check("nothing pending-verdict left for an accepted message",
          not (mailbox / "state" / f"{m1}.status").exists())
    st = json.loads((mailbox / "state" / "hermes-mac-ingest.state.json").read_text())
    check("last_result carries requeue + claim counters",
          all(k in st["last_result"] for k in ("requeued=", "claim_lost=", "claim_errors=")), st["last_result"])

    # (b) a genuinely unknown slug is rejected, and the reason is left where the producer can read it
    m2 = "20260922T010100Z-bbbb2222"
    (mailbox / "new" / f"{m2}.json").write_text(json.dumps(raw_message(m2, "dev_bogus", name="bogus-probe"), indent=2))
    run_ingest(env)
    vp = mailbox / "state" / f"{m2}.status"
    verdict = json.loads(vp.read_text()) if vp.exists() else {}
    check("rejection leaves a producer-readable verdict", vp.exists())
    check("verdict names the phase and lists the known task types",
          verdict.get("phase") == "rejected" and "dev_update" in str(verdict.get("last_reason")),
          str(verdict.get("last_reason")))
    check("rejection counted in state, not silently swallowed",
          "requeued=1" in json.loads((mailbox / "state" / "hermes-mac-ingest.state.json").read_text())["last_result"])
    for _ in range(2):
        run_ingest(env)
    check("still quarantined after 3 attempts", (mailbox / "failed" / f"{m2}.json").exists())
    check("--status surfaces the pending verdict",
          "pending verdicts:" in run_ingest(env, "--status").stdout)

    # (c) claim contention is counted rather than silent
    m3 = "20260922T010200Z-cccc3333"
    (mailbox / "new" / f"{m3}.json").write_text(json.dumps(raw_message(m3, "ping")))
    (mailbox / "processing" / "hermes-mac" / f"{m3}.json").write_text("{}")
    run_ingest(env)
    check("lost claim is counted in state",
          "claim_lost=1" in json.loads((mailbox / "state" / "hermes-mac-ingest.state.json").read_text())["last_result"])
    (mailbox / "processing" / "hermes-mac" / f"{m3}.json").unlink()
    (mailbox / "new" / f"{m3}.json").unlink()

    # (d) operator requeue after a consumer-side fix: clean slate, no half-spent attempts
    run_ingest(env, "--requeue-failed")
    check("--requeue-failed returns the quarantined message to new/", (mailbox / "new" / f"{m2}.json").exists())
    check("attempt counter reset", not (mailbox / "state" / f"{m2}.attempt").exists())
    check("reason sidecar cleared", not (mailbox / "failed" / f"{m2}.json.reason").exists())

    # (e) producer fixes its declared slug -> message flows through on the first clean attempt
    f = mailbox / "new" / f"{m2}.json"
    fixed = json.loads(f.read_text())
    fixed["task_type"] = "dev_update"
    f.write_text(json.dumps(fixed, indent=2))
    p = run_ingest(env)
    check("corrected message applies on its first clean attempt",
          len(list((mailbox / "processed").rglob(f"{m2}.json"))) == 1 and "applied=1" in p.stdout,
          p.stdout.strip().splitlines()[-1])
    check("its verdict file was cleared on success", not (mailbox / "state" / f"{m2}.status").exists())


def test_requests():
    banner("TEST 9 — task_request: parked, never executed, never failed for being unclaimed, never invisible")
    mailbox, vault, home = new_fixture("t9-requests")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")

    pf = mailbox.parent / "req.json"
    pf.write_text(json.dumps({
        "request": "change the line delta in Co-edit log.md line 37 from +19 to +6",
        "target": "Co-edit log.md line 37",
        "requires": ["vault write access"],
        "acceptance": "the entry reads (+6 lines) and the card is unchanged",
        "source": "20260922T010918Z-daf72f9e",
        "reply_expected": True,
    }, indent=2))
    run_ingest(env, "--enqueue", str(pf), "--task-type", "task_request",
               "--author", "hermes-docker", "--consumer", "hermes-mac")
    p = run_ingest(env)

    parked = list((mailbox / "requests").glob("*.json"))
    check("request parked in requests/", len(parked) == 1, parked[0].name if parked else "-")
    check("run says PARKED, not applied", "PARKED" in p.stdout and "parked=1" in p.stdout)
    notes = list((vault / "Hermes/Hermes Logs/dockerVM").glob("Dev Updates *.md"))
    check("request touched no dev-update log", notes == [])
    board = (vault / "Hermes/Hermes Kanban.md").read_text()
    check("request carded nothing", "change the line delta" not in board)
    check("no attempt counted (unclaimed is not broken)",
          not (mailbox / "state" / f"{parked[0].stem}.attempt").exists())
    rstate = json.loads((mailbox / "state" / f"{parked[0].stem}.request.json").read_text())
    check("request state records the ask + acceptance",
          rstate["status"] == "pending" and "line 37" in rstate["request"]
          and rstate["acceptance"].startswith("the entry reads"))
    check("human-visible mirror written to the vault",
          (vault / "Hermes/Mailbox Requests.md").exists()
          and "line 37" in (vault / "Hermes/Mailbox Requests.md").read_text())
    check("counters expose the unactioned request (not invisible)",
          "pending=1" in json.loads((mailbox / "state" / "hermes-mac-ingest.state.json").read_text())["last_result"])
    check("the run announced it",
          "REQUESTS PENDING: 1 unactioned" in p.stdout)
    check("--requests lists it with acceptance",
          "acceptance: the entry reads" in run_ingest(env, "--requests").stdout)
    check("--status marks it UNSERVED",
          "UNSERVED" in run_ingest(env, "--status").stdout)

    # a request claimed by a crashed consumer is re-parked, never failed
    claimed = mailbox / "processing" / "hermes-mac" / parked[0].name
    os.rename(parked[0], claimed)
    old = time.time() - 30 * 60
    os.utime(claimed, (old, old))
    p = run_ingest(env)
    check("stale claim re-parked into requests/, not failed/",
          len(list((mailbox / "requests").glob("*.json"))) == 1
          and list((mailbox / "failed").glob("*")) == [], p.stdout.strip().splitlines()[-1])
    check("re-park counted as a reap, not an attempt",
          not (mailbox / "state" / f"{claimed.stem}.attempt").exists())

    # close as done -> receipt on the existing dev_update/modified path
    rid = claimed.stem
    p = run_ingest(env, "--request-done", rid, "--summary", "Corrected line 37 to (+6 lines).")
    receipts = list((mailbox / "new").glob("*.json"))
    check("closing as done publishes exactly one receipt", len(receipts) == 1)
    rcv = json.loads(receipts[0].read_text()) if receipts else {}
    check("receipt reuses dev_update + change_type modified + in_reply_to",
          rcv.get("task_type") == "dev_update" and rcv.get("payload", {}).get("change_type") == "modified"
          and rcv.get("in_reply_to") == rid, json.dumps({k: rcv.get(k) for k in ("task_type", "in_reply_to")}))
    check("receipt is addressed to the requesting side",
          rcv.get("consumer") == "hermes-docker" and rcv.get("author") == "hermes-mac")
    check("request archived and marked done",
          len(list((mailbox / "processed").rglob(f"{rid}.json"))) == 1
          and json.loads((mailbox / "state" / f"{rid}.request.json").read_text())["status"] == "done")
    run_ingest(env)   # the counters are refreshed on the next pass, as in production
    check("closed request left the pending list",
          "pending=0" in json.loads((mailbox / "state" / "hermes-mac-ingest.state.json").read_text())["last_result"])

    # routing: the receipt is not ours to claim
    p = run_ingest(env)
    st_after = json.loads((mailbox / "state" / "hermes-mac-ingest.state.json").read_text())
    check("mail addressed to the other consumer is left alone (and is quiet, not chatter)",
          len(list((mailbox / "new").glob("*.json"))) == 1 and "others=1" in st_after["last_result"]
          and p.stdout.strip() == "", st_after["last_result"])
    check("--status splits claimable vs addressed-elsewhere",
          "addressed to another consumer: 1" in run_ingest(env, "--status").stdout)

    # kind: task carries the contract even under the wrong slug
    m2 = "20260922T030000Z-cccc0003"
    (mailbox / "new" / f"{m2}.json").write_text(json.dumps({
        "message_id": m2, "author": "hermes-docker", "consumer": "hermes-mac",
        "created_at": "2026-09-22T03:00:00Z", "task_type": "dev_update", "kind": "task",
        "schema": 1, "attempt": 0, "in_reply_to": None,
        "payload": {"summary": "untyped ask, parked by the kind contract"},
    }))
    p = run_ingest(env)
    check("kind: task parks even under a non-request slug",
          len(list((mailbox / "requests").glob(f"{m2}.json"))) == 1 and "kind=task" in p.stdout)

    # expires_at: closed, never executed late
    m3 = "20260922T030100Z-dddd0004"
    (mailbox / "new" / f"{m3}.json").write_text(json.dumps({
        "message_id": m3, "author": "hermes-docker", "consumer": "hermes-mac",
        "created_at": "2026-09-22T03:01:00Z", "task_type": "task_request", "kind": "task",
        "schema": 1, "attempt": 0, "in_reply_to": None,
        "payload": {"request": "expire this one", "reply_expected": True,
                    "expires_at": "2026-01-01T00:00:00Z"},
    }))
    p = run_ingest(env)
    check("an expired request is closed unexecuted",
          json.loads((mailbox / "state" / f"{m3}.request.json").read_text())["status"] == "expired"
          and "expired=1" in p.stdout, p.stdout.strip().splitlines()[-1])
    check("an expired request never publishes a receipt",
          not any(json.loads(x.read_text()).get("in_reply_to") == m3 for x in (mailbox / "new").glob("*.json")))

    # an ask parked for the OTHER consumer shares this namespace - it is not ours to action
    m4 = "20260922T030200Z-ffff0005"
    (mailbox / "requests" / f"{m4}.json").write_text(json.dumps({
        "message_id": m4, "author": "hermes-mac", "consumer": "hermes-docker",
        "created_at": "2026-09-22T03:02:00Z", "task_type": "task_request", "kind": "task",
        "schema": 1, "attempt": 0, "in_reply_to": None,
        "payload": {"request": "their ask, not mine", "reply_expected": True},
    }))
    (mailbox / "state" / f"{m4}.request.json").write_text(json.dumps({
        "message_id": m4, "status": "pending", "parked_at": "2026-09-22T03:02:00Z",
        "author": "hermes-mac", "file": f"{m4}.json", "request": "their ask, not mine",
        "target": "", "requires": [], "acceptance": "", "source": "", "expires_at": "",
        "reply_expected": True,
    }))
    before = run_ingest(env, "--requests").stdout
    n_before = int(re.search(r"(\d+) pending", before).group(1))
    run_ingest(env)
    after = run_ingest(env, "--requests").stdout
    n_after = int(re.search(r"(\d+) pending", after).group(1))
    check("an ask addressed to the other consumer is not counted as mine",
          n_after == n_before and "their ask, not mine" not in after,
          f"pending {n_before} -> {n_after}")
    check("it is not listed as UNSERVED either", "their ask, not mine" not in run_ingest(env, "--status").stdout)
    check("and it is not mirrored into my request note",
          "their ask, not mine" not in (vault / "Hermes/Mailbox Requests.md").read_text())


def test_carding_optin():
    banner("TEST 11 — carding is opt-in: the vault-side writer holds the pen")
    mailbox, vault, home = new_fixture("t11-carding")
    env = env_for(mailbox, vault, home)
    base = mailbox.parent
    board_path = vault / "Hermes/Hermes Kanban.md"
    run_ingest(env, "--provision")

    def send(**kw):
        pf = write_payload_file(base, payload(**kw))
        run_ingest(env, "--enqueue", str(pf)); run_ingest(env)

    def cards():
        txt = board_path.read_text()
        return [l.strip() for l in txt.splitlines() if l.startswith("- [ ]")]

    before = board_path.read_text()
    send(name="penside-test", ctype="new", card=False,
         summary="A producer that can write the board announces with modified and writes the card itself.")
    check("a dev_update without the card flag does not touch the board",
          board_path.read_text() == before and not (vault / "Hermes/Hermes Kanban.md.bak").exists())
    check("...but it is still logged in the Dev Updates note",
          "penside-test — new" in (list((vault / "Hermes/Hermes Logs/dockerVM").glob("Dev Updates *.md"))[0].read_text()))

    send(name="penside-test", ctype="new", card=True,
         summary="Carding asked for explicitly — this one should land.")
    txt = board_path.read_text()
    a = txt.find("## Hermes-dockerVM (Active)"); d = txt.find("## Done")
    c = re.search(r"- \[[ x]\] penside-test", txt).start()   # box-agnostic: active and done differ only by the box
    check("with `card: true` the card lands in their Active column", a != -1 and c != -1 and a < c < d,
          f"active@{a} card@{c} done@{d}")
    check("and it carries Status: active", "**Status:** active" in txt[c:c+600])
    check("with an UNCHECKED box (the plugin archives on the box, so this must stay in step)",
          txt[c:c+20].startswith("- [ ]"), txt[c:c+20])

    send(name="penside-test", ctype="new", card=True, summary="Same item again — must update, not duplicate.")
    check("a repeat for the same dev_item updates in place (no duplicate card)",
          len([x for x in cards() if "penside-test" in x]) == 1, str([x for x in cards() if "penside-test" in x]))
    check("cards() sees both box states (checked done cards are still cards)",
          all(re.match(r"- \[[ x]\] ", x) for x in cards()))

    send(name="penside-test", ctype="completed", card=True, summary="Landed.")
    txt = board_path.read_text()
    a = txt.find("## Hermes-dockerVM (Active)"); d = txt.find("## Done")
    c = re.search(r"- \[[ x]\] penside-test", txt).start()   # box-agnostic: active and done differ only by the box
    check("completed moves the card to Done", d != -1 and c > d, f"active@{a} card@{c} done@{d}")
    check("with Status: done", "**Status:** done" in txt[c:c+900])
    check("and a CHECKED box — done cards are checked cards", txt[c:c+20].startswith("- [x]"), txt[c:c+20])


def test_request_note_scope():
    banner("TEST 12 — the request note lists THIS side's requests, and says so")
    # Their ask of 2026-09-22 read my note as stale because it wanted three closes listed that were
    # requests *I sent them* — excluded here on purpose, since mirroring them is what once made the
    # other side's parked mail look like this side's pending work. The filter is right; the note just
    # never said whose requests it lists. Now it does, and it reports both sides' counts.
    mailbox, vault, home = new_fixture("t12-note-scope")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")

    mine = "20260922T180000Z-aaaa0001"
    (mailbox / "new" / f"{mine}.json").write_text(json.dumps({
        "message_id": mine, "author": "hermes-docker", "consumer": "hermes-mac",
        "created_at": "2026-09-22T18:00:00Z", "task_type": "task_request", "kind": "task",
        "schema": 1, "attempt": 0, "in_reply_to": None,
        "payload": {"request": "Please do the thing that is mine.", "reply_expected": True}}))

    other = "20260922T180100Z-bbbb0002"
    (mailbox / "requests" / f"{other}.json").write_text(json.dumps({
        "message_id": other, "author": "hermes-mac", "consumer": "hermes-docker",
        "created_at": "2026-09-22T18:01:00Z", "task_type": "task_request", "kind": "task",
        "schema": 1, "attempt": 0, "in_reply_to": None,
        "payload": {"request": "their work, not ours", "reply_expected": True}}))
    (mailbox / "state" / f"{other}.request.json").write_text(json.dumps({
        "message_id": other, "status": "done", "parked_at": "2026-09-22T18:01:00Z",
        "author": "hermes-mac", "consumer": "hermes-docker", "file": f"{other}.json",
        "request": "their work, not ours", "closed_at": "2026-09-22T18:05:00Z"}))

    run_ingest(env)
    note = (vault / "Hermes/Mailbox Requests.md").read_text()
    scope = [l for l in note.split("\n") if "Right now" in l]
    check("the note states whose requests it lists", "addressed to this side" in note)
    check("...carries the ask addressed to this side", mine in note)
    check("...never lists the other side's request", other not in note)
    check("...and reconciles both sides' counts",
          "1 pending here" in note and "1 closed on the other side" in note, scope[0] if scope else "no Scope line")
    lst = run_ingest(env, "--requests").stdout
    check("--requests reports the other side separately", "on the other side" in lst)


def test_publish_drops_stub():
    banner("TEST 13 — a publish cleans up its own AppleDouble stub")
    # macOS writes `._<name>` beside every file it puts on an SMB share, and rename() does not carry
    # the stub along, so one is left in tmp/ and one can appear beside the published file. A stub in
    # new/ raised UnicodeDecodeError in the DockerVM's consumer on 2026-09-22 and killed its run,
    # leaving the mail behind it unseen for three minutes. Their side now skips such files; this side
    # stops creating them, which is why publish() calls drop_stub() on both paths.
    import importlib.util
    mailbox, vault, home = new_fixture("t13-stub")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")

    spec = importlib.util.spec_from_file_location("ingest_under_test", Path(__file__).resolve().parent / "ingest.py")
    assert spec and spec.loader, "cannot load ingest.py for the in-process publish test"
    ing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ing)

    envelope = ing.make_envelope({"dev_item": "stub-test", "change_type": "modified", "summary": "stub case"},
                                 "hermes-mac", "dev_update")
    name = f"{envelope['message_id']}.json"
    tmp, dst = mailbox / "tmp" / name, mailbox / "new" / name
    tmp.write_text("{}")                                    # what the publisher would have written
    (tmp.parent / f"._{name}").write_text("stub")            # its sidecar, as the OS leaves it in tmp/
    (dst.parent / f"._{name}").write_text("stub")            # and one beside the destination
    ing.publish(mailbox, envelope)

    check("the message was published", dst.exists())
    check("no stub left in tmp/", not (tmp.parent / f"._{name}").exists())
    check("no stub left beside the published message", not (dst.parent / f"._{name}").exists())


def test_link_escaping():
    banner("TEST 10 — a message payload can never create a vault link (payload link escaping)")
    mailbox, vault, home = new_fixture("t10-escape")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")
    probe = payload(name="escape-probe", ctype="new",
                    summary="quotes [[wikilink]] and [[X]] as examples, plus an ![[embed]]")
    probe["details"] = ["detect [[wikilink]] references that resolve to nothing",
                        '{"issue": "broken link to [[X]]"}']
    pf = write_payload_file(mailbox.parent, probe)
    run_ingest(env, "--enqueue", str(pf))
    run_ingest(env)

    note = next((vault / "Hermes/Hermes Logs/dockerVM").glob("Dev Updates *.md"))
    body = note.read_text()
    bare = re.findall(r"(?<!`)(!?\[\[[^\]\n]*\]\])(?!`)", body)
    check("no bare wikilink anywhere in the generated entry", bare == [], str(bare))
    check("the example markup survived, escaped, in the note",
          "`[[wikilink]]`" in body and "`[[X]]`" in body)
    board = (vault / "Hermes/Hermes Kanban.md").read_text()
    check("kanban card Notes escaped too",
          not re.search(r"(?<!`)\[\[X\]\](?!`)", board))
    check("the summary line is escaped as well", "`[[wikilink]]`" in body.split("**Summary:**")[1][:200])


def test_kanban_archive_filing():
    banner("TEST 14 — filing finished cards: ticked Done + the plugin's archive, newest-first")
    _mailbox, vault, _home = new_fixture("t14-filing")
    board_path = vault / "Hermes/Hermes Kanban.md"
    arch = vault / "Hermes/05 Archive"
    arch.mkdir(parents=True, exist_ok=True)
    t = "\t"
    board_path.write_text(
        "---\n\nkanban-plugin: board\ntags:\n  - kanban\ncard_template: |\n  Task:\n  Node:\n\n---\n\n"
        "## Backlog\n\n- [ ] A backlog card\n\n\n"
        "## Hermes-Mac (Active)\n\n- [ ] An active card\n\n\n"
        "## Done\n\n"
        f"- [x] Done card one\n{t}- **Task:** Done card one\n{t}- **Status:** done\n\n"
        f"- [x] Done card two\n{t}- **Task:** Done card two\n{t}- **Status:** done\n\n"
        f"- [ ] Done but unticked\n{t}- **Task:** Done but unticked\n{t}- **Status:** done\n\n\n"
        "***\n\n## Archive\n\n"
        f"- [x] Archived card A\n{t}- **Task:** Archived card A\n{t}- **Status:** done — 2026-09-22\n\n"
        '%% kanban:settings\n```\n{"kanban-plugin":"board","archive-with-date":true}\n```\n%%')
    note = arch / "Hermes Kanban Archive 2026.md"
    note.write_text(
        "---\ntags:\n  - archive\n---\n\n# 🗄️ Hermes Kanban Archive 2026\n\nProse here.\n\n"
        "← [[Hermes/Hermes|Home]]\n\n"
        f"## Archived 2026-09-22\n\n- [x] An older card\n{t}- **Task:** An older card\n{t}- **Status:** done\n")

    roll = companion("kanban-archive-roll.py")
    today = datetime.now().strftime("%Y-%m-%d")

    def run_roll(*args):
        p = subprocess.run([PY, str(roll), "--vault", str(vault), *args], capture_output=True, text=True)
        print(f"  $ kanban-archive-roll.py {' '.join(args)}  -> rc={p.returncode}")
        for line in p.stdout.strip().splitlines():
            print(f"      {line}")
        return p

    def cards(text):
        return [x.group(1) for x in re.finditer(r"^[ \t]*- \[[ x]\][ \t]*(.*\S)?[ \t]*$", text, re.M)]

    def heads(text):
        return [m.group(1).strip() for m in re.finditer(r"^## Archived (.+?)\s*$", text, re.M)]

    before_board = board_path.read_text()
    p = run_roll("--dry-run")
    check("dry run reports the pass and writes nothing",
          p.returncode == 0 and "nothing written" in p.stdout
          and board_path.read_text() == before_board)
    p = run_roll()
    bt, nt = board_path.read_text(), note.read_text()
    check("rc=0", p.returncode == 0, str(p.returncode))
    check("board loses exactly the 3 finished cards, nothing else",
          len(cards(before_board)) - len(cards(bt)) == 3, f"{len(cards(before_board))} -> {len(cards(bt))}")
    check("an UNticked card in Done is left where it is", "Done but unticked" in bt)
    check("the empty Done column reads as the plugin expects", "## Done" in bt)
    check("the archive section and its `***` are gone from the board",
          "## Archive" not in bt and bt.count("***") == 0)
    check("kanban:settings survives the rebuild", '%% kanban:settings' in bt)
    check("the note is newest-first, with today's cards on top",
          heads(nt)[0] == today and nt.index(today) < nt.index("## Archived 2026-09-22"), str(heads(nt)))
    check("a second section for a date that already had one is MERGED, not duplicated",
          heads(nt).count("2026-09-22") == 1 and "Archived card A" in nt and "An older card" in nt,
          str(heads(nt)))
    check("cards merged into an EXISTING date section are PREPENDED (newest pass on top)",
          nt.index("Archived card A") < nt.index("An older card"),
          "the 2026-09-22 section must read: Archived card A, then An older card")
    check("the plugin's archive stamp is stripped from the filed card",
          "**Status:** done — 2026-09-22" not in nt)
    check("the note's existing prose header survived the rewrite", "Prose here." in nt)
    check("backs the board up before writing", (vault / "Hermes/Hermes Kanban.md.bak").exists())

    b0, n0 = bt, nt
    p = run_roll()
    check("a re-run is a no-op: rc=0, reports nothing to file",
          p.returncode == 0 and "nothing to file" in p.stdout)
    check("...and leaves both files byte-identical",
          board_path.read_text() == b0 and note.read_text() == n0)

    # a done-only pass must NOT touch the archive section: the plugin's parser needs the `***`
    # and the `## Archive` heading to appear together.
    board_path.write_text(
        "---\n\nkanban-plugin: board\n\n---\n\n## Done\n\n"
        f"- [x] A later card\n{t}- **Task:** A later card\n{t}- **Status:** done\n\n\n"
        "***\n\n## Archive\n\n"
        f"- [x] Still staged\n{t}- **Task:** Still staged\n{t}- **Status:** done — 2026-09-22\n\n"
        '%% kanban:settings\n```\n{"kanban-plugin":"board"}\n```\n%%')
    run_roll("--source", "done")
    bt = board_path.read_text()
    check("--source done files the Done card", "A later card" not in bt)
    check("--source done leaves a staged archive section intact",
          "## Archive" in bt and "Still staged" in bt)
    check("...with its `***` and exactly one of them",
          bt.count("***") == 1 and re.search(r"\*\*\*\s*\n\s*## Archive", bt) is not None)

    nt = note.read_text()
    check("a LATER pass files above the earlier cards of the same date",
          nt.index("A later card") < nt.index("Done card one"),
          "today's section must lead with the card the later pass filed")


def test_stamp_ages():
    banner("TEST 15 — a state stamp carries its offset, and a parked ask's age is real")
    # 2026-10-10: park() wrote datetime.now() — naive — and strftime("%z") is empty on a naive
    # value, so every parked ask carried an offset-less stamp; age_hours() then failed to parse it
    # and its except-branch returned 0.0, which reads exactly like "parked seconds ago". The age
    # column and oldest_pending were meaningless and a five-day-old ask never aged. The stamp must
    # now carry its offset AND the reader must still handle the offset-less records already on disk.
    import importlib.util
    from datetime import datetime as _dt, timedelta as _td

    spec = importlib.util.spec_from_file_location("ingest_ages", Path(__file__).resolve().parent / "ingest.py")
    assert spec and spec.loader, "cannot load ingest.py for the age test"
    ing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ing)

    stamp = ing.stamp_now()
    try:
        _dt.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
        offset_ok = True
    except ValueError:
        offset_ok = False
    check("stamp_now() carries a UTC offset", offset_ok, stamp)
    check("stamp_now() reads back as ~0 h", abs(ing.age_hours(stamp)) < 0.01,
          f"{ing.age_hours(stamp):.3f}")

    # A naive stamp is local WALL time, and wall time is not elapsed time: five hours back can
    # span a DST boundary and be six real hours on the clock -- it is, on the fall-back morning.
    # So assert the INTERPRETATION (local wall time, never UTC) plus a band wide enough for the
    # one-hour skew, instead of a literal 5.0 that only holds away from a transition.
    legacy_dt = (_dt.now() - _td(hours=5)).replace(microsecond=0)   # the old, naive shape
    legacy = legacy_dt.strftime("%Y-%m-%dT%H:%M:%S")
    age = ing.age_hours(legacy)
    now_local = _dt.now().astimezone()
    as_local = (now_local - legacy_dt.astimezone()).total_seconds() / 3600.0
    as_utc = (now_local - legacy_dt.replace(tzinfo=timezone.utc)).total_seconds() / 3600.0
    offset_h = (now_local.utcoffset() or _td(0)).total_seconds() / 3600.0
    check("a legacy offset-less stamp is read as local wall time", abs(age - as_local) < 0.01,
          f"{age:.3f} h vs local {as_local:.3f} h")
    check("a legacy offset-less stamp is not read as UTC",
          abs(offset_h) < 0.01 or abs(age - as_utc) > 0.5,
          f"{age:.3f} h vs utc {as_utc:.3f} h, host offset {offset_h:+.0f} h")
    check("a legacy offset-less stamp reports a real age, not 0.0", 4.0 < age < 6.1,
          f"{age:.3f} h")
    check("an unparseable stamp still degrades to 0.0 instead of raising",
          ing.age_hours("not-a-stamp") == 0.0 and ing.age_hours("") == 0.0)

    mailbox, vault, home = new_fixture("t15-ages")
    env = env_for(mailbox, vault, home)
    run_ingest(env, "--provision")
    pf = mailbox.parent / "ages.json"
    pf.write_text(json.dumps({
        "request": "prove the parked stamp parses",
        "target": "-", "requires": [], "acceptance": "-",
        "source": "acceptance", "reply_expected": False,
    }, indent=2))
    run_ingest(env, "--enqueue", str(pf), "--task-type", "task_request",
               "--author", "hermes-docker", "--consumer", "hermes-mac")
    run_ingest(env)
    parked = list((mailbox / "requests").glob("*.json"))
    rec = json.loads((mailbox / "state" / f"{parked[0].stem}.request.json").read_text())
    try:
        _dt.strptime(rec["parked_at"], "%Y-%m-%dT%H:%M:%S%z")
        rec_ok = True
    except ValueError:
        rec_ok = False
    check("a parked record's parked_at parses with its offset", rec_ok, rec["parked_at"])


def test_instance_paths():
    banner("TEST 16 — instance paths: the file supplies the vault, the env beats it, a missing vault is fatal")
    mailbox, vault, home = new_fixture("t16-instance")
    base = mailbox.parent
    other = base / "vault-env-wins"
    (other / "Hermes/Hermes Logs/dockerVM").mkdir(parents=True)
    (other / "Hermes/Hermes Kanban.md").write_text(BOARD_FIXTURE)
    (other / "Co-edit log.md").write_text("# Co-edit log\n\n")

    envfile = base / "sync-bridge.env"
    envfile.write_text(f"# instance file -- never in the repo\nVAULT_ROOT={vault}\n")

    env = env_for(mailbox, vault, home)
    env.pop("VAULT_ROOT")                      # nothing but the file names the vault now
    env["SYNC_BRIDGE_ENV"] = str(envfile)

    run_ingest(env, "--provision")
    run_ingest(env, "--enqueue", str(write_payload_file(base, payload())))
    run_ingest(env)
    check("the instance file supplies VAULT_ROOT when the environment does not",
          "- [ ] worldmonitor-v2" in (vault / "Hermes/Hermes Kanban.md").read_text())

    # The environment must still win over the file: otherwise a sandbox run that forgets
    # VAULT_ROOT would write to whatever the instance file names — the real vault.
    run_ingest(env, "--enqueue", str(write_payload_file(base, payload(name="env-wins"))))
    env2 = dict(env)
    env2["VAULT_ROOT"] = str(other)
    run_ingest(env2)
    check("VAULT_ROOT in the environment beats the instance file",
          "- [ ] env-wins" in (other / "Hermes/Hermes Kanban.md").read_text()
          and "- [ ] env-wins" not in (vault / "Hermes/Hermes Kanban.md").read_text())

    # A vault that is not there must stop the run, not read as an empty queue.
    env3 = dict(env)
    env3.pop("SYNC_BRIDGE_ENV", None)
    env3["VAULT_ROOT"] = str(base / "no-such-vault")
    p = subprocess.run([PY, str(INGEST)], env=env3, capture_output=True, text=True)
    check("a missing vault exits non-zero and names the fix",
          p.returncode != 0 and "not a directory" in (p.stdout + p.stderr),
          f"rc={p.returncode} {(p.stderr or p.stdout).strip().splitlines()[:1]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.parse_args()

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)
    print(f"sandbox: {SANDBOX}\ninterpreter: {PY}")

    mailbox, vault, home, env = test_round_trip()
    test_poison()
    test_dead_claim()
    test_redelivery(mailbox, vault, env)
    test_torn_write()
    test_legacy_retired()
    test_stubs()
    test_handshake()
    test_requests()
    test_link_escaping()
    test_request_note_scope()
    test_publish_drops_stub()
    test_carding_optin()
    test_kanban_archive_filing()
    test_stamp_ages()
    test_instance_paths()

    banner("SUMMARY")
    passed = sum(1 for _l, ok, _d in RESULTS if ok)
    for label, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
