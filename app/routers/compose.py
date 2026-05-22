from fastapi import APIRouter, HTTPException
from app.models.journey import JourneyComposeRequest
from app.services.composer import compose_journey

router = APIRouter(prefix="/compose", tags=["compose"])


@router.post("/journey")
def compose_journey_endpoint(request: JourneyComposeRequest):
    """
    Compone un journey AI custom basato sulle preferenze utente.
    L'output viene scritto direttamente nel DB (journeys + journey_steps + feed_items).
    """
    try:
        result = compose_journey(request)
        return result
    except ValueError as e:
        # Errori di validazione (AI ha hallucinated, inventario vuoto, ecc.)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        # Errori imprevisti
        raise HTTPException(status_code=500, detail=f"Composition failed: {e}")