"""
Pydantic models per la composizione AI di un journey.
Output rigorosamente validato — se l'AI sgarra, fallisce subito.
"""
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# INPUT — quello che l'utente chiede
# ─────────────────────────────────────────────

class JourneyComposeRequest(BaseModel):
    """Richiesta dall'app: utente vuole un journey custom."""
    user_id: str = Field(..., description="UUID utente che richiede")
    city_id: str = Field(..., description="UUID della città")

    vibe: Optional[Literal["relaxing", "lively", "cultural", "romantic", "discovery"]] = None
    duration: Optional[Literal["short", "afternoon", "evening", "full_day"]] = None
    kind_preference: Optional[Literal["places_only", "events_only", "mix"]] = "mix"

    surprise_me: bool = False  # se True ignora vibe/duration e l'AI sceglie


# ─────────────────────────────────────────────
# OUTPUT — quello che l'AI deve produrre
# ─────────────────────────────────────────────

class AIJourneyStep(BaseModel):
    """Singolo step composto dall'AI."""
    entity_type: Literal["place", "event"]
    entity_id: str = Field(..., description="UUID del place/event scelto")
    note: str = Field(..., min_length=20, max_length=400,
                      description="Nota editoriale del curatore AI")
    suggested_time: Optional[str] = Field(None, description="Es. '11:00', 'sunset', 'after dinner'")
    duration_min: Optional[int] = Field(None, ge=10, le=480)
    next_transit_mode: Optional[Literal["walk", "transit", "taxi", "bike", "car"]] = None
    next_duration_min: Optional[int] = Field(None, ge=1, le=120)
    next_note: Optional[str] = Field(None, max_length=200)


class AIJourneyComposition(BaseModel):
    """Journey completo composto dall'AI."""
    title: str = Field(..., min_length=4, max_length=80)
    headline: str = Field(..., min_length=8, max_length=120,
                          description="Titolo editoriale forte (italian/english, breve)")
    subtitle: str = Field(..., min_length=6, max_length=100,
                          description="Sottotitolo descrittivo, una riga")
    description: str = Field(..., min_length=80, max_length=600,
                             description="Paragrafo editoriale di apertura")
    duration_min: int = Field(..., ge=30, le=720)
    distance_m: Optional[int] = Field(None, ge=0, le=30000)
    vibe_tags: list[str] = Field(..., min_length=2, max_length=6)
    steps: list[AIJourneyStep] = Field(..., min_length=3, max_length=7)

    @field_validator("steps")
    @classmethod
    def _validate_step_order(cls, steps: list[AIJourneyStep]) -> list[AIJourneyStep]:
        # L'ultimo step non deve avere next_transit_mode (è il finale)
        if steps:
            last = steps[-1]
            if last.next_transit_mode is not None:
                last.next_transit_mode = None
                last.next_duration_min = None
                last.next_note = None
        return steps