"""
Generic website fetcher.

Uses trafilatura for high-quality extraction of the main text plus
metadata (title, author, date, sitename, lead image). Falls back to
returning whatever we got if trafilatura can't find a body.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import trafilatura

from app.models.ingestion import FetchedContent


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
}


def fetch_website(url: str) -> FetchedContent:
    """Fetch a generic web page and normalize to FetchedContent."""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers=_HEADERS) as client:
            resp = client.get(url)
        if resp.status_code >= 400:
            return FetchedContent(
                source_type="url_website",
                metadata={"url": url, "error": f"http {resp.status_code}"},
            )
        html = resp.text
        final_url = str(resp.url)
    except httpx.HTTPError as e:
        return FetchedContent(
            source_type="url_website",
            metadata={"url": url, "error": f"fetch failed: {e}"},
        )

    extracted_raw = trafilatura.extract(
        html,
        output_format="json",
        with_metadata=True,
        include_images=True,
        include_links=False,
        favor_precision=True,
    )

    if not extracted_raw:
        return FetchedContent(
            source_type="url_website",
            metadata={"url": final_url, "error": "no content extracted"},
        )

    data: dict[str, Any] = json.loads(extracted_raw)
    image = data.get("image")

    return FetchedContent(
        source_type="url_website",
        title=data.get("title"),
        description=data.get("description"),
        images=[image] if image else [],
        raw_text=data.get("text"),
        metadata={
            "url": final_url,
            "author": data.get("author"),
            "date": data.get("date"),
            "sitename": data.get("sitename"),
            "language": data.get("language"),
        },
    )