"""
Google Maps fetcher.

Strategy:
1. Resolve shortlinks (maps.app.goo.gl, goo.gl) via HTTP redirect.
2. Pull the place name and lat/lng straight from the long Maps URL with regex.
3. Enrich with Google Places API v1 Text Search (needs GOOGLE_MAPS_API_KEY).

The regex approach is brittle vs the Maps URL format but it's enough to seed
a good Text Search query. The API call brings the trustworthy data.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

import httpx

from app.core.config import settings
from app.models.ingestion import FetchedContent


_NAME_FROM_PATH = re.compile(r"/place/([^/@]+)", re.IGNORECASE)
_COORDS_IN_URL = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_COORDS_AT = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+),")


def fetch_google_maps(url: str) -> FetchedContent:
    """Resolve a Google Maps URL into a FetchedContent (place candidate)."""
    final_url, _ = _follow_redirects(url)

    name = _extract_name(final_url)
    lat, lng = _extract_coords(final_url)

    enriched: dict = {}
    if name and settings.GOOGLE_MAPS_API_KEY:
        enriched = _places_text_search(name, lat, lng)

    display_name = (enriched.get("displayName") or {}).get("text") or name
    description = (enriched.get("editorialSummary") or {}).get("text")

    images: list[str] = []
    for photo in (enriched.get("photos") or [])[:5]:
        if pname := photo.get("name"):
            images.append(
                f"https://places.googleapis.com/v1/{pname}/media"
                f"?maxWidthPx=1200&key={settings.GOOGLE_MAPS_API_KEY}"
            )

    return FetchedContent(
        source_type="url_google_maps",
        title=display_name,
        description=description,
        images=images,
        raw_text=description,
        metadata={
            "url": final_url,
            "name": name,
            "lat": (enriched.get("location") or {}).get("latitude") or lat,
            "lng": (enriched.get("location") or {}).get("longitude") or lng,
            "address": enriched.get("formattedAddress"),
            "phone": enriched.get("internationalPhoneNumber"),
            "website": enriched.get("websiteUri"),
            "rating": enriched.get("rating"),
            "user_rating_count": enriched.get("userRatingCount"),
            "price_level": enriched.get("priceLevel"),
            "types": enriched.get("types"),
        },
    )


def _follow_redirects(url: str) -> tuple[str, int]:
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        return str(resp.url), resp.status_code
    except httpx.HTTPError:
        return url, 0


def _extract_name(url: str) -> str | None:
    m = _NAME_FROM_PATH.search(url)
    if not m:
        return None
    raw = unquote(m.group(1))
    return raw.replace("+", " ").strip()


def _extract_coords(url: str) -> tuple[float | None, float | None]:
    if m := _COORDS_IN_URL.search(url):
        return float(m.group(1)), float(m.group(2))
    if m := _COORDS_AT.search(url):
        return float(m.group(1)), float(m.group(2))
    return None, None


def _places_text_search(name: str, lat: float | None, lng: float | None) -> dict:
    """Google Places API v1 — Text Search with optional location bias."""
    api_url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": ",".join([
            "places.displayName",
            "places.formattedAddress",
            "places.location",
            "places.internationalPhoneNumber",
            "places.websiteUri",
            "places.rating",
            "places.userRatingCount",
            "places.priceLevel",
            "places.editorialSummary",
            "places.photos",
            "places.types",
        ]),
    }
    body: dict = {"textQuery": name, "languageCode": "it"}
    if lat is not None and lng is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": 500,
            }
        }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(api_url, json=body, headers=headers)
        if resp.status_code != 200:
            return {}
        places = (resp.json() or {}).get("places") or []
        return places[0] if places else {}
    except httpx.HTTPError:
        return {}