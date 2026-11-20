#!/usr/bin/env python3
"""parity_check.py — prove both Hermes hosts still run the same pinned toolchain.

Read-only by construction. It never writes a file, never creates state, and it never asks
code-review-graph to open a remote graph: `status`, `doctor`, `query` and the rest migrate
`graph.db` in place on first open, which would corrupt the other host's pinned tree. The remote
graph is read with sqlite only (mode=ro&immutable=1) through the VM's own pinned python, because
the VM has no sqlite3 CLI and a plain mode=ro open needs the live -shm.

Exit codes: 0 = every check measured and equal; 1 = drift; 2 = nothing drifted but at least one
check could not be measured (unreachable host, unparseable output, missing interpreter), or the
pin cannot be read. Drift outranks unevaluable when both are present.

Every version, path and sha comes from config/toolchain-pin.yaml. Nothing here is host-specific
except the pin's own host keys.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

try:
    import yaml
except ImportError as exc:  # the wrapper checks this too; a direct python3 run must not guess
    print(
        "parity_check: PyYAML is required and this interpreter cannot import it "
        f"({exc}). Use mac/parity-check.sh, or install PyYAML into the interpreter.",
        file=sys.stderr,
    )
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PIN = REPO_ROOT / "config" / "toolchain-pin.yaml"
# mac/deploy.sh installs this engine beside a copy of the pin, so the internal-disk cron entry
# point can find it even when the engine runs outside the repo. The repo layout always wins;
# the sibling is only the installed layout's default.
SIBLING_PIN = Path(__file__).resolve().parent / "toolchain-pin.yaml"
GRAPH_DIR = ".code-review-graph"
GRAPH_DB = "graph.db"
COUNT_DRIFT_RATIO = 0.10  # a rebuild may land slightly different numbers; a 10% move is not
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.]+)?)")
GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # `git status` must not refresh/write the index

# --------------------------------------------------------------------------------------------
# rows and process plumbing
# --------------------------------------------------------------------------------------------


@dataclass
class Row:
    check: str
    host: str
    expected: str
    actual: str
    status: str  # ok | drift | unevaluable | skipped
    note: str = ""

    @property
    def ok(self):
        if self.status == "ok":
            return True
        if self.status == "drift":
            return False
        return None  # skipped / unevaluable are measured false, not equal

    def as_json(self):
        return {
            "check": self.check,
            "host": self.host,
            "expected": self.expected,
            "actual": self.actual,
            "ok": self.ok,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class Run:
    argv: list
    rc: int | None
    out: str = ""
    err: str = ""
    error: str = ""


def run(argv, timeout, env=None):
    """Run a command, capturing output. Never raises for a broken command."""
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        return Run(argv, None, error=f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return Run(argv, None, error=f"timed out after {timeout}s: {argv[0]}")
    except OSError as exc:
        return Run(argv, None, error=f"cannot run {argv[0]}: {exc}")
    return Run(argv, proc.returncode, proc.stdout or "", proc.stderr or "")


def ssh_argv(host, remote_argv, timeout):
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        host,
        " ".join(shlex.quote(str(a)) for a in remote_argv),
    ]


def expand(value):
    """$HOME / ~ in a pin value -> a real path string."""
    return os.path.expanduser(os.path.expandvars(str(value)))


def pin_get(pin, dotted, default=None):
    node = pin
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def parse_version(text):
    match = VERSION_RE.search(text or "")
    return match.group(1) if match else None


def first_line(text, limit=160):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def short(sha):
    return (sha or "")[:12]


def default_pin():
    """The repo pin; the copy beside the engine when this is the deployed layout."""
    if DEFAULT_PIN.is_file():
        return DEFAULT_PIN
    if SIBLING_PIN.is_file():
        return SIBLING_PIN
    return DEFAULT_PIN


# --------------------------------------------------------------------------------------------
# graph reads (sqlite only, read-only, immutable)
# --------------------------------------------------------------------------------------------

REMOTE_GRAPH_CODE = r"""
import json, sqlite3, sys
from urllib.parse import quote
db = sys.argv[1]
con = sqlite3.connect("file:" + quote(db, safe="/") + "?mode=ro&immutable=1", uri=True)
meta = dict(con.execute("select key, value from metadata"))
count = lambda q: con.execute(q).fetchone()[0]
print(json.dumps({
    "metadata": meta,
    "nodes": count("select count(*) from nodes"),
    "edges": count("select count(*) from edges"),
    "embeddings": count("select count(*) from embeddings"),
    "files": count("select count(*) from nodes where kind = 'File'"),
}))
"""


def read_graph_local(db_path):
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        con.execute("pragma query_only=ON")
        meta = dict(con.execute("select key, value from metadata"))
        count = lambda q: con.execute(q).fetchone()[0]
        return {
            "metadata": meta,
            "nodes": count("select count(*) from nodes"),
            "edges": count("select count(*) from edges"),
            "embeddings": count("select count(*) from embeddings"),
            "files": count("select count(*) from nodes where kind = 'File'"),
        }
    finally:
        con.close()


def read_graph_remote(python, db_path, host, timeout):
    argv = ssh_argv(host, [python, "-c", REMOTE_GRAPH_CODE, str(db_path)], timeout)
    result = run(argv, timeout + 5)
    if result.error:
        return None, result.error
    if result.rc != 0:
        return None, f"sqlite read failed (rc={result.rc}): {first_line(result.err) or first_line(result.out)}"
    try:
        return json.loads(result.out), None
    except (ValueError, TypeError):
        return None, f"unparseable sqlite output: {first_line(result.out) or 'empty'}"


# --------------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------------


@dataclass
class Checker:
    pin: dict
    timeout: int
    no_vm: bool
    rows: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    # -- small helpers ---------------------------------------------------------------------

    @property
    def vm_host(self):
        return str(pin_get(self.pin, "hosts.vm.ssh") or "").strip()

    def host_run(self, host, argv):
        if host == "mac":
            return run(argv, self.timeout, env=GIT_ENV)
        result = run(ssh_argv(self.vm_host, argv, self.timeout), self.timeout + 5, env=GIT_ENV)
        if result.rc == 255 and not result.out.strip():
            result.error = f"ssh {self.vm_host} failed: {first_line(result.err)}"
        return result

    def add(self, row):
        self.rows.append(row)
        return row

    def unevaluable(self, check, host, expected, reason, actual="—"):
        return self.add(Row(check, host, expected, actual, "unevaluable", reason))

    # -- shared check shapes ---------------------------------------------------------------

    def check_version(self, host, check, argv, expected, label):
        result = self.host_run(host, argv)
        if result.error:
            return self.unevaluable(check, host, expected, result.error)
        if result.rc != 0:
            return self.unevaluable(
                check,
                host,
                expected,
                f"`{label}` exited {result.rc}: {first_line(result.err) or first_line(result.out) or 'no output'}",
            )
        actual = parse_version(result.out)
        if actual is None:
            return self.unevaluable(
                check, host, expected, f"unparseable `{label}` output: {first_line(result.out) or 'empty'}"
            )
        status = "ok" if actual == expected else "drift"
        return self.add(Row(check, host, expected, actual, status))

    def check_head(self, host, checkout, expected_sha):
        result = self.host_run(host, ["git", "-C", checkout, "rev-parse", "HEAD"])
        if result.error:
            return self.unevaluable("harness-head", host, short(expected_sha), result.error)
        if result.rc != 0:
            return self.unevaluable(
                "harness-head",
                host,
                short(expected_sha),
                f"git rev-parse failed: {first_line(result.err)}",
            )
        head = result.out.strip()
        if not head:
            return self.unevaluable("harness-head", host, short(expected_sha), "git printed no HEAD")
        ok = bool(expected_sha) and head.startswith(expected_sha)
        return self.add(
            Row(
                "harness-head",
                host,
                short(expected_sha),
                short(head),
                "ok" if ok else "drift",
                "" if ok else f"head is {head}",
            )
        )

    def check_dirty(self, host, checkout):
        result = self.host_run(host, ["git", "-C", checkout, "status", "--porcelain"])
        if result.error:
            return self.unevaluable("harness-dirty", host, "clean", result.error)
        if result.rc != 0:
            return self.unevaluable(
                "harness-dirty",
                host,
                "clean",
                f"git status failed: {first_line(result.err)}",
            )
        paths = [line for line in result.out.splitlines() if line.strip()]
        if not paths:
            return self.add(Row("harness-dirty", host, "clean", "clean", "ok", "report-only"))
        shown = ", ".join(p.strip() for p in paths[:5])
        if len(paths) > 5:
            shown += f", … (+{len(paths) - 5})"
        # A dirty tree is reported, never reset and never drift. This row cannot fail.
        return self.add(
            Row(
                "harness-dirty",
                host,
                "clean",
                f"dirty ({len(paths)} paths)",
                "ok",
                f"report-only, never drift: {shown}",
            )
        )

    def check_graph(self, host, host_key, python, checkout, head):
        expected_schema = pin_get(self.pin, "crg.graph_schema")
        expected_head = head or ""
        expected = f"schema={expected_schema if expected_schema is not None else '?'} head={short(expected_head) or '?'}"
        if expected_schema is None:
            return self.unevaluable(
                "graph", host, expected, "pin has no crg.graph_schema (refusing to invent one)"
            )

        db_path = Path(checkout) / GRAPH_DIR / GRAPH_DB
        if host == "mac":
            if not db_path.is_file():
                return self.add(
                    Row(
                        "graph",
                        host,
                        expected,
                        "absent",
                        "ok",
                        f"no {GRAPH_DB} at {db_path} — baseline, not damage",
                    )
                )
            try:
                data = read_graph_local(db_path)
            except (sqlite3.Error, OSError) as exc:
                return self.unevaluable("graph", host, expected, f"sqlite read failed: {exc}")
        else:
            if not python:
                return self.unevaluable("graph", host, expected, "pin has no hosts.vm.crg_python")
            data, error = read_graph_remote(python, db_path, self.vm_host, self.timeout)
            if error and "no such table" not in error and "unable to open" not in error.lower():
                return self.unevaluable("graph", host, expected, error)
            if error:
                # Could be an absent file or a file that is not a graph. An absent graph is
                # baseline (see the brief); anything else that cannot be opened is damage.
                probe = self.host_run(host, ["test", "-f", str(db_path)])
                if probe.error:
                    return self.unevaluable("graph", host, expected, probe.error)
                if probe.rc == 0:
                    return self.add(
                        Row("graph", host, expected, "unreadable", "drift", f"sqlite: {error}")
                    )
                if probe.rc != 1:
                    return self.unevaluable(
                        "graph", host, expected, f"cannot tell whether {db_path} exists: {error}"
                    )
                return self.add(
                    Row(
                        "graph",
                        host,
                        expected,
                        "absent",
                        "ok",
                        f"no {GRAPH_DB} at {db_path} — baseline, not damage",
                    )
                )
        meta = data.get("metadata") or {}
        actual_schema = meta.get("schema_version")
        stored = meta.get("git_head_sha") or ""
        if not expected_head:
            # The graph itself answered, but head_matches_build cannot be judged without HEAD.
            # Reporting ok would claim a measurement that was never made.
            return self.add(
                Row(
                    "graph",
                    host,
                    expected,
                    f"schema={actual_schema} head={short(stored)}",
                    "unevaluable",
                    "HEAD is unreadable, so head_matches_build cannot be evaluated",
                )
            )
        head_ok = bool(stored) and (
            expected_head.startswith(stored) or stored.startswith(expected_head)
        )
        schema_ok = str(actual_schema) == str(expected_schema)
        status = "ok" if (schema_ok and head_ok) else "drift"

        notes = []
        if not schema_ok:
            notes.append(f"schema {actual_schema} != pinned {expected_schema}")
        if not head_ok:
            notes.append(f"graph built at {short(stored) or 'unknown'}, HEAD {short(expected_head) or 'unknown'}")

        counts = {
            "files": data.get("files"),
            "nodes": data.get("nodes"),
            "edges": data.get("edges"),
            "embeddings": data.get("embeddings"),
        }
        notes.append(
            "files {files} nodes {nodes} edges {edges} embeddings {embeddings}".format(**counts)
        )

        pinned = pin_get(self.pin, f"harness_graph.{host_key}") or {}
        pinned_files = pin_get(self.pin, "harness_graph.files")
        if isinstance(pinned_files, int) and counts["files"] is not None and counts["files"] != pinned_files:
            self.warnings.append(
                f"{host} graph files {counts['files']} vs pinned {pinned_files} "
                f"(a clean rebuild should land exactly on the pinned file count)"
            )
        if isinstance(pinned, dict):
            for key in ("nodes", "edges", "embeddings"):
                base = pinned.get(key)
                have = counts.get(key)
                if isinstance(base, int) and base and isinstance(have, int):
                    delta = (have - base) / base
                    if abs(delta) > COUNT_DRIFT_RATIO:
                        self.warnings.append(
                            f"{host} graph {key} {have} vs pinned {base} "
                            f"({delta:+.1%}) — beyond the 10% rebuild tolerance"
                        )
        if isinstance(counts["nodes"], int) and isinstance(counts["files"], int) and isinstance(
            counts["embeddings"], int
        ):
            expected_emb = counts["nodes"] - counts["files"]
            if counts["embeddings"] != expected_emb:
                self.warnings.append(
                    f"{host} graph embeddings {counts['embeddings']} != nodes - File "
                    f"{expected_emb} (the pin's embeddable-set rule)"
                )

        return self.add(Row("graph", host, expected, f"schema={actual_schema} head={short(stored)}", status, "; ".join(notes)))

    # -- mac checks ------------------------------------------------------------------------

    def mac_checks(self):
        pin = self.pin
        py = pin_get(pin, "hosts.mac.crg_python")
        checkout = pin_get(pin, "hosts.mac.checkout")
        expected_crg = pin_get(pin, "crg.version")
        expected_dsh = pin_get(pin, "dsh.version")
        expected_sha = pin_get(pin, "dsh.sha")
        copies_expected = pin_get(pin, "hosts.mac.crg_copies_expected")

        if not py:
            self.unevaluable("crg-version", "mac", str(expected_crg), "pin has no hosts.mac.crg_python")
        else:
            self.check_version(
                "mac",
                "crg-version",
                [expand(py), "-m", "code_review_graph", "--version"],
                str(expected_crg),
                "code_review_graph --version",
            )

        if copies_expected is None:
            self.unevaluable("crg-copies", "mac", "?", "pin has no hosts.mac.crg_copies_expected")
        else:
            self.check_copies("mac", copies_expected)

        if not expected_dsh:
            self.unevaluable("dsh-version", "mac", "?", "pin has no dsh.version")
        else:
            self.check_version("mac", "dsh-version", ["dsh", "--version"], str(expected_dsh), "dsh --version")

        if not checkout:
            self.unevaluable("harness-head", "mac", short(expected_sha), "pin has no hosts.mac.checkout")
            self.unevaluable("harness-dirty", "mac", "clean", "pin has no hosts.mac.checkout")
            self.unevaluable("graph", "mac", "?", "pin has no hosts.mac.checkout")
            self.check_mcp()
            return

        checkout = expand(checkout)
        head = self.head_of("mac", checkout)
        self.check_head("mac", checkout, str(expected_sha or ""))
        self.check_dirty("mac", checkout)
        self.check_graph("mac", "mac", expand(py) if py else None, checkout, head)
        self.check_mcp()

    def head_of(self, host, checkout):
        """HEAD once, so the graph row compares against the same value the head row shows."""
        result = self.host_run(host, ["git", "-C", checkout, "rev-parse", "HEAD"])
        if result.error or result.rc != 0:
            return ""
        return result.out.strip()

    def check_copies(self, host, expected):
        result = self.host_run(host, ["which", "-a", "code-review-graph"])
        if result.error:
            return self.unevaluable("crg-copies", host, str(expected), result.error)
        if result.rc not in (0, 1):
            return self.unevaluable(
                "crg-copies", host, str(expected), f"which -a exited {result.rc}: {first_line(result.err)}"
            )
        lines = [line.strip() for line in result.out.splitlines() if line.strip()]
        # `which -a` prints one line per PATH entry, so a PATH with the same directory twice
        # reports the same copy three times. A copy is a physical binary: dedupe by realpath.
        copies = []
        for word in lines:
            real = os.path.realpath(expand(word))
            if real not in copies:
                copies.append(real)
        actual = str(len(copies))
        try:
            expected_count = int(expected)
        except (TypeError, ValueError):
            return self.unevaluable(
                "crg-copies", host, str(expected), f"pin value is not an integer: {expected!r}"
            )
        status = "ok" if len(copies) == expected_count else "drift"
        note = ", ".join(copies)
        return self.add(Row("crg-copies", host, str(expected), actual, status, note))

    def check_mcp(self):
        config = Path.home() / ".hermes" / "config.yaml"
        expected = pin_get(self.pin, "hosts.mac.crg_python")
        if not expected:
            return self.unevaluable("mcp-config", "mac", "?", "pin has no hosts.mac.crg_python")
        expected = expand(expected)
        try:
            with open(config, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            return self.unevaluable("mcp-config", "mac", expected, f"no config at {config}")
        except (OSError, yaml.YAMLError) as exc:
            return self.unevaluable("mcp-config", "mac", expected, f"cannot read {config}: {exc}")
        servers = data.get("mcp_servers") if isinstance(data, dict) else None
        entry = servers.get("code-review-graph") if isinstance(servers, dict) else None
        command = entry.get("command") if isinstance(entry, dict) else None
        if not isinstance(command, str) or not command:
            return self.unevaluable(
                "mcp-config", "mac", expected, "mcp_servers['code-review-graph'].command is absent"
            )
        status = "ok" if command == expected else "drift"
        return self.add(
            Row(
                "mcp-config",
                "mac",
                expected,
                command,
                status,
                "" if status == "ok" else "the MCP server would start a different interpreter",
            )
        )

    # -- dockerVM checks -------------------------------------------------------------------

    def vm_expected(self):
        return {
            "crg-version": str(pin_get(self.pin, "crg.version", "?")),
            "dsh-version": str(pin_get(self.pin, "dsh.version", "?")),
            "harness-head": short(str(pin_get(self.pin, "dsh.sha", ""))),
            "harness-dirty": "clean",
            "graph": "schema={} head={}".format(
                pin_get(self.pin, "crg.graph_schema", "?"),
                short(str(pin_get(self.pin, "dsh.sha", ""))),
            ),
        }

    def vm_placeholder_rows(self, status, actual, note):
        for check, expected in self.vm_expected().items():
            self.add(Row(check, "dockerVM", expected, actual, status, note))

    def vm_reachable(self):
        result = self.host_run("dockerVM", ["true"])
        if result.error:
            return False, result.error
        if result.rc != 0:
            return False, f"ssh exit {result.rc}: {first_line(result.err) or 'no output'}"
        return True, ""

    def vm_checks(self):
        if self.no_vm:
            self.vm_placeholder_rows("skipped", "skipped (--no-vm)", "not measured")
            return

        if not self.vm_host:
            self.vm_placeholder_rows(
                "unevaluable", "—", "pin has no hosts.vm.ssh (refusing to guess a hostname)"
            )
            return

        reachable, reason = self.vm_reachable()
        if not reachable:
            self.vm_placeholder_rows("unevaluable", "—", f"dockerVM unreachable: {reason}")
            return

        pin = self.pin
        checkout = pin_get(pin, "hosts.vm.checkout")
        py = pin_get(pin, "hosts.vm.crg_python")
        expected_crg = pin_get(pin, "crg.version")
        expected_dsh = pin_get(pin, "dsh.version")
        expected_sha = str(pin_get(pin, "dsh.sha", ""))

        if not py:
            self.unevaluable("crg-version", "dockerVM", str(expected_crg), "pin has no hosts.vm.crg_python")
        else:
            self.check_version(
                "dockerVM",
                "crg-version",
                [py, "-m", "code_review_graph", "--version"],
                str(expected_crg),
                "code_review_graph --version",
            )

        if not expected_dsh:
            self.unevaluable("dsh-version", "dockerVM", "?", "pin has no dsh.version")
        else:
            self.check_version(
                "dockerVM", "dsh-version", ["dsh", "--version"], str(expected_dsh), "dsh --version"
            )

        if not checkout:
            self.unevaluable("harness-head", "dockerVM", short(expected_sha), "pin has no hosts.vm.checkout")
            self.unevaluable("harness-dirty", "dockerVM", "clean", "pin has no hosts.vm.checkout")
            self.unevaluable("graph", "dockerVM", "?", "pin has no hosts.vm.checkout")
            return

        head = self.head_of("dockerVM", checkout)
        self.check_head("dockerVM", checkout, expected_sha)
        self.check_dirty("dockerVM", checkout)
        self.check_graph("dockerVM", "vm", py, checkout, head)

    # -- run -------------------------------------------------------------------------------

    def run_all(self):
        self.mac_checks()
        self.vm_checks()
        return self.rows


# --------------------------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------------------------


def truncate(value, limit):
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


STATUS_LABEL = {"ok": "ok", "drift": "DRIFT", "unevaluable": "?", "skipped": "skip"}


def render_table(rows):
    headers = ("status", "check", "host", "expected", "actual", "note")
    limits = (5, 18, 10, 60, 60, 90)
    cells = []
    for row in rows:
        cells.append(
            (
                STATUS_LABEL.get(row.status, row.status),
                row.check,
                row.host,
                row.expected,
                row.actual,
                row.note,
            )
        )
    widths = []
    for i, head in enumerate(headers):
        natural = max([len(head)] + [len(truncate(c[i], limits[i])) for c in cells])
        widths.append(min(natural, limits[i]))
    out = ["  ".join(head.ljust(w) for head, w in zip(headers, widths)).rstrip()]
    for c in cells:
        out.append(
            "  ".join(truncate(v, limits[i]).ljust(w) for i, (v, w) in enumerate(zip(c, widths))).rstrip()
        )
    return "\n".join(out)


def summarise(rows, warnings):
    drift = sum(1 for r in rows if r.status == "drift")
    unevaluable = sum(1 for r in rows if r.status == "unevaluable")
    skipped = sum(1 for r in rows if r.status == "skipped")
    measured = {
        host: sum(1 for r in rows if r.host == host and r.status in ("ok", "drift"))
        for host in ("mac", "dockerVM")
    }
    if drift:
        exit_code = 1
    elif unevaluable:
        exit_code = 2
    else:
        exit_code = 0
    summary = {
        "checks": len(rows),
        "drift": drift,
        "unevaluable": unevaluable,
        "skipped": skipped,
        "warnings": len(warnings),
        "measured": measured,
        "exit_code": exit_code,
    }
    return summary


def summary_line(summary):
    text = (
        f"{summary['checks']} checks, {summary['drift']} drift, "
        f"{summary['unevaluable']} unevaluable "
        f"({summary['measured']['mac']} mac, {summary['measured']['dockerVM']} dockerVM measured"
    )
    if summary["skipped"]:
        text += f", {summary['skipped']} skipped"
    if summary["warnings"]:
        noun = "warning" if summary["warnings"] == 1 else "warnings"
        text += f", {summary['warnings']} {noun}"
    return text + ")"


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="parity_check.py",
        description="Read-only parity check for the pinned toolchain on both Hermes hosts.",
    )
    default_pin_path = default_pin()
    parser.add_argument(
        "--pin",
        default=str(default_pin_path),
        help=f"toolchain pin to read (default: {default_pin_path})",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object instead of a table")
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        metavar="N",
        help="seconds per remote command (default: 20)",
    )
    parser.add_argument("--no-vm", action="store_true", help="measure the Mac only; mark the VM rows skipped")
    return parser.parse_args(argv)


def load_pin(path):
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("pin is not a mapping")
    return data


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.timeout <= 0:
        print("parity_check: --timeout must be a positive number of seconds", file=sys.stderr)
        return 2

    try:
        pin = load_pin(args.pin)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        message = f"pin cannot be read: {args.pin}: {exc}"
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": 2,
                        "checks": [],
                        "summary": {
                            "checks": 0,
                            "drift": 0,
                            "unevaluable": 0,
                            "skipped": 0,
                            "warnings": 0,
                            "measured": {"mac": 0, "dockerVM": 0},
                            "exit_code": 2,
                        },
                        "pin": {"path": str(args.pin)},
                        "error": message,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"parity: {message}", file=sys.stderr)
        return 2

    checker = Checker(pin=pin, timeout=args.timeout, no_vm=args.no_vm)
    rows = checker.run_all()
    summary = summarise(rows, checker.warnings)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": summary["exit_code"] == 0,
                    "exit_code": summary["exit_code"],
                    "checks": [row.as_json() for row in rows],
                    "summary": summary,
                    "pin": dict(pin, path=str(Path(args.pin).expanduser())),
                    "warnings": checker.warnings,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(render_table(rows))
        for warning in checker.warnings:
            print(f"warn: {warning}")
        print(summary_line(summary))

    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
