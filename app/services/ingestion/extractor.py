"""
LLM extractor.

Takes a FetchedContent (whatever the source was), asks the configured LLM
to extract either a place or an event in ESCO voice, returns an
ExtractedDraft ready to feed to `ingestion_complete()` RPC.

The payload shape matches public.places / public.events rows exactly so
that publish_draft → admin_bulk_import_* works without remapping.
"""

from __future__ import annotations

from app.core.config import settings
from app.models.ingestion import ExtractedDraft, FetchedContent
from app.services.llm import get_llm


_SYSTEM_PROMPT = """Sei un editor di ESCO, una city-companion editoriale italiana per la fascia 25-45 urban-creative.

VOCE ESCO:
- Asciutta, sensoriale, almeno 1 tip pratico in ogni descrizione (es. "arriva prima delle 19", "chiedi il tavolo dietro").
- Niente "iconico", "esperienza unica", "must-try", "imperdibile", emoji, esclamativi.
- Lingua: italiano. Apostrofi curly (l'estate, non l\\'estate).

Dato un contenuto grezzo (post Instagram, scheda Google Maps, pagina web), estrai UN place O UN event — decidi tu in base al contenuto. Un place è un luogo fisico permanente; un event è un'occorrenza con data/ora.

OUTPUT: solo JSON valido. Niente markdown. Niente fence. Schema:

{
  "kind": "place" | "event",
  "payload": { ... },
  "quality_score": 0.0..1.0,
  "ai_notes": str | null
}

Se kind == "place", payload deve avere SOLO questi campi (omettere o mettere null se non noti):
{
  "name": str,
  "slug": str,                       // lowercase-hyphenated-include-city, es. "al-baretto-milano"
  "description": str,                // 60-150 parole, voce ESCO, 1 tip pratico minimo
  "address": str | null,
  "lat": number | null,
  "lng": number | null,
  "price_level": 1 | 2 | 3 | 4 | null,
  "popularity": int,                 // 0..100. Famoso intl 85-95, locale 60-80, niche 40-60
  "website_url": str | null,
  "phone": str | null,               // formato internazionale +39...
  "instagram_handle": str | null,    // senza @
  "whatsapp_number": str | null,
  "booking_url": str | null,
  "booking_email": str | null
}

Se kind == "event", payload deve avere SOLO questi campi:
{
  "title": str,
  "slug": str,                       // includi la data, es. "marco-carola-hi-2026-07-22"
  "description": str,                // 80-200 parole, voce ESCO, 1 tip pratico minimo
  "venue_name": str,
  "start_at": str | null,            // ISO 8601 con timezone, es. "2026-07-22T23:00:00+02:00"
  "end_at": str | null,
  "price_min": number | null,
  "price_max": number | null,
  "lat": number | null,
  "lng": number | null,
  "popularity": int,                 // 0..100
  "website_url": str | null,
  "phone": str | null,
  "ticket_url": str | null,
  "booking_email": str | null
}

REGOLE FERREE:
1. Non inventare dati. Se non sai un campo, metti null. quality_score riflette la tua confidenza.
2. quality_score: 0.8+ se foto + dati ben chiari; 0.5-0.7 se manca address o lat/lng; <0.5 se incerto.
3. ai_notes: 1 riga in italiano per il revisore (es. "manca lat/lng", "data sospetta", "potrebbe essere duplicato di X").
4. slug: lowercase, hyphenated, niente accenti, includi città/data se nota.
5. Description in italiano, prima persona plurale evitata, presente semplice, voce ESCO.
"""


def extract_draft(content: FetchedContent, city_name: str | None = None) -> ExtractedDraft:
    """
    Run LLM extraction over fetched content.

    Raises ValueError if the LLM returns something malformed; the worker
    catches and marks the job failed (with retry up to max_attempts).
    """
    llm = get_llm()
    user_prompt = _build_user_prompt(content, city_name)

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

    # If we have an image from the fetcher and the payload didn't include one, inject it
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

    return ExtractedDraft(
        kind=kind,
        payload=payload,
        quality_score=quality_score,
        ai_notes=result.get("ai_notes"),
        processor_version=processor_version,
    )


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
        parts.append(f"IMAGES: {len(content.images)} disponibili (prima: {content.images[0]})")

    if content.metadata:
        meta_lines = []
        for k, v in content.metadata.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            meta_lines.append(f"  {k}: {v}")
        if meta_lines:
            parts.append("METADATA:\n" + "\n".join(meta_lines))

    return "\n\n".join(parts)