from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    APP_NAME: str = "IoT Cadence Backend"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_JWT_SECRET: str = ""

    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    YOLO_DEFAULT_MODEL: str = "yolov8n"
    YOLO_DEFAULT_CONFIDENCE: float = 0.5

    ANOMALY_THRESHOLD_PCT: float = 15.0
    CAMERA_PING_TIMEOUT_SECONDS: float = 3.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
