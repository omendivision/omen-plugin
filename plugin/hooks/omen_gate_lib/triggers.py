"""Classify a shell command: is it a build, a flash, or neither?

This module is the product's false-positive firewall. It runs on EVERY shell
command the agent issues, so the miss path must be free, and a wrong BLOCK is
the failure that gets the whole gate uninstalled — measured elsewhere at
15-30 minutes to triage one false positive, with two enough to lose the user.

Three rules earned the hard way (adversarial review rounds 1-2):

1. ANCHOR ON THE RESOLVED EXECUTABLE, never a substring. A substring matcher
   blocks `rg burn_efuse firmware/`, `git commit -m "gate espefuse burn_efuse"`
   and `man espefuse` — on day one, in our own repo, whose test fixtures are
   full of those literals.
2. GATE WINS OVER WARM. `idf.py build flash monitor` matches both lists; a
   real flash must never be classified as a build.
3. GATE ON THE VALUE, NOT THE VERB. `-ob RDP=0xBB` is a 30-second recovery;
   `-ob RDP=0xCC` is scrap the board. One nibble apart, same verb.

Everything it cannot resolve — `./flash.sh`, `just flash`, `npm run flash` —
is declared out of coverage rather than guessed at. A gate a wrapper walks
past is worse than no gate if the engineer believes it covers him, so the
boundary is published.
"""

from __future__ import annotations

import re
import shlex

# ---- outcomes ----------------------------------------------------------

NONE = "none"          # not our business — the overwhelmingly common case
WARM = "warm"          # a build: start a review, never block
GATE = "gate"          # a flash: block until a verdict exists
IRREVERSIBLE = "irreversible"   # one-way silicon change: block harder


# ---- the lexicon -------------------------------------------------------
# tool basename -> the argument that makes it a flash. `None` means the tool
# only ever flashes, so its presence is enough.

_FLASH_VERBS: dict[str, set[str] | None] = {
    "west": {"flash"},
    "idf.py": {"flash"},
    "esptool": {"write_flash", "write-flash"},
    "esptool.py": {"write_flash", "write-flash"},
    "probe-rs": {"download", "run"},
    "cargo": {"flash", "embed"},
    "openocd": None,            # gated on the `program` command below
    "jlinkexe": {"loadfile"},
    "jlink": {"loadfile"},
    # tools whose flashiness depends on a VALUE, not a verb word, are
    # gated by _WRITE_PATTERNS below — listing them here with None would
    # classify `nrfjprog --readregs` and `dfu-util -l` as flashes
    "stm32_programmer_cli": set(),
    "nrfjprog": set(),
    "nrfutil": set(),
    "dfu-util": set(),
    "dfu-programmer": {"flash"},
    "avrdude": set(),
    "pyocd": {"flash"},
    "st-flash": {"write"},
    "stm32flash": None,
    "picotool": {"load"},
    "teensy_loader_cli": None,
    "mcumgr": {"upload"},
    "arduino-cli": {"upload"},
    "make": {"flash", "program", "upload", "erase", "burn"},
    "pio": {"upload"},
    "platformio": {"upload"},
}

_BUILD_VERBS: dict[str, set[str] | None] = {
    "west": {"build"},
    "idf.py": {"build"},
    "cargo": {"build"},
    "make": None,               # bare `make` is a build
    "cmake": {"--build"},
    "ninja": None,
    "pio": {"run"},
    "platformio": {"run"},
}

# Irreversible-silicon signatures. Matched on the VALUE, and only when the
# resolved executable is the tool that actually executes it.
_IRREVERSIBLE: list[tuple[str, re.Pattern, str]] = [
    ("stm32_programmer_cli", re.compile(r"(?i)\brdp\s*=\s*0x?cc\b"),
     ("STM32 readout protection Level 2: debug port dead, option bytes "
      "frozen, irreversible by ST's own documentation")),
    ("espefuse", re.compile(r"(?i)\bburn[-_](efuse|key|block)\b"),
     "eFuse bits burn 0->1 only; hardware cannot clear them"),
    ("espefuse.py", re.compile(r"(?i)\bburn[-_](efuse|key|block)\b"),
     "eFuse bits burn 0->1 only; hardware cannot clear them"),
    ("espefuse", re.compile(r"(?i)set[-_]flash[-_]voltage"),
     "burns the flash-rail eFuse; the wrong rail kills the module"),
    ("espefuse.py", re.compile(r"(?i)set[-_]flash[-_]voltage"),
     "burns the flash-rail eFuse; the wrong rail kills the module"),
    ("avrdude", re.compile(r"(?i)-U\s*[lhe]fuse:w:"),
     ("AVR fuse write: clearing SPIEN or setting RSTDISBL ends ISP access; "
      "recovery needs a high-voltage programmer")),
]

# Tools where a read and a write share the executable AND the verb slot.
# Matching the tool alone here is how `espefuse summary`, `dfu-util -l` and
# `nrfjprog --readregs` become false positives — the exact class that gets a
# gate uninstalled in its first afternoon.
_WRITE_PATTERNS: dict[str, re.Pattern] = {
    "stm32_programmer_cli": re.compile(
        r"(?i)(^|\s)(-d|--download|-w|--write)(\s|$)|-ob\s+\S+="),
    "nrfjprog": re.compile(r"(?i)--(program|memwr|qspiprogram)\b"),
    "nrfutil": re.compile(r"(?i)\b(program|protection-set)\b"),
    "dfu-util": re.compile(r"(?i)(^|\s)(-D|--download)(\s|$)"),
    "avrdude": re.compile(r"(?i)-U\s*\w+:w:"),
    "stm32flash": re.compile(r"(?i)(^|\s)-w(\s|$)"),
    "teensy_loader_cli": re.compile(r"\.hex\b"),
}

# `-ob RDP=<anything except 0xAA and 0xCC>` is Level 1 — recoverable, and
# classified by EXCLUSION because enumerating 0xBB misses RDP=0x11.
_RDP_ANY = re.compile(r"(?i)\brdp\s*=\s*(0x)?([0-9a-f]{2})\b")

# Mass-erase / factory-data destroyers: device survives, per-unit data
# (MAC, calibration, certificates, provisioning) does not.
_DESTRUCTIVE = re.compile(
    r"(?i)(--eraseall|--recover|mass[-_]erase|chip[-_]erase|erase[-_]flash"
    r"|--allow-erase-all|:force:unprotect|:mass-erase)")

# Writes below the app partition clobber a bootloader / partition table.
_LOW_OFFSET = re.compile(r"\b0x0*(0|[1-7][0-9a-fA-F]{0,3})\b")


# ---- coverage instrumentation ------------------------------------------
# Deliberately LOOSE. This never decides anything: it exists to answer the
# one question the lexicon cannot answer about itself — what are we missing?
#
# A missed trigger costs one unreviewed flash; a wrong trigger costs the
# product. So coverage is chased by OBSERVING, never by widening the matcher
# and hoping. Anything this flags that `classify` called NONE is a candidate
# gap, reported and then either added to the lexicon with a test, or
# explicitly declared out of coverage.
_LOOKS_DANGEROUS = re.compile(
    r"(?i)\b(flash|program|upload|burn|erase|download|bootload|dfu|"
    r"fuse|efuse|unlock|provision)\b")

# Words that make a dangerous-looking line obviously not a flash. Without
# these the reporter drowns in `git commit -m "flash fix"` and `pip
# download`, and a noisy signal gets ignored exactly like a noisy gate.
_OBVIOUSLY_NOT = re.compile(
    r"(?i)^\s*(git|gh|rg|grep|man|cat|less|echo|sed|awk|ls|find|"
    r"pip|pip3|apt|apt-get|brew|docker|curl|wget|"
    r"pytest|cargo\s+(test|clippy|fmt|doc|check))\b")

# npm/yarn/pnpm are deliberately NOT excluded: `npm run flash` is a real
# wrapper shape, and `npm run build` carries no dangerous word so it never
# reaches here anyway.


def near_miss(command: str) -> str | None:
    """A command that LOOKS device-touching but that we did not gate.

    Reports two cases, and the second is the more dangerous one:

      NONE  we saw nothing at all — a tool or wrapper missing from the
            lexicon, so a flash would proceed unreviewed
      WARM  we called it a build — worse, because the engineer sees the
            gate "working" while a flash slips through classified as a
            compile (`make program-device` was found exactly this way)

    Returns the normalised head (tool + up to two args), never the full
    string: the head is what a lexicon entry gets written from, and it keeps
    paths and filenames out of what we send.
    """
    if not command or _OBVIOUSLY_NOT.search(command):
        return None
    if not _LOOKS_DANGEROUS.search(command):
        return None
    verdict = classify(command)[0]
    if verdict not in (NONE, WARM):
        return None
    for argv in _split_commands(command):
        while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
            argv = argv[1:]
        if not argv:
            continue
        head = [_basename(argv[0])] + [a for a in argv[1:3]
                                       if not a.startswith(("/", "."))]
        joined = " ".join(head)
        if _LOOKS_DANGEROUS.search(joined) or len(argv) > 1:
            return f"{joined[:80]}  [{verdict}]"
    return None


def _basename(word: str) -> str:
    """argv[0] -> comparable tool name, without path or .exe."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def _split_commands(command: str) -> list[list[str]]:
    """One shell string -> the argv of each command in it.

    `make && make flash` is two commands and the second one flashes. A
    matcher that looked at the string as a whole would classify the pair by
    whichever pattern it happened to test first.
    """
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        return []                      # unbalanced quotes: unparseable
    out: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word in ("&&", "||", ";", "|", "&"):
            if current:
                out.append(current)
            current = []
        else:
            current.append(word)
    if current:
        out.append(current)
    return out


def _verb_hit(tool: str, args: list[str],
              table: dict[str, set[str] | None]) -> bool:
    if tool not in table:
        return False
    verbs = table[tool]
    if verbs is None:
        return True
    if not verbs:                       # value-gated: ask _WRITE_PATTERNS
        pattern = _WRITE_PATTERNS.get(tool)
        return bool(pattern and pattern.search(" ".join(args)))
    return any(a.lower() in verbs for a in args)


def classify(command: str) -> tuple[str, str | None]:
    """(outcome, reason). `reason` is populated for IRREVERSIBLE only.

    Never raises: an unparseable command is NONE, because refusing to run
    what we cannot read would block the user's shell over our own bug.
    """
    best = NONE
    reason: str | None = None

    for argv in _split_commands(command):
        if not argv:
            continue
        # `FOO=bar west flash` — step past leading environment assignments
        while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
            argv = argv[1:]
        if not argv:
            continue
        tool = _basename(argv[0])
        args = argv[1:]
        # `python -m esptool …`, `uv run west …`: step past the runner
        while tool in ("python", "python3", "uv", "uvx", "poetry", "pipx",
                       "npx", "bunx") and args:
            if args[0] in ("-m", "run", "exec"):
                args = args[1:]
                continue
            tool, args = _basename(args[0]), args[1:]
            break
        joined = " ".join(args)

        for want_tool, pattern, why in _IRREVERSIBLE:
            if tool == want_tool and pattern.search(joined):
                return IRREVERSIBLE, why

        if tool == "stm32_programmer_cli" and "-ob" in [a.lower() for a in args]:
            m = _RDP_ANY.search(joined)
            if m and m.group(2).lower() == "aa":
                return IRREVERSIBLE, ("RDP regression to Level 0 mass-erases "
                                      "flash: firmware and per-unit data gone")

        if _verb_hit(tool, args, _FLASH_VERBS):
            if tool == "openocd" and "program" not in joined:
                pass
            else:
                if _DESTRUCTIVE.search(joined):
                    return IRREVERSIBLE, ("mass erase: the device survives, "
                                          "per-unit factory data does not")
                if tool in ("esptool", "esptool.py") and _LOW_OFFSET.search(joined):
                    return IRREVERSIBLE, ("write below the app partition: "
                                          "overwrites bootloader or partition "
                                          "table")
                best = GATE
                continue

        # a destructive verb on a flashing tool, without a write verb
        if tool in _FLASH_VERBS and _DESTRUCTIVE.search(joined):
            return IRREVERSIBLE, ("mass erase: the device survives, per-unit "
                                  "factory data does not")

        if best != GATE and _verb_hit(tool, args, _BUILD_VERBS):
            best = WARM

    return best, reason
