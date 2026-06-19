"""
URL classifier for Content OS ingestion.

Given a raw URL string, returns the source_type that the worker will use
to dispatch to the right fetcher. Pure function — no I/O, no exceptions
that escape, fully testable.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# instagram.com, www.instagram.com, m.instagram.com, instagr.am
_INSTAGRAM_HOSTS = re.compile(
    r"^(?:www\.|m\.)?instagram\.com$|^instagr\.am$",
    re.IGNORECASE,
)

# google.com/maps, www.google.com/maps, maps.google.com, maps.app.goo.gl, goo.gl/maps
_GOOGLE_MAPS_HOSTS = re.compile(
    r"^(?:www\.)?google\.[a-z.]+$|^maps\.google\.[a-z.]+$|^maps\.app\.goo\.gl$|^goo\.gl$",
    re.IGNORECASE,
)
_GOOGLE_MAPS_PATH = re.compile(r"^/maps(/|$)", re.IGNORECASE)


def classify_url(raw_input: str) -> str:
    """
    Classify a raw URL into a source_type known to ingestion_sources.

    Returns one of:
        - "url_instagram"
        - "url_google_maps"
        - "url_website"
        - "url_other"  (input not parseable as a URL)
    """
    if not raw_input or not isinstance(raw_input, str):
        return "url_other"

    candidate = raw_input.strip()
    if not candidate:
        return "url_other"

    # If the user pasted "instagram.com/x", urlparse needs a scheme
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return "url_other"

    host = (parsed.hostname or "").lower()
    if not host:
        return "url_other"

    if _INSTAGRAM_HOSTS.match(host):
        return "url_instagram"

    if _GOOGLE_MAPS_HOSTS.match(host):
        # goo.gl / maps.app.goo.gl are always Maps shortlinks
        if host.endswith("goo.gl") or host == "maps.app.goo.gl":
            return "url_google_maps"
        # google.com is Maps only when path starts with /maps
        if _GOOGLE_MAPS_PATH.match(parsed.path or ""):
            return "url_google_maps"
        return "url_website"

    return "url_website"