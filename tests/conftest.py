"""Pytest fixtures for start-my-week.

The autouse `_isolated_tempdir` fixture below makes every test run with
a redirected `tempfile.gettempdir()` so production cache paths cannot
be clobbered by test code.

Why this exists (2026-05-26 incident report):

The production code in `scripts/fetch_fulltext.py` writes downloaded
PDFs to `tempfile.gettempdir()/<predictable_name>.pdf` — typically
`/var/folders/.../T/biorxiv_10.64898_2026.05.18.724443.pdf` on macOS.
A test in `test_fetch_chain.py` (`test_happy_new_biorxiv_prefix`)
happened to use the same real-world DOI as a paper that was being
fetched mid-pipeline. Pytest's `FAKE_PDF_BYTES = b"%PDF-1.4\\nfake..."`
overwrote the cached 7.6 MB real PDF with 39 bytes of test fixture,
which then cascaded through to a corrupted vault archive and a 39-byte
Zotero attachment.

The shallow fix (use clearly-fake DOIs in tests) is already in
`tests/test_fetch_chain.py` — see commit `cd4db63`. This fixture is the
*structural* fix: even if a future test author accidentally uses a real
DOI again, the redirected tempdir means no production path can be hit.

The redirect is per-test (pytest's `tmp_path` is a per-test fresh
directory), and the autouse mechanism ensures it applies to every
test in `tests/` without each test having to opt in.
"""
from __future__ import annotations

import tempfile
import pytest


@pytest.fixture(autouse=True)
def _isolated_tempdir(monkeypatch, tmp_path):
    """Reroute tempfile.gettempdir() to a per-test tmp_path.

    Production functions that write to
    `Path(tempfile.gettempdir()) / "<name>.pdf"` (e.g. `_try_biorxiv`,
    `_try_playwright_landing`, `_download_pdf`) will write into the
    test's isolated tmp_path instead of the system temp dir. Tests can
    still inspect those paths via `tmp_path`.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture(autouse=True)
def _unpaywall_email(monkeypatch):
    """Provide a contact email so Unpaywall-path tests exercise the HTTP logic.

    Production `_try_unpaywall` returns early (skips the source) when
    `UNPAYWALL_EMAIL` is unset — Unpaywall's ToS requires a contact email and
    we never ship a hard-coded one. Tests that drive the Unpaywall branch
    therefore need the var set; this autouse fixture supplies a dummy address.
    """
    monkeypatch.setenv("UNPAYWALL_EMAIL", "tests@example.com")
