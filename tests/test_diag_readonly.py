"""Static safety checks for scripts/diag_readonly.sh.

The diagnostic script is meant to be run on the LIVE trading droplet, so it must
be provably read-only and must never print secrets. These tests scan the script
text for forbidden commands; they do not execute it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diag_readonly.sh"


def _code_lines() -> list[str]:
    """Script lines with blank lines and full-line comments stripped."""
    out = []
    for raw in SCRIPT.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(raw)
    return out


def _strip_quoted(line: str) -> str:
    """Remove single/double quoted substrings so SQL text and echo payloads
    (which legitimately contain `>=`, `>>>`, etc.) don't look like shell syntax."""
    line = re.sub(r"'[^']*'", "", line)
    line = re.sub(r'"[^"]*"', "", line)
    return line


def test_script_exists_and_has_shebang():
    assert SCRIPT.is_file()
    assert SCRIPT.read_text().startswith("#!/usr/bin/env bash")


# Mutating / stateful commands that must never appear (scanned on FULL lines,
# including inside quotes — a write verb in a quoted SQL string is still a write).
FORBIDDEN_PATTERNS = [
    r"\bsystemctl\s+(restart|start|stop|reload|enable|disable|kill)\b",
    r"\bgit\s+(pull|push|commit|reset|checkout|merge|rebase|fetch|clone|clean)\b",
    r"\bpip\s+install\b",
    r"\bapt(-get)?\s+(install|update|upgrade|remove)\b",
    r"\b(rm|mv|cp|chmod|chown|truncate|tee|dd)\b",
    r"\bkill(all)?\b",
    r"\bcurl\b[^\n|]*\|\s*(bash|sh)\b",       # curl | bash
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM)\b",
    r"PRAGMA[^\n\"']*=",                       # PRAGMA that writes
]

# Shell output-redirect patterns (scanned on QUOTE-STRIPPED lines only).
REDIRECT_PATTERNS = [
    r">>",                                     # append redirect to a file
    r"(?<![0-9])>(?!\s*/dev/null)(?!&)",       # write redirect (except >/dev/null, 2>&1)
]

# Secret-exposing commands that must never appear.
SECRET_PATTERNS = [
    r"\bcat\b[^\n]*\.env",
    r"\b(less|more|head|tail|grep|awk|sed)\b[^\n]*\.env",
    r"\bprintenv\b",
    r"(^|\s|;)env(\s|$)",                      # bare `env` dump
    r"get_secret_value",                       # must not appear in the committed probe
    r"\becho\b[^\n]*\$\{?[A-Z_]*(KEY|SECRET|TOKEN)",
]


@pytest.mark.parametrize("pat", FORBIDDEN_PATTERNS)
def test_no_write_or_stateful_commands(pat):
    rx = re.compile(pat)
    hits = [ln for ln in _code_lines() if rx.search(ln)]
    assert not hits, f"forbidden pattern {pat!r} found in diag script: {hits}"


@pytest.mark.parametrize("pat", REDIRECT_PATTERNS)
def test_no_file_output_redirects(pat):
    rx = re.compile(pat)
    hits = [ln for ln in _code_lines() if rx.search(_strip_quoted(ln))]
    assert not hits, f"file-redirect pattern {pat!r} found in diag script: {hits}"


@pytest.mark.parametrize("pat", SECRET_PATTERNS)
def test_no_secret_printing_commands(pat):
    rx = re.compile(pat)
    # The redaction SED= line legitimately mentions key/secret token NAMES; it
    # is the one line allowed to contain those words (it masks them in output).
    hits = [
        ln for ln in _code_lines()
        if rx.search(ln) and not ln.lstrip().startswith("SED=")
    ]
    assert not hits, f"secret-exposing pattern {pat!r} found in diag script: {hits}"


def test_every_sqlite3_invocation_is_readonly():
    """Each sqlite3 CLI call must pass -readonly; the `command -v sqlite3`
    existence probe is exempt, and the venv fallback must open with mode=ro."""
    for ln in _code_lines():
        stripped = _strip_quoted(ln)
        if re.search(r"\bsqlite3\s", stripped) and "command -v sqlite3" not in stripped:
            assert "-readonly" in stripped, f"sqlite3 call without -readonly: {ln!r}"
    text = SCRIPT.read_text()
    if "sqlite3.connect" in text:
        assert "mode=ro" in text, "venv sqlite fallback must use mode=ro"


def test_declares_readonly_intent():
    assert "READ-ONLY" in SCRIPT.read_text()
