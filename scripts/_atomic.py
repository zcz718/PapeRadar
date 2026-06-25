#!/usr/bin/env python3
"""Atomic file writes — temp-then-rename to avoid partial-write artefacts.

The motivation comes from two concurrent-run race conditions identified
in DEFERRED.md item #4:

1. `search_papers.py` writes `arxiv_filtered.json` with a plain
   `open(...).write(json.dumps(...))`. Two `/paperadar` runs in
   parallel can corrupt each other's output mid-write — one process
   reads a half-flushed file the other process is still writing.

2. `fetch_fulltext.py` writes downloaded PDFs to predictable filenames
   (`<label>_<safe_doi>.pdf`). Two concurrent fetches of the same paper
   would clobber each other.

The standard fix in both cases is a temp-then-rename pattern: write to
a uniquely-named temp file, then atomically rename to the final path.
On POSIX (`os.replace`) the rename is atomic relative to other readers
on the same filesystem.

This module provides the high-level helper for JSON output. PDF tmpfile
naming uses `tempfile.mkstemp` directly at the call sites for simplicity.

Note: the `tests/conftest.py` autouse fixture (which redirects
`tempfile.gettempdir()` per-test) still applies as belt-and-braces
against the test-clobbers-production-cache class of bug.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Union


def atomic_write_json(
    obj: Any,
    path: Union[str, Path],
    indent: int = 2,
    default: Any = str,
) -> None:
    """Write `obj` as JSON to `path`, atomically.

    Strategy:
      1. Write the JSON to `<path>.tmp.<uuid8>` (unique per call).
      2. `os.replace(tmp, path)` — atomic rename on POSIX.

    Other readers of `path` either see the old version or the new
    version; they never see a half-written file. Crash between step 1
    and step 2 leaves an orphan tmp file (caller can sweep `*.tmp.*`
    if they want) but does not corrupt `path` itself.

    Encoding: utf-8 with `ensure_ascii=False` (matches the existing
    `json.dumps` usage in search_papers.py — preserves non-ASCII paper
    titles intact rather than escaping them).

    `default=str` is the historic default in this repo so datetime
    objects in arxiv_filtered.json serialise as ISO strings rather
    than raising TypeError. Pass `default=None` if you want strict
    JSON-serialisable-only behaviour.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent, ensure_ascii=False,
                      default=default)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup — don't propagate a cleanup error over the
        # original one.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
