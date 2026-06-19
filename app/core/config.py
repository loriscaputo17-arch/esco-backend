from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # LLM providers
    GEMINI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    # LLM config
    LLM_PROVIDER: str = "gemini"
    LLM_MODEL_GEMINI: str = "gemini-2.5-flash-lite"
    LLM_MODEL_CLAUDE: str = "claude-haiku-4-5"

    # Push notifications
    PUSH_WEBHOOK_SECRET: str = ""

    # Environment
    ENV: str = "development"
    PORT: int = 8000

    # Content OS — ingestion
    GOOGLE_MAPS_API_KEY: str = ""
    RAPIDAPI_KEY: str = ""
    RAPIDAPI_INSTAGRAM_HOST: str = "instagram-scraper-api3.p.rapidapi.com"
    RAPIDAPI_INSTAGRAM_PATH: str = "/wp-json/mediafy/v1/post_info"
    INGESTION_WORKER_SECRET: str = ""


settings = Settings()  # type: ignore