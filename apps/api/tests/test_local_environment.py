from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.core.config import PROJECT_ROOT, settings
from app.main import app


@pytest.mark.parametrize("scope", ["production", "isolated", "mixed"])
def test_environment_reports_actual_paths_without_opening_databases(tmp_path: Path, monkeypatch, scope: str):
    production = PROJECT_ROOT / "local-data"
    glossary = (production if scope != "isolated" else tmp_path) / "glossary.sqlite3"
    memory = (production if scope == "production" else tmp_path) / "translation-memory.sqlite3"
    monkeypatch.setattr(settings, "glossary_database_path", glossary)
    monkeypatch.setattr(settings, "translation_memory_database_path", memory)
    monkeypatch.setattr(settings, "local_data_profile", "named-test")
    before = list(tmp_path.iterdir())
    with TestClient(app) as client:
        response = client.get("/api/environment")
    assert response.status_code == 200
    assert response.json() == {
        "profile": "named-test", "data_scope": scope,
        "glossary_database_path": str(glossary.resolve()),
        "translation_memory_database_path": str(memory.resolve()),
        "translation_profile": settings.local_translation_profile,
        "ocr_database_path": str(settings.resolved_ocr_database_path.resolve()),
    }
    assert list(tmp_path.iterdir()) == before
