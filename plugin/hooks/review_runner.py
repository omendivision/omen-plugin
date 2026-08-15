#!/usr/bin/env python3
"""The adversarial reviewer — spawned detached by the hook, never by the
main agent.

A reviewer briefed by the implementer inherits the implementer's blind
spots; that is the whole reason this runs from the hook with its own prompt
and a fresh context, seeing the changed files and the oracle and nothing
else. It runs as a headless instance of whichever agent CLI is installed, so
the engineer's source never leaves the machine while the ground truth (the
parsed netlist, the datasheet evidence) is fetched over MCP.

EVERY exit path reports. A reviewer that dies without saying so leaves the
gate denying against a live lease with nothing behind it, while the UI shows
a progress indicator — a permanent block that looks like patience.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request

API_BASE = os.environ.get("OMEN_API_BASE", "https://api-v1.usefirmware.com")
TOKEN = os.environ.get("OMEN_TOKEN", "")
RUN_ID = os.environ.get("OMEN_RUN_ID", "")
DESIGN_ID = os.environ.get("OMEN_DESIGN_ID") or None
CHANGED = json.loads(os.environ.get("OMEN_CHANGED") or "[]")
REVIEW_TIMEOUT_S = int(os.environ.get("OMEN_REVIEW_TIMEOUT", "180"))

# Ordered by how well they carry a one-shot prompt with MCP access.
# The reviewer is headless, so nobody is there to approve a tool call.
# Without an explicit allow-list the MCP calls come back permission-denied
# and the review proceeds WITHOUT the oracle — producing a confident
# plausibility review wearing an oracle's credentials. Caught by the eval
# harness, which is the only reason we know it was happening.
_OMEN_TOOLS = ["mcp__omen__get_schematic", "mcp__omen__query",
               "mcp__omen__resolve", "Read", "Grep", "Glob"]

RUNNERS = [
    ("claude", ["claude", "-p", "--output-format", "text",
                "--allowedTools", *_OMEN_TOOLS]),
    # codex exec is OUT until headless MCP-tool access is proven: it has
    # no --allowedTools equivalent (verified against codex 0.146), so a
    # codex-run review silently loses the oracle — the exact confident-
    # plausibility failure the allow-list exists to prevent.
]

PROMPT = """You are reviewing a firmware change that is about to be flashed
to real hardware in the next few seconds. Assume the code is WRONG and try
to prove it. You are not here to endorse.

STAKES: a wrong pin mapping or a bad timing constant destroys the board on
the first PWM cycle. There is no stack trace and no undo.

GROUND TRUTH — use it, do not guess:
- Call the omen MCP tool get_schematic("{design_id}") for the parsed
  netlist: every net, which refdes and pin each net touches, pin names and
  electrical types. "Which MCU pin drives INH" is a LOOKUP in that model.
- Call omen resolve/query for datasheet evidence about any part you reason
  about (pin functions, absolute maximums, timing requirements).

FILES THAT CHANGED (read them in full; the dangerous interaction is often
between a changed line and an unchanged one in the same file):
{files}

REPORT RULES:
- Every finding must cite something checkable: a net, a refdes, a pin, or a
  file:line. A finding you cannot anchor is a hypothesis — drop it.
- Severity: `blocker` = destroys hardware or bricks silicon. `major` =
  wrong behaviour on real hardware. `minor` = everything else.
  ONLY `blocker` stops the flash, so use it precisely.
- State the OBSERVABLE you used, never the category you inferred.
  "This is a gate driver so this is risky" is not a finding.
  "motor.c:41 drives PA8 during the low-side window; the netlist has
  PA8 -> U1.2 = INH, the high-side input" is.
- Inability to find a relevant net is NEVER a pass. Say what you could not
  determine.
- If the change touches nothing that can reach hardware, say so and pass.

OUTPUT: a single JSON object, nothing else:
{{"findings": [{{"severity": "blocker|major|minor", "claim": "...",
"evidence": "...", "file": "...", "line": 0, "net": "...",
"refdes": "...", "pin": "..."}}]}}
"""


DRY_RUN = bool(os.environ.get("OMEN_REVIEW_DRYRUN"))


def post(path: str, payload: dict) -> None:
    """In dry-run the verdict goes to stdout instead of the server. Used by
    the eval harness so a scoring run exercises the SHIPPED prompt and code
    path without minting leases or polluting the verdict store."""
    if DRY_RUN:
        print(json.dumps({"path": path, **payload}))
        return
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception:
        pass


def report_failure(reason: str) -> None:
    """The reviewer's own deny(). Releases the lease so the next trigger
    denies with the truth rather than a spinner."""
    post(f"/review/runs/{RUN_ID}/failed", {"reason": reason})
    sys.exit(0)


_BIN_DIRS = ["~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin",
             "~/.claude/local", "/usr/bin"]


def _find_binary(name: str) -> str | None:
    """shutil.which PLUS the places GUI-trimmed PATHs miss — a hook
    fired from a desktop-launched harness inherits a minimal PATH, and
    a reviewer that cannot find `claude` releases the lease and the
    flash proceeds unreviewed (adversarial review P6)."""
    found = shutil.which(name)
    if found:
        return found
    import os
    for d in _BIN_DIRS:
        cand = os.path.expanduser(f"{d}/{name}")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def pick_runner() -> tuple[str, list[str]] | None:
    for name, argv in RUNNERS:
        binary = _find_binary(argv[0])
        if binary:
            return name, [binary, *argv[1:]]
    return None


def extract_json(text: str) -> dict | None:
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    blob = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
                    continue
                if isinstance(blob, dict) and "findings" in blob:
                    return blob
                start = None
    return None


def main() -> None:
    if not DRY_RUN and not (RUN_ID and TOKEN):
        return
    chosen = pick_runner()
    if chosen is None:
        report_failure("no agent CLI found on PATH (claude/codex)")
    name, argv = chosen

    prompt = PROMPT.format(
        design_id=DESIGN_ID or "<no design synced — say so and review the "
                               "code on its own terms>",
        files="\n".join(f"  {p}" for p in CHANGED[:60]) or "  (none)")

    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True,
                              text=True, timeout=REVIEW_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        report_failure(f"reviewer ({name}) timed out after {REVIEW_TIMEOUT_S}s")
    except Exception as exc:
        report_failure(f"reviewer ({name}) failed to start: {exc}")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-200:].strip()
        report_failure(f"reviewer ({name}) exited {proc.returncode}: {tail}")

    blob = extract_json(proc.stdout or "")
    if blob is None:
        report_failure(f"reviewer ({name}) returned no parsable verdict")

    findings = [f for f in blob.get("findings", []) if isinstance(f, dict)]
    post("/review/verdict", {"run_id": RUN_ID, "findings": findings,
                             "model": name})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:      # never leave a lease dangling
        report_failure(f"reviewer crashed: {type(exc).__name__}: {exc}")
