from fastapi import APIRouter
from app.core.config import settings
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health_check():
    """Liveness probe — ritorna 200 se il backend è attivo."""
    return {
        "status": "ok",
        "service": "esco-backend",
        "env": settings.ENV,
    }


@router.get("/db")
def health_db():
    """Controlla anche che il DB risponda."""
    try:
        sb = get_supabase()
        # Query semplice: conta le città (è veloce e leggera)
        result = sb.table("cities").select("id", count="exact").limit(1).execute()
        return {
            "status": "ok",
            "db": "connected",
            "cities_count": result.count,
        }
    except Exception as e:
        return {
            "status": "error",
            "db": "disconnected",
            "error": str(e),
        }