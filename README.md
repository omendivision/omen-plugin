# Omen review gate

Blocks a firmware flash until an adversarial review of the change exists,
using your parsed schematic as ground truth.

When your agent **builds**, a reviewer reads the change against your board and
records what it found. When your agent tries to **flash**, the flash is blocked
if that review found something that damages hardware — a half-bridge driven
shoot-through, a pin configured against the net it actually sits on.

The review runs on your machine. Your firmware source never leaves it; only the
findings are sent, so a blocked agent can be told why.

## Install

**Claude Code** — no Python package needed, the plugin ships the hook:

```
/plugin marketplace add omendivision/omen-plugin
/plugin install omen-tools@omen
```

**Codex CLI** — no plugin system, so use the CLI:

```
pipx install omen-tools && omen install
```

`omen install` detects which agents are on the machine and wires each one.
`uv tool install omen-tools` works too if you'd rather not install pipx.

Cursor is not supported: it is the one client with no OAuth dynamic client
registration, so its MCP auth needs a static client we have not stood up.

Both routes need `python3` on your PATH, and an Omen API token — get one at
[usefirmware.com](https://usefirmware.com) and connect the MCP server; the gate
reads the token your MCP config already holds.

Check it is actually armed:

```
omen doctor
```

## Skipping

Bring-up sometimes means flashing something you know is wrong.

```
/omen-skip-review     # 1 hour, then the gate comes back on its own
/omen-enable-review   # turn it back on now
```

`OMEN_SKIP_VERIFY=1 west flash` skips a single command.

If the review service is unreachable the flash is **allowed**, with a loud
warning that it was never reviewed — being offline on a bench or a plane
should not stop you flashing a board. The trade is deliberate: disconnecting
is a bypass, but it is never a silent one.

The skip window expires by itself on purpose — a gate you can turn off
permanently is one you turn off once and never turn back on.

## What it sends

| Sent | Not sent |
| --- | --- |
| SHA-256 of each source file's contents | The file contents |
| Findings the reviewer produced | Your firmware source |
| The command that triggered the gate | |

The server diffs the hashes against your last review to tell the local reviewer
which files changed. It cannot reconstruct the files from them.

## When it does not block

- A build never blocks. One hook serves both build and flash; failing a build
  over a flaky network is worse than anything this gate prevents.
- Only a `blocker` — hardware damage — stops a flash. `major` and `minor`
  findings are reported and let through. A gate that blocks on style is a gate
  that gets uninstalled.

## License

MIT
