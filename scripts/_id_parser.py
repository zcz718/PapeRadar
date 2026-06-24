#!/usr/bin/env python3
"""Centralized paper-ID parsing (PMID / arXiv / DOI).

Before this module existed (2026-05), each script in `scripts/` had its
own slightly-different version of "is this a PMID? a DOI? an arXiv ID?".
That meant a fix to the regex (e.g. accepting lowercase `pmid:`) only
propagated to one caller and the rest kept the old behaviour. DEFERRED.md
item #3.

Single source of truth for:
- `parse_paper_id(s)` — return `(pmid_or_None, doi_or_None)`.
- `strip_pmid_prefix(s)` — `"PMID:42098827" -> "42098827"`.
- `format_pmid(s)` — `"42098827" -> "PMID:42098827"`.
- `parse_arxiv_id(s)` — `"arXiv:2501.12345v2" -> "2501.12345"`.

The function shapes are deliberately tolerant: empty / None / garbage
inputs return `None` (for the *_id parsers) or pass-through unchanged
(for the prefix mutators). Callers can rely on never having to
defensively check for None before calling these.

History: `parse_paper_id` is lifted from `fetch_fulltext.py:_parse_paper_id`,
which was the cleanest of the four pre-existing versions.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

_PMID_PREFIX_RE = re.compile(r"^\s*pmid\s*:\s*", re.IGNORECASE)
_PMID_BARE_RE = re.compile(r"^\d{6,9}$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/")
_ARXIV_PREFIX_RE = re.compile(r"^\s*arxiv\s*:\s*", re.IGNORECASE)
_ARXIV_VERSION_SUFFIX_RE = re.compile(r"v\d+$", re.IGNORECASE)


def parse_paper_id(s: str) -> Tuple[Optional[str], Optional[str]]:
    """Return `(pmid, doi)` given a paper-ID string of any common form.

    Recognised inputs:
      - `"PMID:42098827"` / `"pmid:42098827"` → `("42098827", None)`
      - bare 6–9 digit string `"42098827"` → `("42098827", None)`
      - DOI `"10.1186/s13059-026-04096-w"` → `(None, "10.1186/...")`
      - garbage / empty → `(None, None)`

    Note: arXiv IDs are NOT returned via this function (they're neither
    PMID nor DOI). Use `parse_arxiv_id` separately when you need them.
    """
    pid = (s or "").strip()
    if not pid:
        return (None, None)
    if _PMID_PREFIX_RE.match(pid):
        # A bare "PMID:" with no digits must collapse to (None, None), not
        # ("", None) — empty strings violate the documented contract and
        # could slip past truthy guards as a falsy-but-not-None value.
        num = _PMID_PREFIX_RE.sub("", pid).strip()
        return (num or None, None)
    if _PMID_BARE_RE.match(pid):
        return (pid, None)
    if _DOI_RE.match(pid):
        return (None, pid)
    return (None, None)


def strip_pmid_prefix(s: str) -> str:
    """`"PMID:42098827" -> "42098827"`. Pass-through if no PMID prefix.

    Case-insensitive; tolerates whitespace around the colon.
    """
    if not s:
        return ""
    return _PMID_PREFIX_RE.sub("", s.strip())


def format_pmid(pmid: str) -> str:
    """`"42098827" -> "PMID:42098827"`. Idempotent — already-prefixed
    values pass through with normalisation (case + whitespace).
    """
    if not pmid:
        return ""
    return f"PMID:{strip_pmid_prefix(pmid)}"


def parse_arxiv_id(s: str) -> Optional[str]:
    """Return the arXiv ID (no prefix, no version suffix) or None.

    Examples:
      - `"arXiv:2501.12345"` → `"2501.12345"`
      - `"2501.12345v2"` → `"2501.12345"`
      - `"arxiv:2501.12345v2"` → `"2501.12345"`
      - `"q-bio.GN/0501001"` → `"q-bio.GN/0501001"` (old-style)
      - `"PMID:42098827"` → `None`
      - `"10.1101/..."` → `None`
      - garbage → `None`

    arXiv IDs come in two formats:
      - New (post-2007): `YYMM.NNNNN[vN]` e.g. `2501.12345v2`
      - Old: `archive[.subj]/YYMMNNN` e.g. `q-bio.GN/0501001`
    """
    s = (s or "").strip()
    if not s:
        return None
    body = _ARXIV_PREFIX_RE.sub("", s)
    body = _ARXIV_VERSION_SUFFIX_RE.sub("", body)
    if re.fullmatch(r"\d{4}\.\d{4,5}", body):
        return body
    # Old style: archive[.subj]/YYMMNNN
    if re.fullmatch(r"[a-z\-]+(\.[A-Z]{2})?/\d{7}", body):
        return body
    return None
