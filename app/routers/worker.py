"""
Worker router for Content OS ingestion.

POST /worker/process-next
    Processes pending jobs in a loop until queue is idle or budget exhausted.
    Default budget: 25 seconds wall time, 20 jobs max.
    Called by Supabase pg_cron (every minute) and on-demand by the CMS via
    /api/trigger-worker on Next.js side.

GET /worker/health
    Lightweight readiness probe.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, status

from app.core.config import settings
from app.core.supabase_client import get_supabase
from app.models.ingestion import ClaimedJob, ProcessNextResponse
from app.services.ingestion.extractor import extract_draft
from app.services.ingestion.fetchers.website import fetch_website
from app.services.ingestion.resolver import enrich_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/worker", tags=["worker"])

_WORKER_ID = f"render:{os.getenv('RENDER_INSTANCE_ID') or socket.gethostname()}"
_DEFAULT_MAX_SECONDS = 25
_DEFAULT_MAX_JOBS = 20


@router.get("/health")
def health() -> dict[str, Any]:
    """Readiness probe."""
    return {"ok": True, "worker_id": _WORKER_ID}


@router.post("/process-next", response_model=ProcessNextResponse)
def process_next(
    x_worker_secret: str | None = Header(default=None, alias="X-Worker-Secret"),
    max_seconds: int = Query(default=_DEFAULT_MAX_SECONDS, ge=1, le=55),
    max_jobs: int = Query(default=_DEFAULT_MAX_JOBS, ge=1, le=100),
) -> ProcessNextResponse:
    """Process queued jobs in a loop. Returns the last job's outcome."""
    _require_secret(x_worker_secret)

    sb = get_supabase()
    start = time.monotonic()
    processed = 0
    last_result: ProcessNextResponse | None = None

    while processed < max_jobs and (time.monotonic() - start) < max_seconds:
        result = _process_one(sb)
        if result.idle:
            break
        last_result = result
        processed += 1
        logger.info("batch progress: %d job(s) processed", processed)

    if processed == 0:
        return ProcessNextResponse(idle=True)

    logger.info("batch done: %d job(s) in %.1fs", processed, time.monotonic() - start)
    return last_result or ProcessNextResponse(idle=True)


# --------------------------------------------------------- single-job worker ---


def _process_one(sb) -> ProcessNextResponse:
    """Claim and process exactly one job. Returns idle=True if queue empty."""
    claim_resp = sb.rpc("ingestion_claim_next", {
        "p_worker_id": _WORKER_ID,
        "p_lock_minutes": 5,
    }).execute()

    rows = claim_resp.data or []
    if not rows:
        return ProcessNextResponse(idle=True)

    job = ClaimedJob.model_validate(rows[0])
    logger.info("claimed job=%s source=%s attempts=%d",
                job.job_id, job.source_type, job.attempts)

    try:
        content = _dispatch_fetch(job)

        sb.table("ingestion_jobs").update({
            "fetched_content": content.model_dump(mode="json"),
        }).eq("id", str(job.job_id)).execute()

        city_name, city_center = _lookup_city(sb, job.city_id)
        draft = extract_draft(content, city_name=city_name)

        # --- resolver: il modello ha dato il candidato, OSM dà le coordinate ---
        try:
            payload, note = enrich_payload(
                draft.payload, draft.kind, city_name, city_center
            )
            draft.payload = payload
            if note:
                draft.ai_notes = f"{draft.ai_notes or ''}\n{note}".strip()
        except Exception as e:  # noqa: BLE001
            # Il geocoding non deve MAI far fallire un job: il draft senza
            # coordinate è comunque revisionabile, un job perso no.
            logger.warning("resolver fallito su job %s: %s", job.job_id, e)
    except Exception as e:  # noqa: BLE001
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

    hint = getattr(job, "category_hint_id", None)
    if hint and draft_id:
        try:
            sb.table("content_drafts").update(
                {"category_id": str(hint)}
            ).eq("id", draft_id).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("categoria non applicata al draft %s: %s", draft_id, e)

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
    return fetch_website(job.raw_input)

def _lookup_city(sb, city_id) -> tuple[str | None, tuple[float, float] | None]:
    """(nome, (lat, lng) del centro) — entrambi opzionali."""
    if not city_id:
        return None, None
    try:
        resp = (
            sb.table("cities")
            .select("name, center_lat, center_lng, lat, lng")
            .eq("id", str(city_id))
            .single()
            .execute()
        )
        row = resp.data or {}
        name = row.get("name")
        lat = row.get("center_lat") if row.get("center_lat") is not None else row.get("lat")
        lng = row.get("center_lng") if row.get("center_lng") is not None else row.get("lng")
        center = (float(lat), float(lng)) if lat is not None and lng is not None else None
        return name, center
    except Exception:  # noqa: BLE001
        return None, None