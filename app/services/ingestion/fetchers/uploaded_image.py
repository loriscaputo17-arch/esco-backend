"""
Uploaded-image fetcher.

raw_input è un path dentro il bucket 'ingestion-uploads' (es. "2026/06/uuid.jpg").
Generiamo una signed URL valida 1h e la mettiamo in FetchedContent.images,
così l'extractor (vision path) può scaricarla come fa per qualsiasi altra
immagine. Il service role bypassa RLS, quindi la signed URL si crea sempre.
"""

from __future__ import annotations

from app.core.supabase_client import get_supabase
from app.models.ingestion import FetchedContent

_BUCKET = "ingestion-uploads"
_SIGNED_URL_TTL_SECONDS = 3600  # 1h, più che sufficiente per un job


def fetch_uploaded_image(storage_path: str) -> FetchedContent:
    sb = get_supabase()

    try:
        signed = sb.storage.from_(_BUCKET).create_signed_url(
            storage_path, _SIGNED_URL_TTL_SECONDS
        )
    except Exception as e:  # noqa: BLE001
        return FetchedContent(
            source_type="image",
            metadata={"storage_path": storage_path, "error": f"signed url failed: {e}"},
        )

    # supabase-py returns dict-like; key varies between versions
    image_url = (
        signed.get("signedURL") or signed.get("signed_url")
        if isinstance(signed, dict) else None
    )
    if not image_url:
        return FetchedContent(
            source_type="image",
            metadata={"storage_path": storage_path, "error": "no signed URL returned"},
        )

    return FetchedContent(
        source_type="image",
        images=[image_url],
        metadata={"storage_path": storage_path},
    )