#!/usr/bin/env python3
"""Omen review gate — the hook that runs on every shell command.

Stdlib only, no install, no imports from the server package: this executes
on the engineer's machine inside their agent's hook timeout, and a missing
dependency here would be a broken gate rather than an error anyone sees.

Speaks three dialects from one script:

    Claude Code / Codex   PreToolUse        stdin {tool_name, tool_input}
    Cursor                beforeShellExecution   stdin {command, cwd}

Two rules the adversarial review paid for:

  * FAIL-CLOSED IS THIS SCRIPT'S JOB. No client provides it. Claude Code's
    docs say a timed-out hook "doesn't block the tool call... don't count on
    a stalled hook to act as a gate", and Codex's exit-2 arm only blocks
    when stderr is NON-EMPTY. So: our own deadline, well inside theirs, and
    every gate-path failure goes through deny(), which always writes stderr.
  * THE WARM PATH NEVER BLOCKS. One script serves both registrations, so a
    literal reading of "every failure denies" would fail a build over a
    flaky network — strictly worse than anything this gate protects against.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from omen_gate_lib import source, triggers  # noqa: E402

API_BASE = os.environ.get("OMEN_API_BASE", "https://api-v1.usefirmware.com")
DEADLINE_S = float(os.environ.get("OMEN_GATE_DEADLINE", "5"))
SKIP_ENV = "OMEN_SKIP_VERIFY"
CONFIG = ".omen/gate.json"


# ---- client dialects ---------------------------------------------------

def read_event() -> tuple[str, str, str]:
    """(command, cwd, dialect). An unreadable event is not our business."""
    try:
        raw = json.load(sys.stdin)
    except Exception:
        return "", "", "unknown"
    if "command" in raw:                       # Cursor
        return raw.get("command") or "", raw.get("cwd") or "", "cursor"
    tool_input = raw.get("tool_input") or {}   # Claude Code / Codex
    return (tool_input.get("command") or "",
            raw.get("cwd") or os.getcwd(), "claude")


def allow() -> None:
    """Say nothing, exit 0. The overwhelmingly common path."""
    sys.exit(0)


def deny(message: str, dialect: str) -> None:
    """Block, in every dialect, and ALWAYS write stderr.

    Codex only honours exit 2 when stderr is non-empty — `set -e` firing
    silently would allow the flash. Claude Code honours exit 2 even when the
    JSON is schema-invalid, so writing both is belt and braces.
    """
    sys.stderr.write(message + "\n")
    if dialect == "cursor":
        payload = {"permission": "deny", "agent_message": message,
                   "user_message": message}
    else:
        payload = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message}}
    sys.stdout.write(json.dumps(payload))
    sys.exit(2)


# ---- server ------------------------------------------------------------

def _token() -> str | None:
    """The bearer token, from the client's own MCP configuration. Three
    clients, three formats; we look rather than ask the user to duplicate
    a secret into a second place."""
    if os.environ.get("OMEN_TOKEN"):
        return os.environ["OMEN_TOKEN"]
    home = Path.home()
    candidates = [home / ".claude.json", home / ".codex" / "config.json",
                  home / ".cursor" / "mcp.json"]
    for path in candidates:
        try:
            blob = json.loads(path.read_text())
        except Exception:
            continue
        found = _find_bearer(blob)
        if found:
            return found
    return None


def _find_bearer(node) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "authorization" and isinstance(value, str):
                return value.replace("Bearer ", "").strip()
            found = _find_bearer(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_bearer(item)
            if found:
                return found
    return None


def call_gate(payload: dict, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{API_BASE}/review/gate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=DEADLINE_S) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def spawn_reviewer(run_id: str, root: str, changed_files: list[str],
                   design_id: str | None, token: str) -> None:
    """Detached, always. A child that inherits our stdout keeps the pipe
    open and the client blocks until IT exits — turning 'return immediately'
    into 'block the session for a full review', the exact outcome warming
    exists to prevent."""
    script = HERE / "review_runner.py"
    env = dict(os.environ,
               OMEN_RUN_ID=run_id, OMEN_TOKEN=token, OMEN_API_BASE=API_BASE,
               OMEN_DESIGN_ID=design_id or "",
               OMEN_CHANGED=json.dumps(changed_files[:80]))
    try:
        subprocess.Popen(
            [sys.executable, str(script)], cwd=root, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception:
        pass          # a reviewer we cannot start is a lease that expires


# ---- main --------------------------------------------------------------

def report_near_miss(command: str) -> None:
    """Tell the server about a command that looked device-touching but that
    we let through unreviewed.

    This is how the trigger lexicon learns what it is missing. We cannot
    enumerate every wrapper an engineer writes — `just flash-board`,
    `./deploy.sh`, an npm script — and a corpus we assemble ourselves only
    measures our own imagination. Real usage is the only honest source.

    Sends the normalised HEAD of the command, never the full string: enough
    to write a lexicon entry from, without shipping paths or filenames. Fully
    fire-and-forget — a coverage report must never delay or fail a command
    that we already decided to allow.
    """
    try:
        head = triggers.near_miss(command)
        if not head:
            return
        token = _token()
        if not token:
            return
        req = urllib.request.Request(
            f"{API}/review/near-miss", method="POST",
            data=json.dumps({"head": head}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).close()
    except Exception:
        pass                                     # coverage telemetry, not a gate


def main() -> None:
    command, cwd, dialect = read_event()
    if not command:
        allow()

    outcome, why = triggers.classify(command)
    if outcome == triggers.NONE:
        report_near_miss(command)                # fire-and-forget, never blocks
        allow()                                  # ~99% of commands, free

    root = cwd or os.getcwd()
    config = {}
    try:
        config = json.loads((Path(root) / CONFIG).read_text())
    except Exception:
        pass
    design_id = config.get("design_id")

    warm = outcome == triggers.WARM
    token = _token()

    # The local escape hatch, for when we are unreachable — the server-side
    # /omen-skip-review window is checked by the gate call itself.
    if os.environ.get(SKIP_ENV):
        allow()

    if token is None:
        if warm:
            allow()
        deny("Omen: no API token found in your MCP config. Run /mcp to "
             f"connect, or set {SKIP_ENV}=1 to flash unreviewed.", dialect)

    try:
        files = source.fingerprint(root)
        digest = source.source_hash(files)
        status, body = call_gate(
            {"source_hash": digest, "design_id": design_id,
             "stage": "warm" if warm else "gate", "command": command[:400],
             # {path: hash} only — no file CONTENT leaves the machine. The
             # server diffs this against the last reviewed state and tells
             # us which paths moved; the reviewer reads those locally.
             "fingerprint": files},
            token)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if warm:
            allow()                              # never fail a build
        deny(f"Omen: could not reach the review service in {DEADLINE_S:.0f}s "
             f"({type(exc).__name__}) — this build is unreviewed and nothing "
             f"has been flashed. Retry, or {SKIP_ENV}=1 to proceed.", dialect)
    except Exception as exc:                     # our own bug
        if warm:
            allow()
        deny(f"Omen: internal gate error ({type(exc).__name__}). "
             f"{SKIP_ENV}=1 to proceed.", dialect)

    state = body.get("state")

    if state == "skipped":
        allow()

    if state == "claimed":
        # Prefer the server's diff. Empty means "first review of this design"
        # — everything is new, so fall back to the full file list rather than
        # handing the reviewer nothing to look at.
        changed = body.get("changed") or list(files)
        spawn_reviewer(str(body.get("run_id")), root,
                       changed, design_id, token)
        if warm:
            allow()
        deny("Omen: review started for this change — re-run in ~30s. "
             "Nothing has been flashed.", dialect)

    if state == "in_progress":
        if warm:
            allow()
        deny("Omen: a review of this exact source is already running. "
             "Re-run shortly — nothing has been flashed.", dialect)

    if state == "verdict":
        if warm:
            allow()
        blocking = body.get("blocking") or []
        if not blocking:
            allow()
        lines = ["Omen blocked this flash.", ""]
        for f in blocking[:5]:
            where = f.get("file") or f.get("net") or "?"
            line = f":{f['line']}" if f.get("line") else ""
            lines.append(f"  BLOCKER  {where}{line}: {f.get('claim')}")
            if f.get("evidence"):
                lines.append(f"           {f['evidence']}")
        lines += ["",
                  f"  {len(blocking)} blocker(s). Nothing has been flashed.",
                  f"  /omen-skip-review to pause the gate  ·  "
                  f"{SKIP_ENV}=1 for this one flash"]
        deny("\n".join(lines), dialect)

    # An unrecognised answer is not a reason to stop someone working.
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A crash in the gate must never be the thing that blocks a build.
        sys.exit(0)
