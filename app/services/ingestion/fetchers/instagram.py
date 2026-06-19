"""
Instagram fetcher via RapidAPI.

Why not oEmbed/Graph API: requires an approved Meta app and takes weeks.
Why not instaloader: triggers IP bans from Render's egress addresses.
RapidAPI brokers shifting providers behind a stable interface for ~5-10$/1k.

The response shape varies per provider; we normalize using the most common
fields. If you switch provider and the keys change, adjust _normalize().
"""

from __future__ import annotations

import re

import httpx

from app.core.config import settings
from app.models.ingestion import FetchedContent


_SHORTCODE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")
_HASHTAG = re.compile(r"#(\w+)")


def fetch_instagram(url: str) -> FetchedContent:
    """Fetch an Instagram post/reel/IGTV and normalize to FetchedContent."""
    if not settings.RAPIDAPI_KEY:
        return FetchedContent(
            source_type="url_instagram",
            metadata={"url": url, "error": "RAPIDAPI_KEY not configured"},
        )

    shortcode = _extract_shortcode(url)
    if not shortcode:
        return FetchedContent(
            source_type="url_instagram",
            metadata={"url": url, "error": "could not extract shortcode"},
        )

    api_url = f"https://{settings.RAPIDAPI_INSTAGRAM_HOST}{settings.RAPIDAPI_INSTAGRAM_PATH}"
    headers = {
        "X-RapidAPI-Key": settings.RAPIDAPI_KEY,
        "X-RapidAPI-Host": settings.RAPIDAPI_INSTAGRAM_HOST,
    }
    params = {"code_or_id_or_url": url}

    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.get(api_url, params=params, headers=headers)
        if resp.status_code != 200:
            return FetchedContent(
                source_type="url_instagram",
                metadata={
                    "url": url,
                    "shortcode": shortcode,
                    "error": f"rapidapi http {resp.status_code}",
                    "body": resp.text[:300],
                },
            )
        data = resp.json() or {}
    except (httpx.HTTPError, ValueError) as e:
        return FetchedContent(
            source_type="url_instagram",
            metadata={"url": url, "shortcode": shortcode, "error": f"fetch failed: {e}"},
        )

    return _normalize(data, url, shortcode)


def _extract_shortcode(url: str) -> str | None:
    m = _SHORTCODE.search(url)
    return m.group(1) if m else None


def _normalize(data: dict, url: str, shortcode: str) -> FetchedContent:
    """Map a provider-specific response into FetchedContent."""
    # Unwrap if wrapped (some providers use {"data": {...}})
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    caption_raw = data.get("caption")
    caption = (
        caption_raw.get("text") if isinstance(caption_raw, dict)
        else caption_raw or data.get("title")
    )

    images: list[str] = []
    for key in ("display_url", "thumbnail_url", "image_url", "image_versions2"):
        if val := data.get(key):
            if isinstance(val, str):
                images.append(val)
            elif isinstance(val, dict) and (c := val.get("candidates")):
                if c and (u := c[0].get("url")):
                    images.append(u)
    if carousel := data.get("carousel_media") or data.get("edge_sidecar_to_children", {}).get("edges"):
        for item in carousel[:5]:
            node = item.get("node") if isinstance(item.get("node"), dict) else item
            if u := node.get("display_url") or node.get("image_url"):
                images.append(u)

    owner = data.get("owner") or data.get("user") or {}
    location = data.get("location") or {}

    return FetchedContent(
        source_type="url_instagram",
        title=owner.get("username") or data.get("username"),
        description=caption,
        images=list(dict.fromkeys(images))[:8],  # dedup, cap at 8
        raw_text=caption,
        metadata={
            "url": url,
            "shortcode": shortcode,
            "username": owner.get("username") or data.get("username"),
            "owner_full_name": owner.get("full_name"),
            "is_verified": owner.get("is_verified"),
            "location_name": location.get("name") if isinstance(location, dict) else None,
            "location_lat": location.get("lat") if isinstance(location, dict) else None,
            "location_lng": location.get("lng") if isinstance(location, dict) else None,
            "like_count": data.get("like_count") or data.get("edge_media_preview_like", {}).get("count"),
            "comment_count": data.get("comment_count"),
            "taken_at": data.get("taken_at"),
            "hashtags": _HASHTAG.findall(caption or ""),
        },
    )