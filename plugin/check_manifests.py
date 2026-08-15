#!/usr/bin/env python3
"""P9 guard: .claude-plugin and .codex-plugin manifests must agree —
Codex reads BOTH file names, so drift means two live identities."""
import json
import pathlib
import sys

root = pathlib.Path(__file__).parent
a = json.loads((root / ".claude-plugin/plugin.json").read_text())
b = json.loads((root / ".codex-plugin/plugin.json").read_text())
bad = [k for k in ("name", "version", "description") if a.get(k) != b.get(k)]
if bad:
    sys.exit(f"manifest drift on {bad}: claude={a} codex={b}")
print(f"manifests in sync: {a['name']} {a['version']}")
