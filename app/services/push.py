"""
Push notifications service via Expo Push API.
Handles fan-out: 1 notification -> N device tokens -> Expo HTTP API.
"""
import logging
from typing import Optional

import httpx

from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_TIMEOUT = 15.0  # seconds


async def send_push_to_user(
    user_id: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> dict:
    """
    Fetch all push tokens for a user and send the same notification to all devices.
    
    Returns: {"ok": bool, "sent": int, "failed": int, "details": [...]}
    """
    supabase = get_supabase()
    
    # Fetch tutti i token attivi dell'utente
    resp = supabase.table("push_tokens").select("expo_push_token, platform").eq("user_id", user_id).execute()
    tokens_rows = resp.data or []
    
    if not tokens_rows:
        logger.info(f"PUSH: no tokens for user {user_id}")
        return {"ok": True, "sent": 0, "failed": 0, "details": []}

    tokens = [r["expo_push_token"] for r in tokens_rows]
    
    # Build messages array per Expo
    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "data": data or {},
            "sound": "default",
            "priority": "high",
            "channelId": "default",  # Android only
        }
        for token in tokens
    ]
    
    # Invia
    try:
        async with httpx.AsyncClient(timeout=EXPO_TIMEOUT) as client:
            response = await client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                },
            )
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError as e:
        logger.error(f"PUSH: HTTP error sending to user {user_id}: {e}")
        return {"ok": False, "sent": 0, "failed": len(tokens), "error": str(e)}
    
    # Parse risposta Expo
    data_arr = result.get("data", [])
    sent = sum(1 for r in data_arr if r.get("status") == "ok")
    failed = sum(1 for r in data_arr if r.get("status") == "error")
    
    # Logging errori per debug
    for i, r in enumerate(data_arr):
        if r.get("status") == "error":
            logger.warning(
                f"PUSH: token error for user {user_id}: "
                f"token={tokens[i][:20]}... details={r.get('details')}"
            )
            # Token "DeviceNotRegistered" → rimuovi dal DB
            if r.get("details", {}).get("error") == "DeviceNotRegistered":
                try:
                    supabase.table("push_tokens").delete().eq("expo_push_token", tokens[i]).execute()
                    logger.info(f"PUSH: removed dead token {tokens[i][:20]}...")
                except Exception as ex:
                    logger.error(f"PUSH: failed to remove dead token: {ex}")
    
    logger.info(f"PUSH: user {user_id} sent={sent} failed={failed}")
    return {
        "ok": True,
        "sent": sent,
        "failed": failed,
        "details": data_arr,
    }


def build_notification_payload(kind: str, payload: dict, entity_type: Optional[str], entity_id: Optional[str]) -> tuple[str, str, dict]:
    """
    Given a notification kind + payload (from DB), build (title, body, data) for push.
    Stays in sync with the in-app rendering.
    """
    actor = payload.get("actor_nickname") or "Someone"
    p = payload or {}
    
    title = "ESCO"
    body = ""
    data = {"kind": kind}
    if entity_type:
        data["entity_type"] = entity_type
    if entity_id:
        data["entity_id"] = entity_id
    
    if kind == "follow":
        title = f"@{actor}"
        body = "started following you"
    elif kind == "list_saved":
        title = f"@{actor}"
        body = f"saved your list \"{p.get('list_name', '')}\""
    elif kind == "place_noted":
        title = f"@{actor}"
        body = f"noted on {p.get('place_name', 'a place')} you saved"
    elif kind == "system_journey":
        title = "New journey"
        body = p.get("journey_headline") or p.get("journey_title") or ""
    else:
        title = "ESCO"
        body = "Something happened in your city."
    
    return title, body, data