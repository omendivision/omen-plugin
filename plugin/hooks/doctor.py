#!/usr/bin/env python3
"""omen-doctor — is the gate actually ARMED, in the harness that will
use it? Stdlib only.

The design lesson this encodes (adversarial review P1): a doctor that
only self-runs the hook script certifies an unarmed gate. On Codex an
installed-but-untrusted hook is a SILENT fail-open — same config, no
message, the flash sails through. So this doctor checks the states the
harness consults, not just the script:

  1. python3 resolvable on a STRIPPED path (the GUI-launch context)
  2. the hook script itself denies a fake `west flash` (exit 2)
  3. an API token is discoverable (the gate denies everything without
     one, which is loud but useless)
  4. Claude: the plugin is enabled
  5. Codex: [features] hooks = true AND our hook appears trusted —
     absent either, flashes are NOT gated there and nothing will say so
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TICK, CROSS, DOT = "✓", "✗", "·"
failures = 0


def say(ok: bool | None, msg: str) -> None:
    global failures
    mark = DOT if ok is None else (TICK if ok else CROSS)
    if ok is False:
        failures += 1
    print(f"{mark} {msg}")


def main() -> int:
    home = Path(os.environ.get("HOME") or Path.home())

    # 1. python3 in a GUI-like PATH
    probe = subprocess.run(
        ["sh", "-c", "command -v python3 || command -v python || "
                     "echo /usr/bin/python3"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    py = probe.stdout.strip().splitlines()[-1] if probe.stdout else ""
    say(Path(py).exists(), f"python3 resolves on a minimal PATH: {py or '?'}")

    # 2. the hook script blocks a flash
    event = json.dumps({"tool_name": "Bash",
                        "tool_input": {"command": "west flash"}})
    try:
        proc = subprocess.run(
            [py or "python3", str(HERE / "omen_gate.py")], input=event,
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": str(home),
                 "OMEN_SELFTEST": "1"})
        say(proc.returncode == 2,
            f"hook script blocks `west flash` (exit {proc.returncode})")
    except Exception as exc:  # noqa: BLE001
        say(False, f"hook script failed to run: {exc}")

    # 3. token discovery (same three paths the hook reads)
    sys.path.insert(0, str(HERE))
    try:
        from omen_gate import _token
        tok = _token()
    except Exception:  # noqa: BLE001
        tok = None
    say(bool(tok), "API token discoverable"
        + ("" if tok else " — connect the omen MCP (see /omen-setup); "
                          "until then every flash is denied"))

    # 4. Claude: plugin enabled?
    claude_cfg = home / ".claude.json"
    enabled = None
    for cfg in (claude_cfg, home / ".claude" / "settings.json"):
        try:
            text = cfg.read_text()
        except OSError:
            continue
        if "omen-tools" in text:
            enabled = True
            break
    say(enabled, "Claude Code: omen-tools plugin referenced in config"
        if enabled else "Claude Code: plugin not found in config — "
        "/plugin install omen-tools (skip if you only use Codex)")

    # 5. Codex: features.hooks + trust — THE silent fail-open
    codex_cfg = home / ".codex" / "config.toml"
    try:
        toml_text = codex_cfg.read_text()
    except OSError:
        say(None, "Codex: not installed on this machine (skipping)")
        print()
        return 1 if failures else 0

    hooks_on = False
    in_features = False
    for line in toml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_features = stripped == "[features]"
        elif in_features and stripped.replace(" ", "").startswith(
                "hooks=true"):
            hooks_on = True
    say(hooks_on, "Codex: [features] hooks = true"
        if hooks_on else "Codex: hooks are DISABLED — add `hooks = true` "
        "under [features] in ~/.codex/config.toml; flashes are NOT "
        "gated in Codex until you do")

    trusted = "omen_gate" in toml_text and "trust" in toml_text.lower()
    say(trusted if hooks_on else None,
        "Codex: omen hook appears trusted"
        if trusted else "Codex: cannot confirm the omen hook is trusted "
        "— run /hooks inside Codex and approve it; an untrusted hook is "
        "a SILENT fail-open (the flash just runs, nothing warns you)")

    print()
    if failures:
        print(f"{CROSS} {failures} problem(s) — the gate is NOT fully "
              f"armed. Fix the lines above.")
        return 1
    print(f"{TICK} the gate is armed in every harness checked above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
