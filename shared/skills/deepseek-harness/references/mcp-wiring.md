# Wiring an MCP server into dsh

dsh builds its plugin tree from YAML patch layers. The order differs by line, and getting it
wrong is silent:

- **0.2.x (current pin: 0.2.2-rc.5)** — each bundle's patch, then the profile's
  `~/.dsh/profiles/<profile>/cordis.patch.yml`, then the **home-level**
  `$DSH_HOME/cordis.patch.yml` (default `~/.dsh/cordis.patch.yml`). The home file is composed for
  every profile, the desktop one included, so it is the right place for a server every profile needs.
- **0.1.x** — only the per-profile layer exists (`~/.dsh/profiles/<profile>/cordis.patch.yml`);
  a home-level file is ignored entirely (see the pitfall below).

Either way the sibling `cordis.yml` names the bundle(s) the profile composes, and an untouched
patch file is a comment block plus `[]`.

## Row shape

```yaml
- insert:                        # top-level list of patch operations; `insert` adds rows
    - id: mcp-<name>             # unique row id; the `mcp-` prefix matches shipped examples
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: <name>       # [A-Za-z0-9_-]{1,32}; namespace for the tool names
        transport: stdio         # or streamable-http (then `url` + optional `headers`)
        command: /abs/path/to/python
        args: ['-m', 'some_module', 'serve']
```

The model sees that server's tools as `mcp__<serverName>__<rawToolName>` — the same
server-qualified shape Claude Code and Codex use. One row per server; an edit hot-swaps the
connection without a restart. For `streamable-http`, swap `command`/`args` for `url` and optional
`headers`; `env` takes values, and `!!js` expressions are evaluated by dsh itself. The plugin
ships in the checkout at `packages/mcp/mcp-client`; its README is the authoritative field list for
the build you are on.

## Do not let an installer choose the interpreter

`code-review-graph install --platform dsh` (2.4.0) writes the row into the home layer with
`command: uvx` and `args: [code-review-graph, serve]`. On a host that already has a pinned
code-review-graph interpreter — the venv on the Mac, the system python on dockerVM — that is a
**second, unversioned copy** answering the same graphs, and the one the installer picked is not the
one `~/.hermes/config.yaml` runs. Hand-write the row instead, pointing at the pinned interpreter:

```yaml
        command: /Users/ron/.local/venvs/crg/bin/python3     # or /usr/bin/python3 on dockerVM
        args: ['-m', 'code_review_graph', 'serve']
```

## Verify, do not assume

`dsh --profile <name> --dump-config` prints the composed tree without booting, every layer
included. A row that was actually read appears there:

```
dsh --profile headless --dump-config | grep -A6 mcp-client
```

`--dump-config` requires `--profile`; without it the CLI exits `error: --profile <name> is
required`. A second, stronger check: start the profile and watch for the client's own line —
`Starting MCP server 'code-review-graph' with transport 'stdio'` appears at boot when the row is
live. A row in a file that no layer reads produces neither.

## Pitfall: the layer the build does not read

An installer that assumes 0.2.x writes `$DSH_HOME/cordis.patch.yml`. On a 0.1.x build that file is
ignored: no error, no row, the server never starts, and the file on disk looks like proof that it
ran. The mirror-image mistake bites when upgrading — a home-layer row written months ago on a
build that could not read it suddenly goes live on 0.2.x. After any installer touches dsh config,
run the `--dump-config` grep before believing the wiring landed; if the row is missing on 0.1.x,
copy the same `insert` into each profile's `cordis.patch.yml`.

Provider and model defaults are a different layer: `~/.dsh/settings.yaml` carries `llm-*` provider
blocks and `agent-default-model`, not MCP rows.
