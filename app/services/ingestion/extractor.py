"""
LLM extractor.

If the fetched content includes images, we use Gemini's multimodal input
to let the model READ the image (flyers, menus, etc). For text-only content
or other LLM providers, we fall back to the standard text-only path via
the shared get_llm() factory.
"""

from __future__ import annotations

import json
import logging
from io import BytesIO

import httpx

from app.core.config import settings
from app.models.ingestion import ExtractedDraft, FetchedContent
from app.services.llm import get_llm

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """Sei un editor di ESCO, una city-companion editoriale italiana per la fascia 25-45 urban-creative.

VOCE ESCO:
- Asciutta, sensoriale, almeno 1 tip pratico in ogni descrizione (es. "arriva prima delle 19", "chiedi il tavolo dietro").
- Niente "iconico", "esperienza unica", "must-try", "imperdibile", emoji, esclamativi.
- Lingua: italiano. Apostrofi curly (l'estate, non l\\'estate).

═══════════════════════════════════════════════════════════════════
REGOLE FERREE — DA RISPETTARE PRIMA DI TUTTO IL RESTO
═══════════════════════════════════════════════════════════════════

1. ESTRAI SOLO DAL CONTENT FORNITO (testo + immagini allegate). Non hai accesso a internet, a Google, a tue memorie su locali o eventi specifici. Se il content non lo dice, NON LO SAI.

2. LE IMMAGINI SONO FONTE PRIMARIA. Se è un flyer, leggi titolo, data, artisti, orario, venue, dress-code DA QUELLA. La caption del post può essere vuota o povera: in quel caso il flyer È il content.

3. NON FARE "ARRICCHIMENTO". Mai mettere lat/lng, indirizzi, telefoni, prezzi, popolarità, orari che NON sono nel content fornito (testo o immagini). Lat/lng vanno solo se presenti in METADATA. Mai inventare coordinate "credibili".

4. IL NOME del place o il titolo dell'event deve apparire LETTERALMENTE nel content (caption, raw_text, metadata, O nell'immagine). Se il content menziona "Arca" e tu scrivi "La Prosciutteria", stai allucinando: errore grave.

5. DECIDI place vs event in base al content:
   - event se c'è una DATA esplicita (oggi, domani, 05.07.26, weekend del...) o l'oggetto è un'occorrenza (DJ set, mostra, opening, festival, "Sunday Remedy")
   - place se l'oggetto è un luogo permanente senza una data specifica

6. QUALITY_SCORE è la tua confidenza, NON la qualità del posto:
   - 0.80+ solo se hai nome chiaro + descrizione + almeno 2-3 campi factual (address O lat/lng O phone O website O artisti)
   - 0.50-0.79 se hai nome + descrizione ma pochi dati factual
   - 0.20-0.49 se il content è povero — IN QUESTO CASO genera descrizione MINIMA, non inventare contesto
   - <0.20 se il content non identifica chiaramente un place o event

7. AI_NOTES è per il revisore umano. Sii onesto: "estratto dal flyer, manca lat/lng", "caption vuota, info solo dall'immagine", "data sospetta", "potrebbe essere collaborazione con X".

═══════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════

Solo JSON valido. No markdown. No fence. Schema:

{
  "kind": "place" | "event",
  "payload": { ... },
  "quality_score": 0.0..1.0,
  "ai_notes": str
}

Se kind == "place", payload contiene SOLO questi campi (null se non noto):
{
  "name": str,
  "slug": str,
  "description": str,
  "address": str | null,
  "lat": number | null,
  "lng": number | null,
  "price_level": 1 | 2 | 3 | 4 | null,
  "popularity": int,
  "website_url": str | null,
  "phone": str | null,
  "instagram_handle": str | null,
  "whatsapp_number": str | null,
  "booking_url": str | null,
  "booking_email": str | null
}

Se kind == "event", payload contiene SOLO questi campi:
{
  "title": str,
  "slug": str,
  "description": str,
  "venue_name": str,
  "start_at": str | null,
  "end_at": str | null,
  "price_min": number | null,
  "price_max": number | null,
  "lat": number | null,
  "lng": number | null,
  "popularity": int,
  "website_url": str | null,
  "phone": str | null,
  "ticket_url": str | null,
  "booking_email": str | null
}
"""


def extract_draft(content: FetchedContent, city_name: str | None = None) -> ExtractedDraft:
    """
    Run LLM extraction. Uses Gemini vision when images are present, falls
    back to text-only otherwise (or when vision fails for any reason).
    """
    user_prompt = _build_user_prompt(content, city_name)

    use_vision = (
        bool(content.images)
        and settings.LLM_PROVIDER.lower() == "gemini"
        and bool(settings.GEMINI_API_KEY)
    )

    result: dict | None = None
    if use_vision:
        try:
            result = _extract_with_gemini_vision(user_prompt, content.images)
            logger.info("extracted via vision: %d image(s)", len(content.images))
        except Exception as e:  # noqa: BLE001
            logger.warning("vision path failed, falling back to text-only: %s", e)
            result = None

    if result is None:
        llm = get_llm()
        result = llm.generate_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.4,
        )

    kind = result.get("kind")
    if kind not in ("place", "event"):
        raise ValueError(f"LLM returned invalid kind: {kind!r}")

    payload = result.get("payload")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("LLM returned empty or invalid payload")

    if content.images and not payload.get("cover_image"):
        payload["cover_image"] = content.images[0]

    score = result.get("quality_score")
    try:
        quality_score = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        quality_score = 0.5

    processor_version = f"{settings.LLM_PROVIDER}:" + (
        settings.LLM_MODEL_GEMINI if settings.LLM_PROVIDER.lower() == "gemini"
        else settings.LLM_MODEL_CLAUDE
    )
    if use_vision and result is not None:
        processor_version += ":vision"

    return ExtractedDraft(
        kind=kind,
        payload=payload,
        quality_score=quality_score,
        ai_notes=result.get("ai_notes"),
        processor_version=processor_version,
    )


# --------------------------------------------------------------- helpers ---


def _extract_with_gemini_vision(user_prompt: str, image_urls: list[str]) -> dict:
    """Direct Gemini multimodal call. Downloads up to 3 images as bytes."""
    import google.generativeai as genai

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name=settings.LLM_MODEL_GEMINI,
        generation_config={"response_mime_type": "application/json"},
    )

    image_parts = []
    for url in image_urls[:3]:
        part = _download_image_part(url)
        if part is not None:
            image_parts.append(part)

    if not image_parts:
        raise RuntimeError("no images could be downloaded")

    full_prompt = (
        _SYSTEM_PROMPT
        + "\n\n---\n\n"
        + user_prompt
        + "\n\nLEGGI ATTENTAMENTE LE IMMAGINI ALLEGATE — sono la fonte primaria per flyer di eventi, locandine, menu."
    )

    response = model.generate_content(
        [full_prompt, *image_parts],
        generation_config={
            "temperature": 0.4,
            "response_mime_type": "application/json",
        },
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini vision returned empty response")
    return json.loads(text)


def _download_image_part(url: str) -> dict | None:
    """Fetch an image URL, return a Gemini-compatible part dict, or None on failure."""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        ctype = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            return None
        # Cap at ~4 MB to avoid blowing the model context
        data = resp.content[: 4 * 1024 * 1024]
        return {"mime_type": ctype, "data": data}
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("image download failed (%s): %s", url, e)
        return None


def _build_user_prompt(content: FetchedContent, city_name: str | None) -> str:
    parts: list[str] = [f"SOURCE TYPE: {content.source_type}"]

    if city_name:
        parts.append(f"CITY HINT (l'admin ha selezionato): {city_name}")

    if content.title:
        parts.append(f"TITLE: {content.title}")

    if content.description:
        parts.append(f"DESCRIPTION/CAPTION:\n{content.description}")

    if content.raw_text and content.raw_text != content.description:
        parts.append(f"RAW TEXT:\n{content.raw_text[:3000]}")

    if content.images:
        parts.append(
            f"IMAGES: {len(content.images)} immagine/i allegata/e — leggile per estrarre dati dal flyer/locandina."
        )

    if content.metadata:
        meta_lines = []
        for k, v in content.metadata.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            meta_lines.append(f"  {k}: {v}")
        if meta_lines:
            parts.append("METADATA:\n" + "\n".join(meta_lines))

    return "\n\n".join(parts)