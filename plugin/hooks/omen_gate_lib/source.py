"""What changed, without requiring git.

The verdict key has to be computable at both hook sites, on any checkout, in
milliseconds. Keying on `git write-tree` made a version-control system a
hard dependency of flashing a board — and firmware trees are routinely a
folder from a vendor SDK, an unzipped example, or a scratch directory that
was never `git init`ed. "Not a git repo" would then have meant "denied
forever", which is the gate failing in the one direction it must not.

So identity is the same mechanism the schematic layer already uses: sha256
per file, sorted, combined. Git is used when present — `git ls-files` is a
faster and more accurate ignore filter than anything we would write — but it
is an optimisation, never a requirement.

Two consequences worth stating:

  * We detect changes at FILE granularity, not line granularity. The
    reviewer reads changed files in full rather than a unified diff. That is
    the more robust input anyway: a shoot-through bug is often a changed pin
    assignment interacting with an UNCHANGED dead-time constant, and a diff
    contains neither half of that pair.
  * Build output must not be included, or the fingerprint churns every
    compile and no verdict is ever reusable. Ignored directories are
    excluded by name as well as by .gitignore, so a non-git tree behaves
    like a well-configured git one.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

# Extensions worth reviewing. Everything else is noise for a firmware review
# and only adds fingerprint churn.
SOURCE_SUFFIXES = {
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".ino", ".S", ".s",
    ".rs", ".py", ".zig", ".go",
    ".dts", ".dtsi", ".overlay", ".conf", ".kconfig", ".prj",
    ".ld", ".cmake", ".mk", ".ioc", ".toml", ".yaml", ".yml", ".json",
}
SOURCE_NAMES = {"Makefile", "CMakeLists.txt", "Kconfig", "prj.conf",
                "platformio.ini", "sdkconfig", "west.yml"}

# Directories that never contain reviewable source and always contain churn.
IGNORED_DIRS = {
    ".git", ".svn", ".hg", "build", "builds", "_build", "cmake-build-debug",
    "out", "dist", "target", "node_modules", ".venv", "venv", "__pycache__",
    ".pio", ".pytest_cache", ".mypy_cache", ".ruff_cache", "zephyr",
    "modules", "bootloader", ".west", "managed_components",
}

MAX_FILE_BYTES = 2 * 1024 * 1024      # a source file over 2 MB is generated
MAX_FILES = 4000                       # a firmware tree is not a monorepo


def _git_files(root: Path) -> list[Path] | None:
    """Tracked + untracked-but-not-ignored, via git. None when unavailable.

    `-uall` matters: a brand-new `motor_config.h` that has never been
    `git add`ed is in neither the tree nor `git diff`, and missing it would
    keep a stale verdict valid across exactly the change most likely to be
    dangerous.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            cwd=root, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    return [root / n for n in names]


def _walked_files(root: Path) -> list[Path]:
    """Fallback for a directory that is not a git repo."""
    out: list[Path] = []
    stack = [root]
    while stack and len(out) < MAX_FILES:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            hidden_dir = (entry.name.startswith(".")
                          and entry.name != ".vscode" and entry.is_dir())
            if hidden_dir:
                continue
            if entry.is_dir():
                if entry.name not in IGNORED_DIRS:
                    stack.append(entry)
            else:
                out.append(entry)
    return out


def _is_source(path: Path) -> bool:
    if path.name in SOURCE_NAMES:
        return True
    return path.suffix in SOURCE_SUFFIXES


def fingerprint(root: str | Path) -> dict[str, str]:
    """{relative posix path: sha256} over reviewable source files.

    Deterministic, ignores build output, and works with or without git.
    """
    root = Path(root).resolve()
    files = _git_files(root)
    if files is None:
        files = _walked_files(root)

    out: dict[str, str] = {}
    for path in files:
        if not _is_source(path):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in IGNORED_DIRS for part in Path(rel).parts[:-1]):
            continue
        out[rel] = hashlib.sha256(data).hexdigest()
    return out


def source_hash(files: dict[str, str]) -> str:
    """One stable id for a whole source state. Same shape as the schematic
    layer's design identity: sort, join, hash — so identical trees on two
    machines produce the same key and a verdict is reusable across them."""
    joined = "\n".join(f"{p}:{h}" for p, h in sorted(files.items()))
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


def changed(previous: dict[str, str],
            current: dict[str, str]) -> dict[str, list[str]]:
    """What moved between two fingerprints, as paths the reviewer can open.

    File granularity by design (see module docstring): the reviewer reads
    these whole rather than receiving a unified diff, because the dangerous
    interaction is often between a changed line and an unchanged one.
    """
    prev_keys, cur_keys = set(previous), set(current)
    return {
        "added": sorted(cur_keys - prev_keys),
        "removed": sorted(prev_keys - cur_keys),
        "modified": sorted(k for k in prev_keys & cur_keys
                           if previous[k] != current[k]),
    }


def is_empty(change: dict[str, list[str]]) -> bool:
    return not any(change.values())
