#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared JSON schemas for paper-analyze artifacts.

Why this exists: the pipeline passes JSON files between independent
scripts (`fetch_fulltext.py` → `generate_note.py`) and between Python
and the orchestrating agent (PubMed MCP path in SKILL.md Step 4.2A
writes a fulltext.json by hand). Without a declared schema, additions
and renames on either side would silently break downstream consumers —
the Phase H audit identified this as Bug #4 (schema drift).

The dataclass below is the single source of truth for the fulltext.json
shape. Both `fetch_fulltext.py` and any orchestrator-emitted file must
match it. `load_fulltext()` validates on read and raises ValueError on
mismatch, per the project's *"sanity checks must fail loud"* discipline.

Schema versioning policy:
- Bump `FULLTEXT_SCHEMA_VERSION` whenever the shape changes
  incompatibly (field renamed/removed, semantics shifted).
- Adding new optional fields with safe defaults does NOT require a
  version bump.
- `generate_note.py` reads through `load_fulltext()` and refuses files
  whose version doesn't match the running code's expectation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional

FULLTEXT_SCHEMA_VERSION = 1


@dataclass
class Fulltext:
    """Canonical shape for `fulltext.json`.

    Source labels (string values for `source`) — keep this list in sync
    with the labels emitted by `fetch_fulltext.py`'s `_try_*` functions
    and the PubMed MCP branch in `start-my-day/SKILL.md` Step 4.2A:

      "user-pdf"
      "drop-folder/metadata-doi"
      "drop-folder/metadata-pmid"
      "drop-folder/filename-match"
      "pmc-oa-xml"
      "europepmc-xml"
      "unpaywall-pdf"
      "unpaywall-bmc-springer"
      "unpaywall-landing"
      "unpaywall-pdf-meta"
      "unpaywall-bmc-springer-meta"
      "unpaywall-landing-meta"
      "publisher_pattern:plos:<journal>"
      "publisher_pattern:elife"
      "publisher_pattern:elife-rp"
      "publisher_pattern:mdpi"
      "publisher_pattern:frontiers"
      "doi-landing"
      "playwright"
      "biorxiv"
      "pubmed_mcp"
    """
    pmid: str = ""
    doi: str = ""
    source: str = ""
    pdf_path: Optional[str] = None  # None for text-only sources
    text: str = ""                  # required, non-empty
    abstract: str = ""
    fetched_from: str = ""
    sources_tried: List[str] = field(default_factory=list)
    schema_version: int = FULLTEXT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def load_fulltext(path: str) -> Fulltext:
    """Read + validate a `fulltext.json` file.

    Raises:
        FileNotFoundError: if `path` doesn't exist.
        ValueError: if schema_version mismatches or `text` is empty.
        json.JSONDecodeError: if the file isn't valid JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    v = d.get("schema_version")
    if v != FULLTEXT_SCHEMA_VERSION:
        raise ValueError(
            f"fulltext.json schema_version {v!r} != expected "
            f"{FULLTEXT_SCHEMA_VERSION}. "
            f"Re-run fetch_fulltext.py to regenerate, or upgrade "
            f"generate_note.py to consume the new version."
        )
    if not (d.get("text") or "").strip():
        raise ValueError(
            f"fulltext.json has empty `text` field: {path}. "
            f"This shouldn't happen — fetch_fulltext.py only writes "
            f"on non-empty extraction."
        )
    known = {k: d[k] for k in Fulltext.__dataclass_fields__ if k in d}
    return Fulltext(**known)
