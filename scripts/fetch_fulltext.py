#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_fulltext.py — Best-effort full-text fetcher for paper-analyze.

The point of this script is to make sure the "deep analysis" step never
produces an abstract-only note while pretending to be a full read. It tries
the following sources, in order, stopping on first hit:

  1. User-supplied PDF via --pdf <path>.
  2. Drop-folder scan (default ~/Downloads/, override via env
     PAPER_PDF_DROP_DIR). Matches by PMID, DOI tail, or publisher filename
     hints (s13059-... for Genome Biology BMC; PIIS... for Cell Press; etc.).
  3. PubMed Central OA fulltext XML (NCBI efetch). Available only for PMC OA.
  4. EuropePMC fulltext XML. Covers many non-PMC PubMed papers when authors
     have submitted to UKPMC.
  5. Unpaywall API (DOI-keyed). Returns best OA PDF URL; we download + extract.
  6. Publisher-specific pure-OA patterns (PLOS, eLife, MDPI, Frontiers).
  6b. Generic DOI landing → citation_pdf_url scrape (VPN-aware). Catches
      paywalled-but-institutionally-subscribed publishers (Nature, Springer,
      OUP, CSHL Press, …). No-op on Cloudflare-fronted publishers.
  6c. Playwright-based Cloudflare bypass (VPN-aware). Real headless
      Chromium clears the CF JS challenge, then downloads the publisher
      PDF in the same browser context. Catches PNAS, Cell Press, Wiley,
      Elsevier, Adv. Sci. when VPN + subscription align. Gracefully
      no-ops if playwright isn't installed.
  7. bioRxiv / medRxiv API by DOI (10.1101/...).

Output:
  fulltext.json  — Canonical schema declared in `_schemas.py:Fulltext`.
                   Fields: pmid, doi, source, pdf_path (None for
                   text-only sources), text, abstract, fetched_from,
                   sources_tried, schema_version. Validated by
                   `_schemas.load_fulltext()` at the consumer side.
  NO_FULLTEXT.txt — if all sources fail; lists tried sources for debugging.

Exit codes:
  0 — success (fulltext.json written).
  1 — all sources failed (NO_FULLTEXT.txt written).
  2 — bad input.

Usage:
  python3 fetch_fulltext.py --paper-id PMID:42098827 [--doi 10.1186/...] \
                            --out fulltext.json
  python3 fetch_fulltext.py --paper-id PMID:42098827 --pdf path/to/file.pdf \
                            --out fulltext.json

Environment:
  PAPER_PDF_DROP_DIR  override default ~/Downloads/ drop folder.
  UNPAYWALL_EMAIL     your contact email; required to enable the Unpaywall
                      source (Unpaywall's API ToS). Unset = Unpaywall skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import requests
    _USE_REQUESTS = True
except ImportError:
    import urllib.request  # noqa: F401
    _USE_REQUESTS = False


# ---------------------------------------------------------------------------
# HTTP + input plumbing
# ---------------------------------------------------------------------------

# A single shared Session so cookies set on a publisher's landing page
# (e.g. a subscription/consent cookie) persist into the subsequent PDF
# request. Without this, every GET was a cookieless one-shot and many
# institutionally-subscribed publishers handed back an HTML access page
# instead of the PDF even on a correct VPN IP.
_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        # Opt-in institutional proxy (e.g. an EZproxy/SOCKS endpoint) for
        # users whose access is proxy-based rather than VPN-IP-based.
        proxy = os.environ.get("PAPER_FULLTEXT_PROXY", "").strip()
        if proxy:
            _SESSION.proxies.update({"http": proxy, "https": proxy})
            logger.info("[http] routing through PAPER_FULLTEXT_PROXY=%s", proxy)
    return _SESSION


def _looks_like_cf_challenge(body) -> bool:
    """True if an HTTP body is a Cloudflare/Turnstile interstitial.

    Detection is body-based (not status-based) because CF increasingly
    serves challenge pages with HTTP 200, which the old `status in
    (403, 503)` gate missed — so the impersonation retry never fired.
    """
    if not body:
        return False
    low = (body if isinstance(body, str)
           else body.decode("utf-8", errors="replace")).lower()
    return (("just a moment" in low and "challenge" in low)
            or "challenge-platform" in low
            or "cf-mitigated" in low
            or "/cdn-cgi/challenge-platform" in low)


def _capabilities() -> dict:
    """Report which optional transports/PDF extractors are importable.

    Lets a failure explain *why* (e.g. "playwright: NO") instead of a
    generic NO_FULLTEXT — the audit found the whole Cloudflare-bypass and
    PDF-extraction capability silently absent on a default install.
    """
    caps = {}
    for name, mod in (("curl_cffi", "curl_cffi"),
                      ("playwright", "playwright"),
                      ("PyMuPDF", "fitz"),
                      ("pypdf", "pypdf")):
        try:
            __import__(mod)
            caps[name] = True
        except Exception:
            caps[name] = False
    caps["pdftotext"] = bool(shutil.which("pdftotext"))
    caps["pdf_extractor_available"] = (
        caps["pdftotext"] or caps["PyMuPDF"] or caps["pypdf"])
    return caps

def _http_get(url, timeout=30, accept=None, binary=False, user_agent=None,
              impersonate=None):
    """Fetch a URL; return (status_code, body) or (None, None) on error.

    Pass user_agent="browser" to use a realistic desktop-browser UA for
    publishers that bot-filter on UA (e.g. MDPI). Default keeps the
    identifying skill UA so server logs stay clean.

    Pass `impersonate="chrome124"` (or another curl_cffi browser tag) to
    route the request through curl_cffi instead of `requests`. curl_cffi
    mimics a real Chrome TLS fingerprint (JA3/JA4), which is necessary
    for Cloudflare-fronted hosts that fingerprint connections — most
    notably bioRxiv from late 2025 onwards. Empirically (probed
    2026-05-26 against bioRxiv 10.64898/...), curl_cffi clears CF in
    ~2 s where plain `requests` returns 403 immediately and headless
    Playwright stalls 60+ s. Falls back to the default `requests` path
    if `curl_cffi` isn't installed, so the function is safe to call
    with `impersonate=` on machines without the optional dep.
    """
    if user_agent == "browser":
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
    else:
        ua = "fetch-fulltext/1.0 (start-my-day skill)"
    headers = {"User-Agent": ua}
    if accept:
        headers["Accept"] = accept

    # CF-fronted hosts: try TLS impersonation first. Gracefully fall through
    # if curl_cffi isn't installed so the function still works on minimal
    # envs (per "soft optional" dep policy).
    if impersonate:
        try:
            from curl_cffi import requests as _curl_requests
        except ImportError:
            logger.info("[_http_get] curl_cffi not installed; "
                        "ignoring impersonate=%s and falling back to requests",
                        impersonate)
        else:
            try:
                r = _curl_requests.get(
                    url, headers=headers, timeout=timeout,
                    impersonate=impersonate,
                )
                return r.status_code, (r.content if binary else r.text)
            except Exception as e:
                # Fall THROUGH to the plain-requests path below — a
                # transient curl_cffi error shouldn't abort the whole fetch
                # when an ordinary request might still succeed. (Previously
                # this returned (None, None) and killed the call.)
                logger.warning("HTTP GET (impersonate=%s) failed: %s (%s); "
                               "falling back to plain requests",
                               impersonate, url, e)

    try:
        if _USE_REQUESTS:
            r = _get_session().get(url, headers=headers, timeout=timeout,
                                   allow_redirects=True)
            return r.status_code, (r.content if binary else r.text)
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return resp.status, (data if binary else data.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning("HTTP GET failed: %s (%s)", url, e)
        return None, None


def _parse_paper_id(paper_id: str):
    """Return (pmid_or_None, doi_or_None) given a paper id string.

    Thin wrapper preserved for source compatibility. The canonical
    implementation lives in `scripts/_id_parser.py` so the same parsing
    rules apply across `fetch_fulltext`, `save_to_zotero`,
    `search_pubmed`, and `generate_note`.
    """
    try:
        from _id_parser import parse_paper_id
    except ImportError:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from _id_parser import parse_paper_id
    return parse_paper_id(paper_id)


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_path: str) -> str:
    """Extract plain text from a PDF.

    Backends, in preference order:
      1. pdftotext (poppler CLI) — best layout fidelity, but a system
         binary that is frequently absent (esp. on macOS).
      2. PyMuPDF / fitz — pip-installable and pinned in requirements.txt,
         so this is the reliable fallback when poppler is missing.
      3. pypdf — last resort.

    Returns "" only when EVERY backend is missing or fails, and logs loudly
    in that case. This matters: callers treat empty text as a fetch failure
    (`if not text.strip(): return None`), so a missing extractor used to
    silently discard perfectly-downloaded PDFs — a prime cause of "can't
    access the paper even on VPN".
    """
    if shutil.which("pdftotext"):
        try:
            import subprocess
            out = subprocess.run(
                ["pdftotext", "-layout", pdf_path, "-"],
                capture_output=True, text=True, timeout=120,
            )
            if out.returncode == 0 and out.stdout:
                return out.stdout
        except Exception as e:
            logger.warning("pdftotext failed: %s", e)
    # PyMuPDF (fitz) — pinned in requirements.txt, so the normal fallback.
    try:
        import fitz  # PyMuPDF
        with fitz.open(pdf_path) as doc:
            text = "\n".join(page.get_text() for page in doc)
        if text.strip():
            return text
    except ImportError:
        logger.info("PyMuPDF (fitz) not installed; trying pypdf")
    except Exception as e:
        logger.warning("PyMuPDF extraction failed: %s", e)
    # Last resort: pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ImportError:
        logger.error(
            "No PDF text extractor available (tried pdftotext, PyMuPDF, "
            "pypdf). Install one — `pip install PyMuPDF` (or `brew install "
            "poppler`) — otherwise every downloaded PDF is discarded as a "
            "failed fetch.")
    except Exception as e:
        logger.warning("pypdf failed: %s", e)
    return ""


def _looks_like_doi(s: str) -> bool:
    return bool(s and re.match(r"^10\.\d{4,9}/", s))


# ---------------------------------------------------------------------------
# 1. user-supplied PDF
# ---------------------------------------------------------------------------

def _try_user_pdf(pdf_path: str, pmid: str, doi: str):
    p = Path(pdf_path).expanduser()
    if not p.is_file():
        logger.warning("[user-pdf] %s does not exist", p)
        return None
    text = _extract_pdf_text(str(p))
    if not text.strip():
        logger.warning("[user-pdf] no text extracted from %s", p)
        return None
    return {
        "pdf_path": str(p),
        "text": text,
        "source": "user-pdf",
        "fetched_from": str(p),
    }


# ---------------------------------------------------------------------------
# 2. drop-folder scan
# ---------------------------------------------------------------------------

def _doi_tail(doi: str) -> str:
    """Return the post-slash segment of a DOI (used for filename matching)."""
    if not doi or "/" not in doi:
        return ""
    return doi.split("/", 1)[1].lower()


_PII_RE = re.compile(r"S?\d{4}-?\d{4}\(?\d{2}\)?\d{5}-?[\dX]", re.IGNORECASE)


def _looks_like_cellpress_pii(filename: str, doi: str) -> bool:
    """Match Cell Press PII filenames (PIIS...) against the paper's DOI."""
    if not doi:
        return False
    m = _PII_RE.search(filename)
    if not m:
        return False
    pii = re.sub(r"[^0-9X]", "", m.group(0).upper())
    # Cell Press DOIs include the PII numbers (e.g. 10.1016/j.stem.2026.04.004
    # corresponds to PII S1934-5909(26)00144-X). Heuristic: if any 7+ digit
    # subsequence of the PII appears in the DOI, accept.
    doi_digits = re.sub(r"[^0-9]", "", doi)
    return any(pii[i:i + 7] in doi_digits for i in range(len(pii) - 6))


def _try_drop_folder(pmid: str, doi: str, drop_dir: Path):
    """Scan a directory of mixed PDFs for one that matches PMID or DOI."""
    if not drop_dir.is_dir():
        logger.info("[drop-folder] %s does not exist; skipping", drop_dir)
        return None
    doi_tail = _doi_tail(doi)
    doi_tail_alpha = re.sub(r"[^a-z0-9]", "", doi_tail) if doi_tail else ""
    pdfs = sorted(drop_dir.glob("*.pdf"))
    if not pdfs:
        return None
    logger.info("[drop-folder] scanning %d PDFs in %s", len(pdfs), drop_dir)
    for p in pdfs:
        name_lower = p.name.lower()
        name_alpha = re.sub(r"[^a-z0-9]", "", name_lower)
        # PMID in filename
        if pmid and pmid in name_lower:
            logger.info("[drop-folder] PMID match: %s", p.name)
            return _wrap_pdf(p, "drop-folder/pmid-filename")
        # DOI tail in filename (e.g. s13059-026-04096-w)
        if doi_tail_alpha and doi_tail_alpha in name_alpha:
            logger.info("[drop-folder] DOI-tail match: %s", p.name)
            return _wrap_pdf(p, "drop-folder/doi-tail-filename")
        # Cell Press PII heuristic
        if doi and _looks_like_cellpress_pii(p.name, doi):
            logger.info("[drop-folder] Cell-Press PII match: %s", p.name)
            return _wrap_pdf(p, "drop-folder/cellpress-pii")
        # Title/subject metadata match — fall back to pdfinfo
        try:
            import subprocess
            info = subprocess.run(["pdfinfo", str(p)], capture_output=True,
                                  text=True, timeout=15)
            if info.returncode == 0 and doi and doi.lower() in info.stdout.lower():
                logger.info("[drop-folder] metadata DOI match: %s", p.name)
                return _wrap_pdf(p, "drop-folder/metadata-doi")
        except Exception:
            pass
    return None


def _wrap_pdf(p: Path, source: str):
    text = _extract_pdf_text(str(p))
    if not text.strip():
        return None
    return {
        "pdf_path": str(p),
        "text": text,
        "source": source,
        "fetched_from": str(p),
    }


# ---------------------------------------------------------------------------
# 3. PMC OA fulltext XML
# ---------------------------------------------------------------------------

def _pmid_to_pmc(pmid: str):
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
        f"?dbfrom=pubmed&db=pmc&id={pmid}&retmode=json"
    )
    status, body = _http_get(url)
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    for ls in data.get("linksets", []):
        for db in ls.get("linksetdbs", []):
            if db.get("dbto") == "pmc":
                links = db.get("links", [])
                if links:
                    return f"PMC{links[0]}"
    return None


def _try_pmc_xml(pmid: str):
    pmc = _pmid_to_pmc(pmid)
    if not pmc:
        logger.info("[pmc] PMID:%s has no PMC OA copy", pmid)
        return None
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pmc&id={pmc.lstrip('PMC')}&retmode=xml"
    )
    status, body = _http_get(url, accept="application/xml")
    if status != 200 or not body or "<article" not in body:
        logger.info("[pmc] efetch returned no article XML for %s", pmc)
        return None
    text = _jats_to_text(body)
    if not text.strip():
        return None
    return {
        "pdf_path": "",
        "text": text,
        "source": "pmc-oa-xml",
        "fetched_from": url,
        "pmc": pmc,
    }


def _jats_to_text(xml_text: str) -> str:
    """Very simple JATS → plain-text flattener (preserves section order)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    pieces = []
    # Abstract
    for ab in root.iter("abstract"):
        pieces.append("ABSTRACT\n" + " ".join(ab.itertext()).strip() + "\n")
    # Body sections
    for body in root.iter("body"):
        for sec in body.iter("sec"):
            title = sec.find("title")
            if title is not None and title.text:
                pieces.append("\n" + title.text.upper() + "\n")
            for p in sec.iter("p"):
                pieces.append(" ".join(p.itertext()).strip())
    return "\n\n".join(pieces)


# ---------------------------------------------------------------------------
# 4. EuropePMC fulltext XML
# ---------------------------------------------------------------------------

def _try_europepmc(pmid: str):
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/article/MED/"
        f"{pmid}/fullTextXML"
    )
    status, body = _http_get(url, accept="application/xml")
    if status != 200 or not body or "<article" not in body:
        logger.info("[europepmc] no fulltext XML for PMID:%s (HTTP %s)",
                    pmid, status)
        return None
    text = _jats_to_text(body)
    if not text.strip():
        return None
    return {
        "pdf_path": "",
        "text": text,
        "source": "europepmc-xml",
        "fetched_from": url,
    }


# ---------------------------------------------------------------------------
# 5. Unpaywall
# ---------------------------------------------------------------------------

# Attribute order inside a <meta> tag is not significant in HTML, and
# Springer/Nature (and several CDN/template stacks) emit
# `<meta content="…" name="citation_pdf_url">` — content BEFORE name. The
# old `name=…[^>]*content=…` pattern silently missed those, so the PDF was
# never discovered. A lookahead asserts the right `name=` is somewhere in
# the same tag, then captures `content=` regardless of order.
_CITATION_PDF_META_RE = re.compile(
    r'<meta\b(?=[^>]*\bname=["\']citation_pdf_url["\'])'
    r'[^>]*?\bcontent=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_BMC_DOI_PREFIX = "10.1186/"


def _find_pdf_in_html(html: str, base_url: str = "") -> str:
    """Find a citation_pdf_url meta tag or another reasonable PDF link."""
    if not html:
        return ""
    m = _CITATION_PDF_META_RE.search(html)
    if m:
        pdf_url = m.group(1).strip()
        # Some publishers embed unescaped HTML entities
        return pdf_url.replace("&amp;", "&")
    # Fallback heuristics: look for any /pdf/ link mentioning the DOI tail
    m = re.search(r'href=["\']([^"\']+\.pdf)["\']', html, re.IGNORECASE)
    if m:
        href = m.group(1)
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/") and base_url:
            from urllib.parse import urljoin
            return urljoin(base_url, href)
        return href
    return ""


def _download_pdf(url: str, label: str, doi_for_filename: str,
                  user_agent=None, impersonate=None):
    """Download a PDF; return (tmp_path, extracted_text) or (None, None).

    Pass `impersonate="chrome124"` for CF-fronted hosts (bioRxiv, etc.)
    to route the request through curl_cffi's TLS impersonation. See
    `_http_get` for details.
    """
    status, content = _http_get(url, binary=True, timeout=60,
                                user_agent=user_agent,
                                impersonate=impersonate)
    if status != 200 or not content:
        logger.info("[%s] download HTTP %s (%d bytes)", label, status,
                    len(content or b""))
        return None, None
    if not content.startswith(b"%PDF"):
        logger.info("[%s] response is not a PDF (HTTP %s, %d bytes)",
                    label, status, len(content))
        return None, None
    # Per-call unique tmpfile so two concurrent runs of the same DOI
    # cannot clobber each other's download (DEFERRED.md #4 — the hazard
    # also enabled the test-fixture-overwrites-real-PDF incident of
    # 2026-05-26). The `{label}_{safe}` prefix is still informative for
    # debugging without being deterministic.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_",
                  doi_for_filename or "paper")
    fd, tmp_path = tempfile.mkstemp(
        suffix=".pdf",
        prefix=f"{label}_{safe}_",
        dir=tempfile.gettempdir(),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    text = _extract_pdf_text(tmp_path)
    if not text.strip():
        # Clean up the orphaned download so failed attempts don't leak
        # temp PDFs (the success path intentionally keeps the file).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None, None
    return tmp_path, text


def _bmc_pdf_url(doi: str) -> str:
    """Return BMC PDF URL using the standard /counter/pdf/<tail>.pdf pattern.

    Works for Genome Biology, BMC Bioinformatics, BMC Genomics, etc. — but we
    need the journal subdomain. Heuristic: most BMC journals expose
    /content/pdf/<tail>.pdf via link.springer.com as a stable mirror.
    """
    if not doi.startswith(_BMC_DOI_PREFIX):
        return ""
    return f"https://link.springer.com/content/pdf/{doi}.pdf"


def _try_unpaywall(doi: str):
    """Unpaywall → publisher landing page → citation_pdf_url meta-tag PDF."""
    if not doi:
        return None
    # Unpaywall's API requires a contact email (their ToS). Resolve it from the
    # environment — also checking ~/.zshrc / launchctl, since Claude Code / Codex
    # subprocesses don't inherit interactive-shell exports. We deliberately do
    # NOT ship a hard-coded address: set UNPAYWALL_EMAIL to your own email to
    # enable this source. If it's unset, skip Unpaywall cleanly and let the
    # other fetch steps handle the paper.
    try:
        from _env_resolve import load_env_from_user_shell
        load_env_from_user_shell(("UNPAYWALL_EMAIL",))
    except ImportError:
        pass
    email = os.environ.get("UNPAYWALL_EMAIL")
    if not email:
        logger.info(
            "[unpaywall] skipped — set UNPAYWALL_EMAIL to enable "
            "(Unpaywall's ToS requires a contact email)"
        )
        return None
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    status, body = _http_get(url, accept="application/json")
    if status != 200 or not body:
        logger.info("[unpaywall] HTTP %s for DOI %s", status, doi)
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None

    if not data.get("is_oa"):
        logger.info("[unpaywall] paper is not OA")
        return None

    best = data.get("best_oa_location") or {}
    candidates = []
    if best.get("url_for_pdf"):
        candidates.append(("unpaywall-pdf", best["url_for_pdf"]))
    # BMC pattern (stable; Unpaywall often leaves url_for_pdf null)
    if doi.startswith(_BMC_DOI_PREFIX):
        candidates.append(("unpaywall-bmc-springer", _bmc_pdf_url(doi)))
    if best.get("url_for_landing_page"):
        candidates.append(("unpaywall-landing", best["url_for_landing_page"]))
    elif best.get("url"):
        candidates.append(("unpaywall-landing", best["url"]))

    for label, candidate_url in candidates:
        if not candidate_url:
            continue
        if candidate_url.lower().endswith(".pdf") or "pdf" in candidate_url.lower():
            tmp, text = _download_pdf(candidate_url, label, doi)
            if tmp:
                return {"pdf_path": tmp, "text": text, "source": label,
                        "fetched_from": candidate_url}
        # Landing page → fetch HTML → look for citation_pdf_url meta
        logger.info("[%s] fetching landing page %s", label, candidate_url)
        status, html = _http_get(candidate_url, accept="text/html")
        if status != 200 or not html:
            continue
        # Some landing pages auto-redirect to PDF; if the body already starts
        # with %PDF (rare on text-mode fetch), treat as PDF. Use mkstemp
        # for collision safety; see _download_pdf for the same rationale.
        if isinstance(html, bytes) and html.startswith(b"%PDF"):
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
            fd, tmp = tempfile.mkstemp(
                suffix=".pdf",
                prefix=f"{label}_{safe}_",
                dir=tempfile.gettempdir(),
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(html)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            text = _extract_pdf_text(tmp)
            if text.strip():
                return {"pdf_path": tmp, "text": text, "source": label,
                        "fetched_from": candidate_url}
        pdf_link = _find_pdf_in_html(html, base_url=candidate_url)
        if not pdf_link:
            continue
        logger.info("[%s] citation_pdf_url: %s", label, pdf_link)
        tmp, text = _download_pdf(pdf_link, label + "-meta", doi)
        if tmp:
            return {"pdf_path": tmp, "text": text,
                    "source": label + "-meta",
                    "fetched_from": pdf_link}

    logger.info("[unpaywall] all candidate URLs failed for %s", doi)
    return None


# ---------------------------------------------------------------------------
# 6. Publisher-specific OA URL patterns (PLOS, eLife, MDPI, Frontiers)
# ---------------------------------------------------------------------------
#
# These cover pure-OA publishers that Unpaywall sometimes misses or only
# reports as landing pages. Each pattern below was current as of 2026-05;
# templates have been stable for years for these publishers, but the
# Frontiers entry deliberately resolves the landing page via doi.org and
# scrapes citation_pdf_url because Frontiers PDFs are served behind UUIDs
# that cannot be constructed from the DOI.

_PLOS_JOURNAL_BY_PREFIX = {
    "10.1371/journal.pone.": "plosone",
    "10.1371/journal.pbio.": "plosbiology",
    "10.1371/journal.pgen.": "plosgenetics",
    "10.1371/journal.pcbi.": "ploscompbiol",
    "10.1371/journal.ppat.": "plospathogens",
    "10.1371/journal.pntd.": "plosntds",
    "10.1371/journal.pmed.": "plosmedicine",
}


def _try_publisher_pattern(doi: str):
    """Try direct PDF URL patterns for known pure-OA publishers."""
    if not doi:
        return None

    # PLOS family (uniform article/file?id=<DOI>&type=printable pattern)
    for prefix, journal in _PLOS_JOURNAL_BY_PREFIX.items():
        if doi.startswith(prefix):
            url = (f"https://journals.plos.org/{journal}/article/file"
                   f"?id={doi}&type=printable")
            tmp, text = _download_pdf(
                url, f"publisher_pattern:plos:{journal}", doi)
            if tmp:
                return {"pdf_path": tmp, "text": text,
                        "source": f"publisher_pattern:plos:{journal}",
                        "fetched_from": url}
            return None  # PLOS prefix matched but download failed; skip others

    # eLife — DOI is 10.7554/eLife.<article_id>[.suffix]. eLife moved to a
    # reviewed-preprints publishing model in 2023, so newer papers live at
    # /reviewed-preprints/<id>.pdf while older versions of record live at
    # /articles/<id>.pdf. Try the modern path first, then the classic one.
    if doi.startswith("10.7554/eLife."):
        article_id = doi[len("10.7554/eLife."):].split(".", 1)[0]
        for sub, label in (("reviewed-preprints",
                            "publisher_pattern:elife-rp"),
                           ("articles", "publisher_pattern:elife")):
            url = f"https://elifesciences.org/{sub}/{article_id}.pdf"
            tmp, text = _download_pdf(url, label, doi)
            if tmp:
                return {"pdf_path": tmp, "text": text,
                        "source": label, "fetched_from": url}
        return None

    # MDPI — DOI is 10.3390/<article_path>; PDF lives at
    # https://www.mdpi.com/<article_path>/pdf. MDPI runs an Akamai-style
    # WAF that returns "Access Denied" to raw HTTP regardless of UA, so
    # this branch usually fails in practice; we still try (costs nothing
    # when MDPI 403s, and the WAF policy could relax in future). User
    # should rely on drop-folder + Unpaywall for MDPI papers when this
    # branch fails.
    if doi.startswith("10.3390/"):
        article_path = doi[len("10.3390/"):]
        url = f"https://www.mdpi.com/{article_path}/pdf"
        tmp, text = _download_pdf(url, "publisher_pattern:mdpi", doi,
                                  user_agent="browser")
        if tmp:
            return {"pdf_path": tmp, "text": text,
                    "source": "publisher_pattern:mdpi",
                    "fetched_from": url}
        return None

    # Frontiers — DOI is 10.3389/<...>; PDF path uses an internal UUID we
    # cannot construct, so resolve the landing page via doi.org and read
    # citation_pdf_url. Unpaywall usually handles this case, but Unpaywall
    # has been observed to return landing-page-only entries for recent
    # Frontiers papers — this gives a second chance.
    if doi.startswith("10.3389/"):
        landing = f"https://doi.org/{doi}"
        logger.info("[publisher_pattern:frontiers] fetching landing %s",
                    landing)
        status, html = _http_get(landing, accept="text/html")
        if status != 200 or not html:
            return None
        pdf_link = _find_pdf_in_html(html, base_url=landing)
        if not pdf_link:
            return None
        tmp, text = _download_pdf(
            pdf_link, "publisher_pattern:frontiers", doi)
        if tmp:
            return {"pdf_path": tmp, "text": text,
                    "source": "publisher_pattern:frontiers",
                    "fetched_from": pdf_link}
        return None

    return None


# ---------------------------------------------------------------------------
# 6b. Generic DOI-landing scrape (VPN-aware)
# ---------------------------------------------------------------------------
#
# Resolves the DOI to the publisher's canonical landing page via
# https://doi.org/<DOI>, extracts the <meta name="citation_pdf_url" ...>
# value (the same tag Google Scholar follows), and downloads that URL.
#
# When the user is connected to an institutional VPN (e.g. Oxford), this
# step reaches paywalled-but-subscribed publishers transparently — the
# downstream HTTP call inherits the VPN's IP, and IP-authenticated
# publishers (Nature group, Springer, OUP, CSHL Press, OUP, Wiley OnlineOpen
# OA pages, etc.) serve the PDF directly. Without VPN, the call still runs
# but returns 403 / a paywall landing page; we detect that and fall through.
#
# Cloudflare-fronted publishers (PNAS, Cell, full Wiley, Elsevier) emit a
# JS challenge HTML on doi.org redirects regardless of IP — this function
# detects the "Just a moment..." / "challenge-platform" marker and returns
# None cleanly, leaving the user to drop the PDF into ~/Downloads/.

def _try_doi_landing(doi: str):
    """Resolve DOI → publisher landing page → citation_pdf_url → download.

    Catches paywalled-but-institutionally-subscribed publishers (Nature,
    Springer, OUP, CSHL Press, etc.) when an institutional VPN is active,
    AND Cloudflare-fronted publishers (bioRxiv, PNAS, etc.) via curl_cffi
    TLS impersonation.

    Strategy: try plain `requests` first (fast, identifies our skill UA
    in server logs); on Cloudflare-challenge response, retry with
    curl_cffi TLS impersonation (clears bioRxiv CF in ~2 s as of
    2026-05). The retry is gated by whether the original response looked
    like a CF block — we don't want to impersonate Chrome on every
    benign landing page.

    Always uses a real-browser User-Agent.
    """
    if not doi:
        return None
    landing = f"https://doi.org/{doi}"
    logger.info("[doi-landing] resolving %s", landing)
    status, html = _http_get(landing, accept="text/html",
                             user_agent="browser")

    # CF block detection is body-based (CF now serves challenge pages with
    # HTTP 200 as well as 403/503). Retry with TLS impersonation whenever
    # the request errored, returned 403/503, or *looks* like a challenge.
    is_cf_block = _looks_like_cf_challenge(html)
    if status is None or status in (403, 503) or is_cf_block:
        logger.info("[doi-landing] CF block suspected (HTTP %s); "
                    "retrying with curl_cffi TLS impersonation", status)
        status, html = _http_get(landing, accept="text/html",
                                 user_agent="browser",
                                 impersonate="chrome124")

    if status != 200 or not html:
        logger.info("[doi-landing] landing HTTP %s", status)
        return None
    html_str = html if isinstance(html, str) else html.decode(
        "utf-8", errors="replace")
    # If we still have a CF challenge HTML after retry, give up cleanly.
    if _looks_like_cf_challenge(html_str):
        logger.info("[doi-landing] Cloudflare challenge persists "
                    "after impersonation retry, skipping")
        return None
    pdf_link = _find_pdf_in_html(html_str, base_url=landing)
    if not pdf_link:
        logger.info("[doi-landing] no citation_pdf_url meta on landing page")
        return None
    logger.info("[doi-landing] citation_pdf_url: %s", pdf_link)
    # Use impersonation for the PDF download too — many CF-fronted hosts
    # also gate the PDF endpoint behind the same fingerprint check.
    tmp, text = _download_pdf(pdf_link, "doi-landing", doi,
                              user_agent="browser",
                              impersonate="chrome124")
    if tmp:
        return {"pdf_path": tmp, "text": text,
                "source": "doi-landing",
                "fetched_from": pdf_link}
    return None


# ---------------------------------------------------------------------------
# 6c. Playwright-based Cloudflare bypass (last resort, VPN-aware)
# ---------------------------------------------------------------------------
#
# When _try_doi_landing detects a Cloudflare challenge, the plain HTTP
# fetcher can't proceed. This step launches a real headless Chromium
# (via the locally-installed `playwright` Python package), navigates to
# https://doi.org/<DOI>, lets Cloudflare's JS challenge execute and clear,
# then downloads the publisher's PDF using the same browser context so
# the CF-cleared session cookies apply.
#
# Works against PNAS / Cell Press / Wiley / Elsevier / Advanced Science
# and similar CF-fronted publishers — but only when the user is also on
# an institutional VPN whose IP range carries the relevant subscription
# (the PDF endpoint still does subscription auth after CF clears).
#
# Cost per CF-protected paper ≈ 5–15 s (browser launch + challenge wait
# + PDF download). For non-CF papers, earlier steps in the chain catch
# them first, so this step never runs.
#
# Gracefully no-ops if `playwright` is not importable so the script
# remains functional even on machines that didn't install Playwright.

def _playwright_fetch_once(doi: str, headless: bool):
    """One Playwright launch + CF clear + PDF download attempt.

    Returns a result dict on success, None on any failure. The outer
    `_try_playwright_landing` runs this once headless, and (if env
    var FETCH_FULLTEXT_HEADED=1 is set) once more with headless=False
    so the user can manually click through CF when the headless
    challenge stalls.

    When env var `FETCH_FULLTEXT_CHROME_PROFILE` is set to a directory
    path, Playwright uses a persistent Chromium user-data-dir at that
    path via `launch_persistent_context`. This keeps `__cf_clearance`
    cookies (and any subscription-auth session cookies the publisher
    might set) across runs. A single manual CF clear in headed mode
    seeds the cookies; subsequent headless runs within the cookie TTL
    bypass the challenge entirely.
    """
    from playwright.sync_api import (
        sync_playwright,
        TimeoutError as PWTimeout,
    )

    landing = f"https://doi.org/{doi}"
    label = "playwright-headless" if headless else "playwright-headed"
    logger.info("[%s] launching Chromium for %s", label, landing)
    # Headed runs may need user interaction — give them a longer CF
    # timeout (3 min vs 60 s). Headless runs stay at 60 s.
    cf_timeout_ms = 60000 if headless else 180000

    # Persistent-profile opt-in. Empty string / unset → ephemeral
    # context. Non-empty → `launch_persistent_context(user_data_dir=...)`
    # so CF clearance cookies survive across runs.
    profile_dir = os.environ.get("FETCH_FULLTEXT_CHROME_PROFILE", "").strip()
    context_args = dict(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
        locale="en-GB",
        timezone_id="Europe/London",
    )

    browser = None
    context = None
    try:
        with sync_playwright() as pw:
            if profile_dir:
                profile_path = Path(profile_dir).expanduser()
                profile_path.mkdir(parents=True, exist_ok=True)
                logger.info("[%s] using persistent Chrome profile at %s",
                            label, profile_path)
                # launch_persistent_context returns a BrowserContext;
                # no separate `browser` object. The context owns the
                # underlying browser process.
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    **context_args,
                )
            else:
                browser = pw.chromium.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(**context_args)
            page = context.new_page()
            if not headless:
                logger.info("[%s] visible browser open — if Cloudflare "
                            "shows a challenge, click it once; the run "
                            "will continue automatically", label)
            try:
                page.goto(landing, wait_until="domcontentloaded",
                          timeout=30000)
                # Wait for the CF challenge to clear: title leaves
                # "just a moment" and URL leaves /cdn-cgi/.
                page.wait_for_function(
                    "() => !document.title.toLowerCase()"
                    "  .includes('just a moment') "
                    "  && !location.pathname.includes('/cdn-cgi/')",
                    timeout=cf_timeout_ms,
                )
            except PWTimeout:
                logger.info("[%s] CF challenge did not clear in %ds",
                            label, cf_timeout_ms // 1000)
                return None

            pdf_url = page.evaluate(
                "() => { const m = document.querySelector("
                "  'meta[name=\"citation_pdf_url\"]'); "
                "  return m ? m.content : null; }"
            )
            if not pdf_url:
                logger.info("[%s] no citation_pdf_url on rendered page",
                            label)
                return None
            logger.info("[%s] citation_pdf_url: %s", label, pdf_url)

            # Download via the browser's download mechanism so all
            # CF-cleared cookies and Sec-Fetch-* headers are sent
            # exactly as a real click would. Many publishers (PNAS,
            # Cell, Wiley) 403 raw context.request.get() calls even
            # when the browser navigation works, because they check
            # for browser-only headers or the __cf_clearance cookie
            # in ways that the request API doesn't fully replicate.
            #
            # page.goto() on a download URL raises "Download is
            # starting" — that's expected and we catch it.
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
            # mkstemp for collision safety; close the fd immediately
            # since we're going to overwrite the file via Playwright's
            # download.save_as. See _download_pdf for the rationale.
            fd, tmp_path = tempfile.mkstemp(
                suffix=".pdf",
                prefix=f"playwright_{safe}_",
                dir=tempfile.gettempdir(),
            )
            os.close(fd)
            tmp = Path(tmp_path)
            # keep_tmp gates cleanup: every failure path below leaks the
            # mkstemp'd file unless we unlink it. Only the success return
            # sets keep_tmp=True (the result dict references the file).
            keep_tmp = False
            try:
                try:
                    with page.expect_download(timeout=60000) as dl_info:
                        try:
                            page.goto(pdf_url, timeout=30000)
                        except Exception:
                            # "Download is starting" is the success
                            # signal — ignore.
                            pass
                    download = dl_info.value
                    download.save_as(str(tmp))
                except PWTimeout:
                    logger.info("[%s] no download event "
                                "(server may have returned an HTML "
                                "access-block page instead of a PDF)",
                                label)
                    return None
                except Exception as e:
                    logger.info("[%s] download error: %s", label, e)
                    return None

                body = tmp.read_bytes() if tmp.exists() else b""
                if not body or not body.startswith(b"%PDF"):
                    logger.info("[%s] downloaded file not a PDF "
                                "(%d bytes, head=%r)",
                                label, len(body), body[:8])
                    return None
                text = _extract_pdf_text(str(tmp))
                if not text.strip():
                    return None
                keep_tmp = True
                return {"pdf_path": str(tmp), "text": text,
                        "source": "playwright" if headless else "playwright-headed",
                        "fetched_from": pdf_url}
            finally:
                if not keep_tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    except Exception as e:
        logger.warning("[%s] error: %s", label, e)
        return None
    finally:
        # Two cleanup paths: ephemeral launches own a `browser`;
        # persistent_context launches own only a `context`. Close
        # whichever exists; both are no-op if `sync_playwright` already
        # tore them down (the `with sync_playwright() as pw:` block exit
        # closes everything anyway).
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        elif context is not None:
            try:
                context.close()
            except Exception:
                pass


def _try_playwright_landing(doi: str):
    """Bypass Cloudflare via headless browser; needs VPN for IP auth.

    Returns None on any failure (missing playwright, CF didn't clear,
    no citation_pdf_url, non-PDF response, exception) so the outer
    chain falls through cleanly.

    When the headless attempt fails AND env var
    `FETCH_FULLTEXT_HEADED=1` is set, retries with a visible browser
    window so the user can manually clear CF if needed. Useful when
    running interactively at the keyboard; leave the env var unset
    for cron / overnight runs that must remain non-interactive.
    """
    if not doi:
        return None
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        logger.info("[playwright] not installed, skipping")
        return None

    # Attempt 1: headless
    result = _playwright_fetch_once(doi, headless=True)
    if result is not None:
        return result

    # Attempt 2 (opt-in): headed fallback
    if os.environ.get("FETCH_FULLTEXT_HEADED", "").strip() in ("1", "true",
                                                                "yes"):
        logger.info("[playwright] headless failed; "
                    "FETCH_FULLTEXT_HEADED=1 — retrying with visible "
                    "browser. Click the CF challenge if it appears.")
        return _playwright_fetch_once(doi, headless=False)
    return None


# ---------------------------------------------------------------------------
# 7. bioRxiv / medRxiv API (by DOI)
# ---------------------------------------------------------------------------

# bioRxiv migrated to its own Crossref-issued prefix `10.64898/...` in late
# 2025; medRxiv and pre-2025 bioRxiv papers still live on the shared CSHL
# prefix `10.1101/...`. Both prefixes resolve to the same biorxiv.org /
# medrxiv.org content URLs, so the only change needed here is widening the
# guard.
_BIORXIV_DOI_PREFIXES = ("10.1101/", "10.64898/")


def _try_biorxiv(doi: str):
    if not doi or not any(doi.startswith(p) for p in _BIORXIV_DOI_PREFIXES):
        return None
    pdf_url = f"https://www.biorxiv.org/content/{doi}.full.pdf"
    # bioRxiv migrated behind full Cloudflare in 2025; plain `requests`
    # returns 403 stateless. TLS impersonation (curl_cffi) clears CF
    # in ~2 s (probed 2026-05-26 against 10.64898/2026.05.18.724443).
    # Use a browser User-Agent for the same reason.
    status, content = _http_get(pdf_url, binary=True,
                                user_agent="browser",
                                impersonate="chrome124")
    if status != 200 or not content or not content.startswith(b"%PDF"):
        logger.info("[biorxiv] download failed (HTTP %s)", status)
        return None
    # Per-call unique tmpfile (mkstemp) — see _download_pdf for the
    # rationale (DEFERRED.md #4 + the 2026-05-26 test-clobber incident).
    safe = doi.replace("/", "_")
    fd, tmp_path = tempfile.mkstemp(
        suffix=".pdf",
        prefix=f"biorxiv_{safe}_",
        dir=tempfile.gettempdir(),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    tmp = Path(tmp_path)
    text = _extract_pdf_text(str(tmp))
    if not text.strip():
        # Don't leak the downloaded temp PDF when extraction yields nothing.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return {
        "pdf_path": str(tmp),
        "text": text,
        "source": "biorxiv",
        "fetched_from": pdf_url,
    }


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def _detect_abstract(text: str) -> str:
    """Best-effort: pull the abstract paragraph from extracted text."""
    if not text:
        return ""
    # Try common headers
    for marker in ("ABSTRACT\n", "Abstract\n", "SUMMARY\n", "Summary\n"):
        idx = text.find(marker)
        if idx != -1:
            tail = text[idx + len(marker):]
            # take up to the next ALLCAPS header or 3000 chars
            stop = re.search(r"\n[A-Z][A-Z0-9 ]{3,}\n", tail)
            return tail[: stop.start() if stop else 3000].strip()
    return ""


def fetch(paper_id: str, doi: str = "", pdf: str = "", drop_dir: str = ""):
    """Try each source; return result dict or None."""
    pmid, parsed_doi = _parse_paper_id(paper_id)
    doi = doi or parsed_doi or ""
    drop_path = Path(drop_dir or
                     os.environ.get("PAPER_PDF_DROP_DIR") or
                     "~/Downloads").expanduser()
    tried = []
    caps = _capabilities()

    # 1. user-supplied PDF
    if pdf:
        tried.append(f"user-pdf:{pdf}")
        r = _try_user_pdf(pdf, pmid, doi)
        if r:
            return r, tried

    # 2. drop-folder scan
    tried.append(f"drop-folder:{drop_path}")
    r = _try_drop_folder(pmid, doi, drop_path)
    if r:
        return r, tried

    # 3. PMC OA XML
    if pmid:
        tried.append("pmc-oa-xml")
        r = _try_pmc_xml(pmid)
        if r:
            return r, tried

    # 4. EuropePMC XML
    if pmid:
        tried.append("europepmc-xml")
        r = _try_europepmc(pmid)
        if r:
            return r, tried

    # 5. Unpaywall
    if doi:
        tried.append(f"unpaywall:{doi}")
        r = _try_unpaywall(doi)
        if r:
            return r, tried

    # 6. Publisher-specific OA URL patterns (PLOS, eLife, MDPI, Frontiers)
    if doi:
        tried.append(f"publisher_pattern:{doi}")
        r = _try_publisher_pattern(doi)
        if r:
            return r, tried

    # 6b. Generic DOI landing → citation_pdf_url (VPN-aware). Catches
    # paywalled-but-IP-subscribed publishers (Nature, Springer, OUP, CSHL
    # Press, …) when the user is on an institutional VPN. Skips cleanly on
    # Cloudflare-challenged landings (PNAS, Cell, full Wiley, Elsevier).
    if doi:
        tried.append(f"doi-landing:{doi}")
        r = _try_doi_landing(doi)
        if r:
            return r, tried

    # 6c. Playwright-based Cloudflare bypass. Last-resort fetcher for
    # CF-fronted publishers (PNAS, Cell, Wiley, Elsevier, Adv. Sci.).
    # Only record it as genuinely "tried" when playwright is importable —
    # otherwise the failure log falsely claimed the VPN/CF path ran when it
    # was a one-line ImportError no-op.
    if doi:
        # Always call it (it no-ops instantly when playwright is absent),
        # but label the `tried` entry honestly so the failure log doesn't
        # imply the CF-bypass path actually ran when it couldn't.
        if caps["playwright"]:
            tried.append(f"playwright-landing:{doi}")
        else:
            tried.append("playwright-landing:SKIPPED(playwright not installed — "
                         "`pip install playwright && playwright install chromium`)")
        r = _try_playwright_landing(doi)
        if r:
            return r, tried

    # 7. bioRxiv
    if doi:
        tried.append(f"biorxiv:{doi}")
        r = _try_biorxiv(doi)
        if r:
            return r, tried

    return None, tried


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="Fetch paper fulltext")
    parser.add_argument("--paper-id", required=True,
                        help="PMID:xxxx, DOI, or bare PMID")
    parser.add_argument("--doi", default="",
                        help="DOI override (skip if paper-id already gives it)")
    parser.add_argument("--pdf", default="",
                        help="Path to user-supplied PDF (highest priority)")
    parser.add_argument("--drop-dir", default="",
                        help="Override drop-folder path "
                             "(default ~/Downloads or $PAPER_PDF_DROP_DIR)")
    parser.add_argument("--out", default="fulltext.json",
                        help="Output JSON path (also writes NO_FULLTEXT.txt "
                             "on failure into the same directory)")
    args = parser.parse_args()

    pmid, doi_from_id = _parse_paper_id(args.paper_id)
    doi = args.doi or doi_from_id or ""

    result, tried = fetch(args.paper_id, doi=doi, pdf=args.pdf,
                          drop_dir=args.drop_dir)

    out_path = Path(args.out)
    if result is None:
        # write NO_FULLTEXT.txt next to the requested --out
        no_path = out_path.parent / "NO_FULLTEXT.txt"
        no_path.parent.mkdir(parents=True, exist_ok=True)
        # Sharper failure message for the bioRxiv/medRxiv CF case so the
        # user gets an exact URL to manually drop, instead of having to
        # parse the "sources tried" list. Both prefixes (10.1101/, the
        # legacy CSHL one, and 10.64898/, the new bioRxiv-issued one)
        # benefit from the same hint.
        hint = ""
        if any(doi.startswith(p) for p in _BIORXIV_DOI_PREFIXES):
            server = "biorxiv" if "biorxiv" in args.paper_id.lower() \
                or doi.startswith("10.64898/") else "biorxiv"
            # medRxiv DOIs are under 10.1101/ too; distinguish by
            # arxiv_filtered.json's `source` field if the agent passes
            # it via --paper-id. Default to bioRxiv URL pattern.
            hint = (
                f"\nThis paper is on bioRxiv/medRxiv behind Cloudflare. "
                f"To recover quickly:\n"
                f"  1) Open this URL in your browser (CF will let a real "
                f"browser through):\n"
                f"     https://www.{server}.org/content/{doi}.full.pdf\n"
                f"  2) Save the PDF into ~/Downloads/ (or your "
                f"PAPER_PDF_DROP_DIR).\n"
                f"  3) Re-run fetch_fulltext.py with the same args; the "
                f"drop-folder scan will pick it up via DOI-tail match.\n"
                f"\nAlternative: set FETCH_FULLTEXT_HEADED=1 to retry "
                f"with a visible browser window you can click through.\n"
            )
        # Report transport/extractor availability so the user can tell a
        # genuine paywall apart from a missing-dependency no-op.
        caps = _capabilities()
        cap_lines = "\n  ".join(
            f"{k}: {'yes' if v else 'NO'}" for k, v in caps.items())
        extractor_warn = ""
        if not caps["pdf_extractor_available"]:
            extractor_warn = (
                "\n*** NO PDF TEXT EXTRACTOR INSTALLED *** — even a "
                "successfully downloaded PDF cannot be read, so this very "
                "likely explains the failure regardless of VPN/access. "
                "Fix: pip install PyMuPDF  (or brew install poppler).\n")
        no_path.write_text(
            "No fulltext found for paper-id=%s doi=%s.\n"
            "Sources tried (in order):\n  - %s\n"
            "Transport/extractor availability:\n  %s\n%s%s"
            % (args.paper_id, doi, "\n  - ".join(tried),
               cap_lines, extractor_warn, hint)
        )
        print(f"FAIL — no fulltext; wrote {no_path}", file=sys.stderr)
        if not caps["pdf_extractor_available"]:
            print("  (NO PDF text extractor installed — see NO_FULLTEXT.txt)",
                  file=sys.stderr)
        sys.exit(1)

    result.setdefault("pmid", pmid or "")
    result.setdefault("doi", doi)
    result.setdefault("abstract", _detect_abstract(result.get("text", "")))
    result["sources_tried"] = tried

    # Wrap into the canonical schema before writing so generate_note.py's
    # load_fulltext() validation passes. _schemas.Fulltext is the single
    # source of truth for the fulltext.json shape.
    try:
        from _schemas import Fulltext
    except ImportError:
        # Fall back when this script is invoked from outside its dir.
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from _schemas import Fulltext
    known_fields = {k: v for k, v in result.items()
                    if k in Fulltext.__dataclass_fields__}
    ft = Fulltext(**known_fields)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ft.to_dict(), ensure_ascii=False, indent=2))
    print(f"OK — fulltext from {ft.source}: {out_path}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
