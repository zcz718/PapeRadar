#!/usr/bin/env python3
"""Shared JSON-over-HTTP fetch helper for paperadar source adapters.

Consolidates the near-identical retry / back-off / HTTP-429 logic that the
OpenAlex, Crossref, and CORE adapters each carried as a private `_fetch_json`.
Those three differ only in their log label and (for CORE) an Authorization
header, so they now delegate here.

bioRxiv and PubMed deliberately keep their own fetchers: bioRxiv has a
fail-loud path that reports an un-cleared 429 to stderr, and PubMed drives the
NCBI E-utilities pair (esearch/efetch) under its own rate-limit budget.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import requests  # availability probe; re-imported locally where used
    _USE_REQUESTS = True
except ImportError:  # pragma: no cover - urllib fallback
    _USE_REQUESTS = False

DEFAULT_UA = "paperadar/1.0"


def fetch_json(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: int = 30,
    retries: int = 3,
    label: str = "http",
    rate_limit_pause: int = 5,
) -> Optional[object]:
    """GET ``url`` and parse JSON; return the decoded object or ``None``.

    On HTTP 429 the call sleeps ``rate_limit_pause * (attempt + 1)`` seconds and
    retries without consuming an extra back-off step; other errors use
    exponential back-off (``2 ** attempt``). After ``retries`` failures it logs a
    single ``[label] fetch error`` line and returns ``None`` so the caller can
    drop the source and leave the rest of the pipeline unaffected.
    """
    hdrs = {"User-Agent": DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries):
        try:
            if _USE_REQUESTS:
                import requests
                resp = requests.get(url, timeout=timeout, headers=hdrs)
                if resp.status_code == 429:
                    time.sleep(rate_limit_pause * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            else:  # pragma: no cover - urllib fallback
                import urllib.request
                rq = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(rq, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error("[%s] fetch error (%s): %s", label, url, e)
    return None
