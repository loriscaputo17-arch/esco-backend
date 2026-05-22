from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Variabili d'ambiente del backend.
    In locale → caricate da .env
    In produzione → caricate da Railway env vars
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Supabase (service-role: chiave admin)
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # Anthropic (per Sessione 13)
    ANTHROPIC_API_KEY: str = ""

    # Environment
    ENV: str = "development"
    PORT: int = 8000


settings = Settings()  # type: ignore