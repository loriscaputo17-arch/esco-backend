from supabase import create_client, Client
from app.core.config import settings


def get_supabase() -> Client:
    """
    Client Supabase con service-role key.
    Bypassa RLS — usalo solo nel backend, mai esporre lato app.
    """
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY,
    )