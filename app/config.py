from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore"
    )
    database_url: str = "postgresql+psycopg://bini:bini_local_only@127.0.0.1:55433/bini"
    allowed_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]


settings = Settings()
