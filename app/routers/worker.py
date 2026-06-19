"""
Worker router for Content OS ingestion.

POST /worker/process-next
    Called by Supabase pg_cron every minute.
    Claims one pending job, runs fetch + extract, writes a content_draft.
    Protected by X-Worker-Secret header.

GET /worker/health
    Lightweight readiness probe.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status

from app.core.config import settings
from app.core.supabase_client import get_supabase
from app.models.ingestion import ClaimedJob, ProcessNextResponse
from app.services.ingestion.classifier import classify_url
from app.services.ingestion.extractor import extract_draft
from app.services.ingestion.fetchers.website import fetch_website

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/worker", tags=["worker"])

_WORKER_ID = f"render:{os.getenv('RENDER_INSTANCE_ID') or socket.gethostname()}"


@router.get("/health")
def health() -> dict[str, Any]:
    """Readiness probe for the cron."""
    return {"ok": True, "worker_id": _WORKER_ID}


@router.post("/process-next", response_model=ProcessNextResponse)
def process_next(
    x_worker_secret: str | None = Header(default=None, alias="X-Worker-Secret"),
) -> ProcessNextResponse:
    """Claim and process one pending ingestion job."""
    _require_secret(x_worker_secret)

    sb = get_supabase()

    # 1. Claim next job (atomic; recovers stuck jobs older than 5 min)
    claim_resp = sb.rpc("ingestion_claim_next", {
        "p_worker_id": _WORKER_ID,
        "p_lock_minutes": 5,
    }).execute()

    rows = claim_resp.data or []
    if not rows:
        return ProcessNextResponse(idle=True)

    job = ClaimedJob.model_validate(rows[0])
    logger.info(
        "claimed job=%s source=%s attempts=%d",
        job.job_id, job.source_type, job.attempts,
    )

    # 2. Dispatch + extract
    try:
        content = _dispatch_fetch(job)
        sb.table("ingestion_jobs").update({
            "fetched_content": content.model_dump(mode="json"),
        }).eq("id", str(job.job_id)).execute()
        city_name = _lookup_city_name(sb, job.city_id)
        draft = extract_draft(content, city_name=city_name)
    except Exception as e:  # noqa: BLE001 — we want to catch anything from third-party APIs
        logger.exception("job %s failed during fetch/extract", job.job_id)
        sb.rpc("ingestion_fail", {
            "p_job_id": str(job.job_id),
            "p_error": str(e)[:1000],
        }).execute()
        return ProcessNextResponse(
            job_id=job.job_id,
            status="failed",
            error=str(e)[:500],
        )

    # 3. Complete (writes content_draft, closes job)
    complete_resp = sb.rpc("ingestion_complete", {
        "p_job_id": str(job.job_id),
        "p_kind": draft.kind,
        "p_payload": draft.payload,
        "p_quality_score": draft.quality_score,
        "p_ai_notes": draft.ai_notes,
        "p_cost_cents": draft.cost_cents,
        "p_processor_version": draft.processor_version,
    }).execute()

    draft_id = complete_resp.data
    logger.info("job %s -> draft %s", job.job_id, draft_id)

    return ProcessNextResponse(
        job_id=job.job_id,
        status="extracted",
        draft_id=UUID(draft_id) if draft_id else None,
    )


# --------------------------------------------------------------- helpers ---


def _require_secret(provided: str | None) -> None:
    expected = settings.INGESTION_WORKER_SECRET
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INGESTION_WORKER_SECRET not configured on server",
        )
    if not provided or provided != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid worker secret",
        )

def _dispatch_fetch(job: ClaimedJob):
    """Pick the right fetcher based on source_type."""
    if job.source_type == "image":
        from app.services.ingestion.fetchers.uploaded_image import fetch_uploaded_image
        return fetch_uploaded_image(job.raw_input)

    if job.source_type == "text":
        from app.services.ingestion.fetchers.raw_text import fetch_raw_text
        return fetch_raw_text(job.raw_input)

    # Tutto il resto (link sito, url generici, legacy Instagram/Maps) → trafilatura
    return fetch_website(job.raw_input)

def _lookup_city_name(sb, city_id) -> str | None:
    """One-shot lookup of city.name for the LLM prompt; None on miss."""
    if not city_id:
        return None
    try:
        resp = sb.table("cities").select("name").eq("id", str(city_id)).single().execute()
        return (resp.data or {}).get("name")
    except Exception:  # noqa: BLE001
        return None