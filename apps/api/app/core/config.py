from pathlib import Path
import tempfile
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


API_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = API_ROOT.parents[1] if API_ROOT.name == "api" and API_ROOT.parent.name == "apps" else API_ROOT


class Settings(BaseSettings):
    app_env: str = "local"
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]
    storage_dir: Path = Path("apps/api/storage")
    processing_tmp_dir: Path = Path(tempfile.gettempdir()) / "jub-koop-pdf-pipeline"
    max_upload_bytes: int = 50 * 1024 * 1024
    poppler_pdftoppm: str = "pdftoppm"
    local_translation_profile: Literal["hplt-en-sl-v2", "opus-en-sla"] = "hplt-en-sl-v2"
    local_translation_model_path: Path = Path("local-models/hplt-en-sl-v2-ct2-int8")
    local_translation_device: str = "cpu"
    local_translation_compute_type: str = "int8"
    glossary_database_path: Path = Path("local-data/glossary.sqlite3")
    translation_memory_database_path: Path = Path("local-data/translation-memory.sqlite3")

    @field_validator("glossary_database_path", "translation_memory_database_path", mode="after")
    @classmethod
    def resolve_local_database_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
