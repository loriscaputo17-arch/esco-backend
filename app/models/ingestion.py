"""
Pydantic models for the ingestion pipeline.

Wire formats only — these are the contracts between layers:
  - ClaimedJob:        what `ingestion_claim_next()` RPC returns to the worker
  - FetchedContent:    what each fetcher produces
  - ExtractedDraft:    what the extractor produces (and we send to `ingestion_complete`)
  - ProcessNextResponse: what POST /worker/process-next returns to the cron
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


SourceType = Literal[
    "url_instagram",
    "url_google_maps",
    "url_website",
    "url_other",
    "manual",
    "image",
    "voice",
]

DraftKind = Literal["place", "event", "journey_candidate"]


class ClaimedJob(BaseModel):
    """A single job claimed from the queue by `ingestion_claim_next()`."""
    job_id: UUID
    source_id: UUID
    source_type: SourceType
    raw_input: str
    normalized_url: Optional[str] = None
    city_id: Optional[UUID] = None
    category_hint_id: Optional[UUID] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    attempts: int


class FetchedContent(BaseModel):
    """
    Raw content returned by a fetcher, normalized into a common shape so the
    extractor doesn't care whether it came from Instagram, Maps, or a site.
    """
    source_type: SourceType
    title: Optional[str] = None
    description: Optional[str] = None
    images: list[str] = Field(default_factory=list)
    raw_text: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractedDraft(BaseModel):
    """
    Result of running Gemini extraction over a FetchedContent.

    `payload` is shaped 1:1 like a row of `places` or `events` so that
    `publish_draft` RPC can hand it straight to admin_bulk_import_*.
    """
    kind: DraftKind
    payload: dict[str, Any]
    quality_score: float = Field(ge=0.0, le=1.0)
    ai_notes: Optional[str] = None
    cost_cents: Optional[int] = None
    processor_version: str = "gemini-2.5-flash-lite-v1"


class ProcessNextResponse(BaseModel):
    """Return shape of POST /worker/process-next."""
    idle: bool = False
    job_id: Optional[UUID] = None
    status: Optional[str] = None  # "extracted" | "duplicate" | "failed" | "pending"
    draft_id: Optional[UUID] = None
    error: Optional[str] = None