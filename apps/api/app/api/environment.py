"""Non-secret local data configuration; never opens a repository or a model."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import PROJECT_ROOT, settings

router = APIRouter()


class LocalEnvironment(BaseModel):
    profile: str
    data_scope: str
    glossary_database_path: str
    translation_memory_database_path: str
    translation_profile: str
    ocr_database_path: str
    reviewed_unit_correction: dict | None = None


@router.get("", response_model=LocalEnvironment, response_model_exclude_none=True)
def local_environment() -> LocalEnvironment:
    from app.services.reviewed_unit_correction import load_reviewed_unit_correction
    try:
        correction = load_reviewed_unit_correction()
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Pripeto dokazilo merskega popravka ni veljavno.") from exc
    glossary = settings.glossary_database_path.resolve()
    memory = settings.translation_memory_database_path.resolve()
    production = (PROJECT_ROOT / "local-data").resolve()
    production_paths = {production / "glossary.sqlite3", production / "translation-memory.sqlite3"}
    # Derive scope from actual paths, not a potentially misleading profile label.
    scope = "production" if {glossary, memory} == production_paths else (
        "mixed" if glossary in production_paths or memory in production_paths else "isolated")
    return LocalEnvironment(profile=settings.local_data_profile, data_scope=scope,
        glossary_database_path=str(glossary), translation_memory_database_path=str(memory),
        translation_profile=settings.local_translation_profile, ocr_database_path=str(settings.resolved_ocr_database_path.resolve()),
        reviewed_unit_correction=correction)
