#!/usr/bin/env python3
"""Small, non-blocking health probe for the local WeRSS enhancement feed."""

import os
import urllib.request
import xml.etree.ElementTree as ET


WERSs_URL = os.getenv("RIBAO_WERSS_URL", "http://127.0.0.1:8001/feed/all.xml")
MAX_BYTES = 2 * 1024 * 1024


def probe_health(url=WERSs_URL):
    """Check that the loopback RSS endpoint is available and returns XML.

    This intentionally performs no login, no refresh, and no data mutation.
    The daily workflow uses the result only to include or gracefully exclude
    an enhancement source.
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "beijing-daily-brief/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            return {"status": "unavailable", "reason": "feed_too_large"}
        root = ET.fromstring(body)
        if root.tag.lower().split("}")[-1] not in {"rss", "feed"}:
            return {"status": "unavailable", "reason": "not_rss_or_atom"}
        return {"status": "healthy", "reason": "rss_ready"}
    except Exception as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
