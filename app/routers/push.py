"""
Push notifications router.
Receives webhook from Supabase DB trigger when a new notification is inserted.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from app.services.push import send_push_to_user, build_notification_payload
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/push", tags=["push"])


class NotifyRequest(BaseModel):
    """Payload sent by Supabase trigger when a notification is inserted."""
    user_id: str
    kind: str
    payload: dict = {}
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None


@router.post("/notify")
async def notify(
    req: NotifyRequest,
    x_esco_secret: Optional[str] = Header(default=None),
):
    """
    Webhook receiver: when DB inserts a notification, this gets called.
    Authenticates via shared secret header.
    """
    # Verifica secret (proteggere endpoint da chiamate non autorizzate)
    if not settings.PUSH_WEBHOOK_SECRET:
        logger.error("PUSH: missing PUSH_WEBHOOK_SECRET env var")
        raise HTTPException(status_code=500, detail="server_misconfig")
    
    if x_esco_secret != settings.PUSH_WEBHOOK_SECRET:
        logger.warning(f"PUSH: invalid secret from request to user {req.user_id}")
        raise HTTPException(status_code=401, detail="unauthorized")
    
    # Build push content
    title, body, data = build_notification_payload(
        kind=req.kind,
        payload=req.payload,
        entity_type=req.entity_type,
        entity_id=req.entity_id,
    )
    
    if not body:
        logger.info(f"PUSH: skipping empty body for kind={req.kind}")
        return {"ok": True, "skipped": True}
    
    # Invia
    result = await send_push_to_user(
        user_id=req.user_id,
        title=title,
        body=body,
        data=data,
    )
    
    return result