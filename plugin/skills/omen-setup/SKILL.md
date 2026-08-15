---
description: Set up or verify the Omen review gate (token, Codex hook trust, doctor)
---

Walk the user through arming the Omen review gate, then PROVE it is
armed. Do these in order:

1. **Doctor first**: run `omen-doctor` (it is on your PATH — shipped in
   this plugin's bin/). Read its output; each ✗ line names its fix.
2. **Token**: if the doctor says no API token is discoverable, the omen
   MCP server is not connected. Tell the user to mint a token at
   https://usefirmware.com/app and either:
   - Claude Code: run `claude mcp add --transport http omen
     https://api-v1.usefirmware.com/mcp --header "Authorization: Bearer
     <token>"` (writes it into ~/.claude.json where the gate hook can
     find it), or
   - export `OMEN_TOKEN=<token>` in their shell profile (works for
     terminal launches; GUI-launched apps do not read shell profiles —
     prefer the config write).
3. **Codex users only**: hooks are off by default and an untrusted hook
   is a SILENT fail-open — the flash just runs, nothing warns anyone.
   Tell the user to (a) add `hooks = true` under `[features]` in
   `~/.codex/config.toml`, (b) run `/hooks` inside Codex and approve
   the Omen gate hook. Do not soften this: until both are done, Codex
   flashes are not gated.
4. **Re-run `omen-doctor`** and show the user the final status. Done
   means every line is ✓ for the harnesses they use.
