#!/usr/bin/env python3
"""Shared env-var resolver — populates os.environ from ~/.zshrc etc.

Why this exists: Claude Code (and many automated runners) invoke scripts
through a *non-interactive* shell that does NOT source `~/.zshrc`. Any
env var the user exported there is invisible inside the Python process.
This module fills the gap by reading the same files an interactive shell
would have read, in priority order:

  1. `os.environ` — already loaded, no-op.
  2. `launchctl getenv VAR` on macOS — the persistent user-env store.
  3. `export VAR=...` lines in `~/.zshenv`, `~/.zshrc`, `~/.zprofile`,
     `~/.bash_profile`, `~/.bashrc` (best-effort, first hit wins).

The resolved value is written back into `os.environ` so downstream code
reads it via the normal path. A log line records which source supplied
each value, useful when debugging "why didn't it pick up my API key".

History: this logic originally lived inside `save_to_zotero.py` as a
Zotero-only helper (`_load_zotero_env_from_user_shell`). It was extracted
here in 2026-05 because two more scripts wanted the same behaviour —
`fetch_fulltext.py` for `UNPAYWALL_EMAIL` and `search_pubmed.py` for
`NCBI_API_KEY`. DEFERRED.md item #2.
"""
from __future__ import annotations

import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

# Searched in this order; first non-empty match wins per variable.
_DEFAULT_RC_FILES: tuple[str, ...] = (
    "~/.zshenv",
    "~/.zshrc",
    "~/.zprofile",
    "~/.bash_profile",
    "~/.bashrc",
)


def _parse_export_from_rc(path: str, var: str) -> str:
    """Return the value of an `export VAR=...` line from a shell rc file.

    Tolerates double-quoted, single-quoted, and bare values. Returns "" if
    the file can't be opened or the variable isn't present.
    """
    try:
        with open(os.path.expanduser(path), "r",
                  encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    pattern = re.compile(
        rf'^\s*(?:export\s+)?{re.escape(var)}='
        r'("([^"]*)"|\'([^\']*)\'|(\S+))\s*(?:#.*)?$',
        re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return ""
    return (m.group(2) or m.group(3) or m.group(4) or "").strip()


def _read_launchctl_var(var: str) -> str:
    """Read a persistent macOS env var via `launchctl getenv`.

    Returns "" on non-macOS, missing var, or any subprocess failure.
    """
    if sys.platform != "darwin":
        return ""
    try:
        import subprocess
        result = subprocess.run(
            ["launchctl", "getenv", var],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def load_env_from_user_shell(
    var_names: tuple[str, ...],
    rc_files: tuple[str, ...] = _DEFAULT_RC_FILES,
) -> None:
    """Populate `os.environ[VAR]` from ~/.zshrc etc. for each `VAR` missing.

    Resolution order per variable:
      1. `os.environ` already has a non-empty value → skip.
      2. `launchctl getenv VAR` on macOS → use that.
      3. `export VAR=...` in any of `rc_files` (first hit wins) → use that.

    No-op if a variable isn't found in any source.

    Logs (at INFO) which source supplied each resolved value so the user
    can audit credential origins.
    """
    for var in var_names:
        if os.environ.get(var, "").strip():
            continue
        value = _read_launchctl_var(var)
        source = "launchctl"
        if not value:
            for rc in rc_files:
                value = _parse_export_from_rc(rc, var)
                if value:
                    source = rc
                    break
        if value:
            os.environ[var] = value
            logger.info("Loaded %s from %s", var, source)
