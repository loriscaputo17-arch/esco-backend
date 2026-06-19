"""
Composizione AI dei journey.
Pesca i place/event disponibili dal DB, costruisce il prompt,
chiama l'LLM, valida l'output, scrive su DB.
"""
import json
import uuid
from typing import Any
from app.core.supabase_client import get_supabase
from app.services.llm import get_llm
from app.models.journey import (
    JourneyComposeRequest,
    AIJourneyComposition,
)


# ─────────────────────────────────────────────
# 1. CONTESTO: fetcha place/event disponibili
# ─────────────────────────────────────────────

def fetch_city_inventory(city_id: str) -> dict[str, Any]:
    """
    Recupera tutti i place + event upcoming della città.
    Questi sono i 'building blocks' che l'AI può scegliere.
    """
    sb = get_supabase()

    places = sb.table("places").select(
        "id, name, description, address, lat, lng, price_level, cover_image, popularity"
    ).eq("city_id", city_id).execute().data

    events = sb.table("events").select(
        "id, title, description, venue_name, start_at, price_min, price_max, lat, lng, popularity, cover_image"
    ).eq("city_id", city_id).gte("start_at", "now()").order("start_at").execute().data

    return {"places": places or [], "events": events or []}


# ─────────────────────────────────────────────
# 2. PROMPT DESIGN — il cuore di tutto
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are the editorial composer for ESCO — a curated city companion.

VOICE: A friend who lives in the city and writes for Monocle, Cereal, Apartamento.
Italian elegance, dry wit, sensorial detail, zero touristy fluff. Specific. Observational.
Short declarative sentences mixed with one elegant longer one. Never TripAdvisor, never Lonely Planet.

═══════════════════════════════════════════════════
RULES FOR NOTES (the editorial voice in each step)
═══════════════════════════════════════════════════

Every "note" must contain at least ONE of these elements:
  • A concrete operational tip: "arrive before 11", "side entrance is empty", "skip the main hall"
  • A specific sensory detail: "the light at 5pm through the glass roof", "the smell of paper in the back room"
  • A counterintuitive instruction: "don't try to see everything", "leave after 30 min", "order a single Negroni"
  • A piece of insider knowledge: "the owner used to design for Olivetti", "the bar opens late on Tuesdays"
  • A small specific fact about THIS place (light, hour, smell, layout, ritual)

═══════════════════════════════════════════════════
BANNED PHRASES — never use these or close variants
═══════════════════════════════════════════════════

  • "Immerse yourself" / "Lose yourself"
  • "A treasure trove" / "A hidden gem"
  • "Embrace the [Italian word] ritual"
  • "Allow the light to guide you"
  • "Step back in time"
  • "Must-see" / "Must-visit"
  • "A perfect blend of"
  • "Heart of [neighborhood]" / "soul of the city"
  • "Culinary indulgence" / "culinary journey"
  • "Unwind" / "Recharge" / "Escape the everyday"
  • "Take in the views"
  • Generic adjectives that say nothing: "striking", "vibrant", "charming", "stunning", "breathtaking", "iconic"

═══════════════════════════════════════════════════
WRITING EXAMPLES — bad vs good
═══════════════════════════════════════════════════

EXAMPLE 1
Bad:  "Begin at Fondazione Prada, a striking reinvention of industrial architecture."
Good: "Skip the main exhibition — the gold-leaf tower at the back is the real reason. Go up, look down."

EXAMPLE 2
Bad:  "Immerse yourself in the Pinacoteca, a treasure trove of Italian masters."
Good: "Go straight to room XXIV for the Caravaggio. Don't try to see everything. Depth, not breadth."

EXAMPLE 3
Bad:  "Embrace the Milanese ritual of aperitivo, a perfect blend of conversation."
Good: "Pick a bar with people standing outside. The ones with no one inside know something. Spritz and three small things, that's the format."

EXAMPLE 4
Bad:  "Take in the breathtaking views from Terrazza Triennale."
Good: "The bar on the terrace is usually empty at 6:30. Order before the crowd. The glass is cool to the touch."

EXAMPLE 5
Bad:  "Wander through Navigli's vibrant nightlife."
Good: "Walk on the right side of the canal — the bars on that side keep the doors open later. Watch where locals stop."

═══════════════════════════════════════════════════
HEADLINE RULES — the most important
═══════════════════════════════════════════════════

CRITICAL: Headlines must be EACH ONE DIFFERENT. Never repeat patterns or structures across journeys.

The headline is a single evocative line, like a magazine title. Concrete imagery, not abstractions.

NEVER use these patterns:
  • "The hour [city/something] [does something]" — banned (too easy)
  • "Best of...", "Top...", "Guide to...", "Ultimate..."
  • "From X to Y" (e.g. "From art to aperitivo")
  • "A [adjective] [noun] in [city]"
  • Patterns starting with "An" or "A" followed by generic noun

DIVERSE EXAMPLES of strong headlines (vary structure, length, mood):
  • "A Sunday that doesn't need a plan"
  • "Where the light stays longest"
  • "The bar that opens when others close"
  • "Three coffees and a bookshop"
  • "Brera, but the side streets"
  • "Tuesday is the new Saturday"
  • "Slow down by the canal"
  • "What the architects do at lunch"
  • "Sunset, slowly"
  • "A walk between two churches"
  • "Drinking with a view"
  • "The afternoon that disappears"

DO NOT copy any of these literally — they show RANGE, not templates.
Each new journey must have a headline NOBODY has used before.

═══════════════════════════════════════════════════
TITLE RULES
═══════════════════════════════════════════════════

Title is the formal name (different from headline).
  • 3-6 words
  • Specific (mention neighborhood, time, mood, or activity)
  • Examples:  "Slow Sunday in Brera", "First Night Downtown", "Architecture in Half a Day"

NEVER use:
  • "Afternoon in Milan" / "An Afternoon in [generic]"
  • "Milanese Afternoon" / "Milanese Stillness"
  • Generic title with no concrete reference

═══════════════════════════════════════════════════
STRUCTURE RULES
═══════════════════════════════════════════════════

1. Use ONLY entity IDs from the provided inventory. Never invent IDs.
2. Steps must follow a logical time + geography order.
3. Transit times must be realistic for the actual city.
4. Last step has no next_transit_mode (it's the end).
5. Steps: 3 to 7.

Output ONLY valid JSON matching the required schema. No prose outside JSON."""


def build_user_prompt(request: JourneyComposeRequest, inventory: dict[str, Any]) -> str:
    """Costruisce il prompt-utente con le preferenze + inventario."""

    # Brief preferenze
    if request.surprise_me:
        brief = "User wants a surprise — pick what would delight them most right now."
    else:
        brief_parts = []
        if request.vibe:
            vibe_map = {
                "relaxing": "calm, slow-paced, contemplative",
                "lively": "energetic, social, vibrant",
                "cultural": "art, architecture, intellectual",
                "romantic": "intimate, warm, evocative",
                "discovery": "off the beaten path, hidden, local",
            }
            brief_parts.append(f"Mood: {vibe_map[request.vibe]}")
        if request.duration:
            dur_map = {
                "short": "2-3 hours, focused",
                "afternoon": "3-5 hours, the afternoon",
                "evening": "evening into night, 4-6 hours",
                "full_day": "a full day, 6-8 hours",
            }
            brief_parts.append(f"Duration: {dur_map[request.duration]}")
        if request.kind_preference == "places_only":
            brief_parts.append("Use only places (no events).")
        elif request.kind_preference == "events_only":
            brief_parts.append("Use only events.")
        brief = "\n".join(brief_parts) if brief_parts else "Free composition."

    # Inventario compatto
    places_list = "\n".join([
        f"  - id={p['id']} | {p['name']} | {p.get('description', '')[:120] if p.get('description') else ''} | addr: {p.get('address', '?')}"
        for p in inventory["places"]
    ]) or "  (no places)"

    events_list = "\n".join([
        f"  - id={e['id']} | {e['title']} | {e.get('description', '')[:120] if e.get('description') else ''} | venue: {e.get('venue_name', '?')} | when: {e.get('start_at', '?')}"
        for e in inventory["events"]
    ]) or "  (no events)"

    valid_place_ids = ", ".join([f'"{p["id"]}"' for p in inventory["places"]])
    valid_event_ids = ", ".join([f'"{e["id"]}"' for e in inventory["events"]])

    id_constraint = f"""## CRITICAL: Valid entity_id values

        The ONLY place IDs you can use:
        [{valid_place_ids}]

        The ONLY event IDs you can use:
        [{valid_event_ids}]

        If you write any other UUID, the system rejects the output. Pick from these lists ONLY.
        Copy IDs exactly — character by character. No modifications."""

    schema_example = """{
  "title": "string (4-80 chars)",
  "headline": "string (8-120 chars, evocative)",
  "subtitle": "string (6-100 chars)",
  "description": "string (80-600 chars, editorial opening paragraph)",
  "duration_min": integer (total in minutes),
  "distance_m": integer or null (total in meters),
  "vibe_tags": ["string", ...] (2-6 lowercase tags),
  "steps": [
    {
      "entity_type": "place" | "event",
      "entity_id": "uuid from inventory",
      "note": "string (20-400 chars, editorial)",
      "suggested_time": "string or null (e.g. '11:00', 'around sunset')",
      "duration_min": integer or null,
      "next_transit_mode": "walk" | "transit" | "taxi" | "bike" | "car" | null,
      "next_duration_min": integer or null,
      "next_note": "string or null (max 200 chars)"
    }
  ]
}"""

    return f"""## Brief
{brief}

## City inventory — Places
{places_list}

## City inventory — Events upcoming
{events_list}

{id_constraint}

## Required JSON output schema
{schema_example}

Now compose the journey. Output ONLY the JSON object."""


# ─────────────────────────────────────────────
# 3. ORCHESTRATION
# ─────────────────────────────────────────────

def compose_journey(request: JourneyComposeRequest) -> dict[str, Any]:
    """
    Pipeline completa:
    1. Fetcha inventario città
    2. Costruisce prompt
    3. Chiama LLM
    4. Valida output
    5. Scrive su DB (journeys + journey_steps + feed_items)
    6. Ritorna il journey appena creato
    """
    # 1. Inventario
    inventory = fetch_city_inventory(request.city_id)
    if not inventory["places"] and not inventory["events"]:
        raise ValueError("Empty city inventory — nothing to compose with.")

    # 2. Prompt
    user_prompt = build_user_prompt(request, inventory)

    # 3. LLM
    llm = get_llm()
    raw = llm.generate_json(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.85,  # un po' alta per creatività editoriale
    )

    # 4. Validation
    try:
        composition = AIJourneyComposition.model_validate(raw)
    except Exception as e:
        raise ValueError(f"AI returned invalid structure: {e}\n\nRaw: {json.dumps(raw)[:1000]}")

    # Verifica che gli ID esistano davvero nell'inventario
    valid_place_ids = {p["id"] for p in inventory["places"]}
    valid_event_ids = {e["id"] for e in inventory["events"]}
    for i, step in enumerate(composition.steps):
        if step.entity_type == "place" and step.entity_id not in valid_place_ids:
            raise ValueError(f"Step {i+1}: place id {step.entity_id} not in inventory (AI hallucinated)")
        if step.entity_type == "event" and step.entity_id not in valid_event_ids:
            raise ValueError(f"Step {i+1}: event id {step.entity_id} not in inventory (AI hallucinated)")

    # 5. Write to DB
    sb = get_supabase()
    slug = f"ai-{uuid.uuid4().hex[:8]}"

    journey_row = {
        "city_id": request.city_id,
        "title": composition.title,
        "slug": slug,
        "headline": composition.headline,
        "subtitle": composition.subtitle,
        "description": composition.description,
        "cover_image": None,  # niente cover per ora, lo riempiamo col primo step's image
        "author_kind": "ai",
        "duration_min": composition.duration_min,
        "distance_m": composition.distance_m,
        "vibe_tags": composition.vibe_tags,
        "visibility": "public",
        "created_by": request.user_id,
    }

    # Insert journey
    inserted = sb.table("journeys").insert(journey_row).execute()
    journey_id = inserted.data[0]["id"]

    # Insert steps
    step_rows = []
    for idx, step in enumerate(composition.steps, start=1):
        is_last = idx == len(composition.steps)
        step_rows.append({
            "journey_id": journey_id,
            "step_order": idx,
            "entity_type": step.entity_type,
            "entity_id": step.entity_id,
            "note": step.note,
            "suggested_time": step.suggested_time,
            "duration_min": step.duration_min,
            "next_transit_mode": None if is_last else step.next_transit_mode,
            "next_duration_min": None if is_last else step.next_duration_min,
            "next_note": None if is_last else step.next_note,
        })
    sb.table("journey_steps").insert(step_rows).execute()

    # Smart cover selection:
    # 1) place più "iconico" (popularity più alta) tra gli step
    # 2) primo step con cover_image valida
    # 3) fallback: niente cover (la home gestisce empty state)
    def find_cover_for_journey() -> str | None:
        candidates = []
        for step in composition.steps:
            if step.entity_type == "place":
                entity = next((p for p in inventory["places"] if p["id"] == step.entity_id), None)
            else:
                entity = next((e for e in inventory["events"] if e["id"] == step.entity_id), None)
            if not entity:
                continue
            cover_img = entity.get("cover_image")
            if not cover_img:
                continue
            popularity = entity.get("popularity") or 0
            candidates.append((popularity, cover_img))

        if not candidates:
            return None

        # Sort per popularity DESC, prendi la migliore
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    cover = find_cover_for_journey()
    if cover:
        sb.table("journeys").update({"cover_image": cover}).eq("id", journey_id).execute()

    # Insert in feed
    sb.table("feed_items").insert({
        "kind": "journey",
        "ref_id": journey_id,
        "city_id": request.city_id,
        "headline": composition.headline,
        "subtitle": composition.subtitle,
        "image_url": cover,
        "vibe_tags": composition.vibe_tags,
        "aspect_ratio": 1.5,
        "score": 70,  # AI journeys partono con score medio-alto
    }).execute()

    return {
        "ok": True,
        "journey_id": journey_id,
        "title": composition.title,
        "headline": composition.headline,
        "steps_count": len(composition.steps),
    }