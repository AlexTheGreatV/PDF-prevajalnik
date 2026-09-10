from pathlib import Path
import tempfile
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


class Settings(BaseSettings):
    app_env: str = "local"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    storage_dir: Path = Path("apps/api/storage")
    processing_tmp_dir: Path = Path(tempfile.gettempdir()) / "jub-koop-pdf-pipeline"
    max_upload_bytes: int = 50 * 1024 * 1024
    poppler_pdftoppm: str = "pdftoppm"
    local_translation_profile: Literal["hplt-en-sl-v2", "opus-en-sla"] = "hplt-en-sl-v2"
    local_translation_model_path: Path = Path("local-models/hplt-en-sl-v2-ct2-int8")
    local_translation_device: str = "cpu"
    local_translation_compute_type: str = "int8"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
