"""
Raw-text fetcher.

The admin pasted free text (a description, a flyer transcription, a press
article, an email from a venue). We just wrap it as FetchedContent and
let Gemini reason over it like any other content.
"""

from __future__ import annotations

from app.models.ingestion import FetchedContent


def fetch_raw_text(text: str) -> FetchedContent:
    return FetchedContent(
        source_type="text",
        raw_text=text,
        metadata={"length": len(text)},
    )