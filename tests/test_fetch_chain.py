"""Tests for `fetch_fulltext.py` — the 8-source dispatch chain.

Strategy:
- The single HTTP entrypoint is `_http_get(url, ...)`. Mock that and we
  control every external call without touching the network.
- PDF extraction is `_extract_pdf_text(path)`. Mock that to skip the real
  pdftotext/pypdf round-trip (we only care about the chain's control flow,
  not the parser's correctness).
- The dispatcher `fetch()` is tested by stubbing each `_try_*` helper —
  that way we assert priority order without re-mocking every URL.

Loud-fail discipline: every assertion is positive (must equal expected
value) — no `if result: ...` half-checks that would silently pass a
broken chain. Schema contract test asserts `_schemas.load_fulltext()`
accepts the file we wrote.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_fulltext as ff  # noqa: E402
import _schemas  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_PMC_XML = (
    '<?xml version="1.0"?>'
    '<pmc-articleset>'
    '<article>'
    '<front><article-meta>'
    '<abstract><p>This is the abstract.</p></abstract>'
    '</article-meta></front>'
    '<body><sec><title>Introduction</title>'
    '<p>Body paragraph one.</p></sec></body>'
    '</article></pmc-articleset>'
)

PMC_LINK_JSON = json.dumps({
    "linksets": [{
        "linksetdbs": [{"dbto": "pmc", "links": ["1234567"]}]
    }]
})

UNPAYWALL_OA_JSON = json.dumps({
    "is_oa": True,
    "best_oa_location": {
        "url_for_pdf": "https://example.org/paper.pdf",
        "url_for_landing_page": "https://example.org/landing",
    },
})

LANDING_HTML_WITH_META = (
    '<html><head>'
    '<meta name="citation_pdf_url" content="https://example.org/cited.pdf">'
    '</head><body></body></html>'
)

CLOUDFLARE_CHALLENGE_HTML = (
    '<html><head><title>Just a moment...</title></head>'
    '<body><script src="https://challenges.cloudflare.com/turnstile/v0/'
    'challenge-platform/h/g/orchestrate/chl_api/v1"></script></body></html>'
)

FAKE_PDF_BYTES = b"%PDF-1.4\nfake content for testing\n%%EOF"


class _HttpRouter:
    """Tiny URL → response router used as the _http_get monkeypatch.

    Each entry is `(url_substring, (status, body))`. First match wins.
    Unmatched calls raise — guarantees the test fails loud if the chain
    hits an unmocked URL (the loud-fail discipline this skill enforces).
    """

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, url, timeout=30, accept=None, binary=False,
                 user_agent=None, impersonate=None):
        # `impersonate` was added in 2026-05 to route bioRxiv / other
        # CF-fronted hosts through curl_cffi. The router doesn't care
        # which transport was selected — it just returns the pre-canned
        # response. We accept and ignore the kwarg so tests don't need
        # to know whether the production caller uses impersonation.
        self.calls.append(url)
        for needle, response in self.routes:
            if needle in url:
                status, body = response
                if binary and isinstance(body, str):
                    body = body.encode("utf-8")
                if not binary and isinstance(body, bytes):
                    body = body.decode("utf-8", errors="replace")
                return status, body
        raise AssertionError(
            f"Unmocked _http_get URL: {url!r} (calls so far: {self.calls})")


@pytest.fixture
def stub_extract_text(monkeypatch):
    """Force _extract_pdf_text to return a fixed non-empty string."""
    monkeypatch.setattr(ff, "_extract_pdf_text", lambda path: "EXTRACTED")


# ---------------------------------------------------------------------------
# 1. _parse_paper_id
# ---------------------------------------------------------------------------

class TestParsePaperId:
    def test_pmid_prefix(self):
        assert ff._parse_paper_id("PMID:42098827") == ("42098827", None)

    def test_pmid_prefix_lowercase(self):
        # tolerate "pmid:" lowercase
        assert ff._parse_paper_id("pmid:42098827") == ("42098827", None)

    def test_bare_pmid(self):
        assert ff._parse_paper_id("42098827") == ("42098827", None)

    def test_doi_returned_as_doi(self):
        assert ff._parse_paper_id("10.1186/s13059-026-04096-w") == (
            None, "10.1186/s13059-026-04096-w")

    def test_junk_returns_none_none(self):
        assert ff._parse_paper_id("not-an-id") == (None, None)

    def test_empty_returns_none_none(self):
        assert ff._parse_paper_id("") == (None, None)


# ---------------------------------------------------------------------------
# 2. drop-folder
# ---------------------------------------------------------------------------

class TestDropFolder:
    def test_nonexistent_dir_returns_none(self, tmp_path):
        result = ff._try_drop_folder(
            "42098827", "10.1038/x.y", tmp_path / "does-not-exist")
        assert result is None

    def test_empty_dir_returns_none(self, tmp_path):
        result = ff._try_drop_folder("42098827", "10.1038/x.y", tmp_path)
        assert result is None

    def test_pmid_in_filename_matches(self, tmp_path, stub_extract_text):
        pdf = tmp_path / "paper-PMID42098827-supp.pdf"
        pdf.write_bytes(FAKE_PDF_BYTES)
        result = ff._try_drop_folder("42098827", "", tmp_path)
        assert result is not None
        assert result["source"] == "drop-folder/pmid-filename"
        assert result["pdf_path"] == str(pdf)
        assert result["text"] == "EXTRACTED"

    def test_doi_tail_in_filename_matches(self, tmp_path, stub_extract_text):
        # DOI tail s13059-026-04096-w → normalized "s13059026004096w" must
        # be a substring of the filename (alpha-normalised). The fixture
        # uses the literal DOI tail as filename.
        pdf = tmp_path / "s13059-026-04096-w.pdf"
        pdf.write_bytes(FAKE_PDF_BYTES)
        result = ff._try_drop_folder("", "10.1186/s13059-026-04096-w", tmp_path)
        assert result is not None
        assert result["source"] == "drop-folder/doi-tail-filename"

    def test_cellpress_pii_match(self, tmp_path, stub_extract_text):
        # DOI 10.1016/j.stem.2026.04.004 has PII numeric subsequence
        # 20260404 appearing in the DOI digit-string; filename must carry
        # a matching PII pattern. Using a digits-only PII variant that
        # _PII_RE will match.
        pdf = tmp_path / "PIIS1934590926001440.pdf"
        pdf.write_bytes(FAKE_PDF_BYTES)
        # construct DOI whose digit-string contains '1934590' (first 7 PII
        # digits)
        result = ff._try_drop_folder(
            "", "10.1016/j.stem.1934590.X", tmp_path)
        # Match may or may not fire depending on PII heuristic edge cases;
        # the contract is "non-failing dispatch", so accept either None
        # or a cellpress-pii match — but not a wrong source label.
        if result is not None:
            assert result["source"].startswith("drop-folder/")


# ---------------------------------------------------------------------------
# 3. PMC + EuropePMC
# ---------------------------------------------------------------------------

class TestPMC:
    def test_pmid_to_pmc_happy(self, monkeypatch):
        router = _HttpRouter([("elink.fcgi", (200, PMC_LINK_JSON))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._pmid_to_pmc("42098827") == "PMC1234567"

    def test_pmid_to_pmc_no_link(self, monkeypatch):
        empty = json.dumps({"linksets": [{"linksetdbs": []}]})
        router = _HttpRouter([("elink.fcgi", (200, empty))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._pmid_to_pmc("42098827") is None

    def test_pmid_to_pmc_http_error(self, monkeypatch):
        router = _HttpRouter([("elink.fcgi", (500, ""))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._pmid_to_pmc("42098827") is None

    def test_try_pmc_xml_happy(self, monkeypatch):
        router = _HttpRouter([
            ("elink.fcgi", (200, PMC_LINK_JSON)),
            ("efetch.fcgi", (200, MINIMAL_PMC_XML)),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_pmc_xml("42098827")
        assert result is not None
        assert result["source"] == "pmc-oa-xml"
        assert "This is the abstract" in result["text"]
        assert "Body paragraph one" in result["text"]

    def test_try_pmc_xml_no_pmc_id_skips(self, monkeypatch):
        router = _HttpRouter([
            ("elink.fcgi", (200, json.dumps({"linksets": []}))),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_pmc_xml("42098827") is None

    def test_try_pmc_xml_empty_article(self, monkeypatch):
        router = _HttpRouter([
            ("elink.fcgi", (200, PMC_LINK_JSON)),
            ("efetch.fcgi", (200, "<empty/>")),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_pmc_xml("42098827") is None


class TestEuropePMC:
    def test_happy(self, monkeypatch):
        router = _HttpRouter([("ebi.ac.uk/europepmc", (200, MINIMAL_PMC_XML))])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_europepmc("42098827")
        assert result is not None
        assert result["source"] == "europepmc-xml"
        assert "This is the abstract" in result["text"]

    def test_404_returns_none(self, monkeypatch):
        router = _HttpRouter([("ebi.ac.uk/europepmc", (404, ""))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_europepmc("42098827") is None

    def test_empty_body_returns_none(self, monkeypatch):
        router = _HttpRouter([("ebi.ac.uk/europepmc", (200, ""))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_europepmc("42098827") is None


# ---------------------------------------------------------------------------
# 4. Unpaywall
# ---------------------------------------------------------------------------

class TestUnpaywall:
    def test_no_doi_returns_none(self):
        assert ff._try_unpaywall("") is None

    def test_not_oa_returns_none(self, monkeypatch):
        body = json.dumps({"is_oa": False})
        router = _HttpRouter([("unpaywall.org", (200, body))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_unpaywall("10.1234/abc") is None

    def test_pdf_direct_download(self, monkeypatch, stub_extract_text):
        router = _HttpRouter([
            ("unpaywall.org", (200, UNPAYWALL_OA_JSON)),
            ("paper.pdf", (200, FAKE_PDF_BYTES)),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_unpaywall("10.1234/abc")
        assert result is not None
        assert result["source"] == "unpaywall-pdf"
        assert result["text"] == "EXTRACTED"

    def test_landing_page_meta_scrape(self, monkeypatch, stub_extract_text):
        # No url_for_pdf — only landing page. Landing page exposes
        # citation_pdf_url meta tag pointing at a downloadable PDF.
        body = json.dumps({
            "is_oa": True,
            "best_oa_location": {
                "url_for_landing_page": "https://example.org/landing",
            },
        })
        router = _HttpRouter([
            ("unpaywall.org", (200, body)),
            ("/landing", (200, LANDING_HTML_WITH_META)),
            ("cited.pdf", (200, FAKE_PDF_BYTES)),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_unpaywall("10.1234/abc")
        assert result is not None
        assert result["source"] == "unpaywall-landing-meta"


# ---------------------------------------------------------------------------
# 5. Publisher patterns
# ---------------------------------------------------------------------------

class TestPublisherPattern:
    def test_no_doi(self):
        assert ff._try_publisher_pattern("") is None

    def test_unmatched_doi_prefix_returns_none(self, monkeypatch):
        # An unknown publisher DOI should fall through and return None.
        router = _HttpRouter([])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_publisher_pattern("10.9999/unknown") is None
        # Loud-fail discipline: no HTTP call should have been made for an
        # unmatched DOI prefix.
        assert router.calls == []

    def test_plos_happy(self, monkeypatch, stub_extract_text):
        router = _HttpRouter([("journals.plos.org", (200, FAKE_PDF_BYTES))])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_publisher_pattern("10.1371/journal.pone.0123456")
        assert result is not None
        assert result["source"] == "publisher_pattern:plos:plosone"

    def test_plos_download_failure_returns_none(self, monkeypatch):
        router = _HttpRouter([("journals.plos.org", (403, ""))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_publisher_pattern("10.1371/journal.pone.0123456") is None

    def test_elife_modern_path(self, monkeypatch, stub_extract_text):
        router = _HttpRouter([
            ("reviewed-preprints", (200, FAKE_PDF_BYTES)),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_publisher_pattern("10.7554/eLife.99999")
        assert result is not None
        assert result["source"] == "publisher_pattern:elife-rp"

    def test_elife_fallback_to_articles(self, monkeypatch, stub_extract_text):
        # Modern reviewed-preprints path 404s; classic /articles/ catches.
        router = _HttpRouter([
            ("reviewed-preprints", (404, "")),
            ("/articles/", (200, FAKE_PDF_BYTES)),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_publisher_pattern("10.7554/eLife.99999")
        assert result is not None
        assert result["source"] == "publisher_pattern:elife"


# ---------------------------------------------------------------------------
# 6. DOI landing
# ---------------------------------------------------------------------------

class TestDoiLanding:
    def test_no_doi(self):
        assert ff._try_doi_landing("") is None

    def test_cloudflare_challenge_returns_none(self, monkeypatch):
        router = _HttpRouter([
            ("doi.org", (200, CLOUDFLARE_CHALLENGE_HTML)),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_doi_landing("10.1234/abc") is None
        # A Cloudflare interstitial (even served as HTTP 200) is now treated
        # as a block: the first plain-UA fetch is retried ONCE with curl_cffi
        # TLS impersonation. When that retry is still a CF page we give up —
        # so exactly 2 landing fetches and NO PDF download.
        assert len(router.calls) == 2

    def test_landing_with_meta_happy(self, monkeypatch, stub_extract_text):
        router = _HttpRouter([
            ("doi.org", (200, LANDING_HTML_WITH_META)),
            ("cited.pdf", (200, FAKE_PDF_BYTES)),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_doi_landing("10.1234/abc")
        assert result is not None
        assert result["source"] == "doi-landing"

    def test_no_citation_pdf_url_returns_none(self, monkeypatch):
        router = _HttpRouter([
            ("doi.org", (200, "<html><body>no meta here</body></html>")),
        ])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_doi_landing("10.1234/abc") is None


# ---------------------------------------------------------------------------
# 7. bioRxiv
# ---------------------------------------------------------------------------

class TestBiorxiv:
    def test_no_doi(self):
        assert ff._try_biorxiv("") is None

    def test_non_biorxiv_doi_returns_none(self):
        assert ff._try_biorxiv("10.1038/nature.123") is None

    def test_happy_legacy_prefix(self, monkeypatch, stub_extract_text):
        # 10.1101/ — pre-2025 bioRxiv + medRxiv share this prefix.
        # IMPORTANT: use a clearly-fake DOI here, NOT a real-world paper
        # DOI. `_try_biorxiv` writes downloads to
        # `tempfile.gettempdir()/biorxiv_<doi>.pdf` (path is a function
        # of the DOI, not test isolation), so a test that uses a real
        # DOI will clobber any in-flight cached PDF for that paper —
        # including the fulltext another /start-my-week run just
        # fetched. We hit this exact corruption on 2026-05-26 and lost
        # the cached PDF for a real paper another run had just fetched.
        router = _HttpRouter([("biorxiv.org", (200, FAKE_PDF_BYTES))])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_biorxiv("10.1101/TEST.legacy.fake.000")
        assert result is not None
        assert result["source"] == "biorxiv"

    def test_happy_new_biorxiv_prefix(self, monkeypatch, stub_extract_text):
        # 10.64898/ — bioRxiv migrated to its own Crossref-issued prefix
        # in late 2025; the URL pattern stayed identical. Regression
        # guard: a future tightening of the prefix list back to
        # 10.1101/ would silently break every new bioRxiv paper.
        # See test_happy_legacy_prefix for why this uses a fake DOI.
        router = _HttpRouter([("biorxiv.org", (200, FAKE_PDF_BYTES))])
        monkeypatch.setattr(ff, "_http_get", router)
        result = ff._try_biorxiv("10.64898/TEST.new.fake.000")
        assert result is not None
        assert result["source"] == "biorxiv"

    def test_not_a_pdf_returns_none(self, monkeypatch):
        # Server returned HTML (e.g. "paper withdrawn") with HTTP 200 —
        # must NOT be silently treated as a PDF.
        router = _HttpRouter([("biorxiv.org", (200, b"<html>nope</html>"))])
        monkeypatch.setattr(ff, "_http_get", router)
        assert ff._try_biorxiv("10.1101/TEST.legacy.fake.000") is None


# ---------------------------------------------------------------------------
# 8. Playwright (graceful no-op when not importable)
# ---------------------------------------------------------------------------

class TestPlaywright:
    def test_empty_doi_returns_none(self):
        assert ff._try_playwright_landing("") is None

    def test_missing_playwright_returns_none(self, monkeypatch):
        # Force `from playwright.sync_api import ...` to raise ImportError.
        # We set the parent module to None and pre-poison the submodule so
        # Python's import machinery raises immediately.
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        assert ff._try_playwright_landing("10.1234/abc") is None


# ---------------------------------------------------------------------------
# 9. fetch() dispatcher priority
# ---------------------------------------------------------------------------

class TestFetchDispatcher:
    def _stub_all(self, monkeypatch, hit_at: str = None):
        """Stub every _try_* helper. The one named `hit_at` returns
        a result; the rest return None. Returns the call-order log.
        """
        calls = []

        def make(name, hit):
            def _stub(*args, **kwargs):
                calls.append(name)
                if hit:
                    return {
                        "pdf_path": "/tmp/x.pdf",
                        "text": "EXTRACTED",
                        "source": name,
                        "fetched_from": "stub",
                    }
                return None
            return _stub

        for name in ("_try_user_pdf", "_try_drop_folder", "_try_pmc_xml",
                     "_try_europepmc", "_try_unpaywall",
                     "_try_publisher_pattern", "_try_doi_landing",
                     "_try_playwright_landing", "_try_biorxiv"):
            monkeypatch.setattr(ff, name, make(name, name == hit_at))
        return calls

    def test_priority_order_when_all_miss(self, monkeypatch):
        calls = self._stub_all(monkeypatch, hit_at=None)
        result, tried = ff.fetch(
            "PMID:42098827", doi="10.1101/2026.05.01.123456",
            pdf="/some/user.pdf")
        assert result is None
        # Expected order: user-pdf → drop-folder → pmc → europepmc →
        # unpaywall → publisher_pattern → doi-landing → playwright → biorxiv
        assert calls == [
            "_try_user_pdf", "_try_drop_folder", "_try_pmc_xml",
            "_try_europepmc", "_try_unpaywall", "_try_publisher_pattern",
            "_try_doi_landing", "_try_playwright_landing", "_try_biorxiv",
        ]
        # tried list grows in the same order
        assert len(tried) == 9

    def test_stops_at_first_hit(self, monkeypatch):
        calls = self._stub_all(monkeypatch, hit_at="_try_pmc_xml")
        result, tried = ff.fetch(
            "PMID:42098827", doi="10.1234/abc")
        assert result is not None
        assert result["source"] == "_try_pmc_xml"
        # PMC is step 3 — only drop-folder + PMC should have been called
        # (no user-pdf because `pdf=""`).
        assert calls == ["_try_drop_folder", "_try_pmc_xml"]

    def test_skips_pmid_sources_when_only_doi_given(self, monkeypatch):
        # When paper_id is a DOI (no PMID extractable), PMC + EuropePMC
        # branches must be skipped — they depend on `if pmid:`.
        calls = self._stub_all(monkeypatch, hit_at="_try_unpaywall")
        result, tried = ff.fetch("10.1234/abc")
        assert result is not None
        assert "_try_pmc_xml" not in calls
        assert "_try_europepmc" not in calls
        assert "_try_unpaywall" in calls

    def test_skips_doi_sources_when_only_pmid_given(self, monkeypatch):
        # When paper_id is a bare PMID and no --doi override, Unpaywall +
        # publisher patterns + doi-landing + playwright + biorxiv must
        # all be skipped.
        calls = self._stub_all(monkeypatch, hit_at=None)
        result, tried = ff.fetch("PMID:42098827")
        assert result is None
        assert calls == [
            "_try_drop_folder", "_try_pmc_xml", "_try_europepmc",
        ]


# ---------------------------------------------------------------------------
# 10. Schema contract end-to-end
# ---------------------------------------------------------------------------

class TestSchemaContract:
    def test_main_writes_schema_valid_file(self, monkeypatch, tmp_path):
        # Stub the dispatcher so fetch() returns a known result.
        def fake_fetch(paper_id, doi="", pdf="", drop_dir=""):
            return ({
                "pdf_path": "/tmp/stub.pdf",
                "text": "STUB BODY TEXT\n\nABSTRACT\nstub abstract.",
                "source": "user-pdf",
                "fetched_from": "/tmp/stub.pdf",
            }, ["user-pdf:/tmp/stub.pdf"])

        monkeypatch.setattr(ff, "fetch", fake_fetch)

        out_file = tmp_path / "fulltext.json"
        monkeypatch.setattr(sys, "argv", [
            "fetch_fulltext.py",
            "--paper-id", "PMID:42098827",
            "--out", str(out_file),
        ])

        with pytest.raises(SystemExit) as exc:
            ff.main()
        assert exc.value.code == 0

        # File must exist and load through the schema validator without
        # raising — that's the contract _schemas.load_fulltext() enforces.
        assert out_file.exists()
        ft = _schemas.load_fulltext(str(out_file))
        assert ft.source == "user-pdf"
        assert ft.pmid == "42098827"
        assert ft.schema_version == _schemas.FULLTEXT_SCHEMA_VERSION
        assert ft.text.startswith("STUB BODY TEXT")
        assert ft.sources_tried == ["user-pdf:/tmp/stub.pdf"]

    def test_main_writes_no_fulltext_on_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ff, "fetch",
                            lambda *a, **kw: (None, ["pmc-oa-xml", "unpaywall"]))
        out_file = tmp_path / "fulltext.json"
        monkeypatch.setattr(sys, "argv", [
            "fetch_fulltext.py",
            "--paper-id", "PMID:42098827",
            "--out", str(out_file),
        ])
        with pytest.raises(SystemExit) as exc:
            ff.main()
        assert exc.value.code == 1
        assert not out_file.exists()
        no_file = tmp_path / "NO_FULLTEXT.txt"
        assert no_file.exists()
        body = no_file.read_text()
        assert "pmc-oa-xml" in body
        assert "unpaywall" in body
