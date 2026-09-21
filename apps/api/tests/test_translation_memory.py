from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.api.translation_memory import get_translation_memory_repository
from app.core.config import PROJECT_ROOT, Settings
from app.main import app
from app.services.translation_memory_repository import (
    SqliteTranslationMemoryRepository,
    TranslationMemoryRevisionConflictError,
    normalize_source_text,
    source_sha256,
)
from app.translation_memory_models import TranslationMemoryEntryCreate, TranslationMemoryEntryUpdate


def payload(**changes: object) -> TranslationMemoryEntryCreate:
    values: dict[str, object] = {
        "source_text": "Apply the product to a clean surface.",
        "target_text": "Izdelek nanesite na čisto površino.",
        "source_language": "en",
        "target_language": "sl",
        "domain": "construction",
        "status": "proposed",
        "origin": "manual_review",
        "source_kind": "native",
    }
    values.update(changes)
    return TranslationMemoryEntryCreate.model_validate(values)


@pytest.fixture
def repository(tmp_path: Path) -> SqliteTranslationMemoryRepository:
    return SqliteTranslationMemoryRepository(tmp_path / "translation-memory.sqlite3")


@pytest.fixture
def client(repository: SqliteTranslationMemoryRepository):
    app.dependency_overrides[get_translation_memory_repository] = lambda: repository
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_migration_and_database_pragmas(repository: SqliteTranslationMemoryRepository) -> None:
    repository.initialize()
    repository.initialize()
    assert repository.schema_version() == 1
    with repository._connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


def test_conservative_normalization() -> None:
    assert normalize_source_text("  Café\r\n65 MPa.  ") == "Café\n65 MPa."
    assert source_sha256("Cafe\u0301") == source_sha256("Café")
    assert source_sha256("Text") != source_sha256("text")
    assert source_sha256("Text.") != source_sha256("Text")
    assert source_sha256("65 MPa") != source_sha256("66 MPa")


def test_lifecycle_history_revision_and_restart(repository: SqliteTranslationMemoryRepository) -> None:
    created = repository.create_entry(payload())
    approved = repository.transition(created.id, 1, "approved", "approve")
    updated = repository.update_entry(approved.id, TranslationMemoryEntryUpdate(
        expected_revision=approved.revision, target_text="Nanesite izdelek na čisto površino.",
    ))
    assert updated.status == "proposed"
    updated = repository.transition(updated.id, updated.revision, "approved", "approve")
    archived = repository.transition(updated.id, updated.revision, "archived", "archive")
    restored = repository.transition(archived.id, archived.revision, "proposed", "restore")
    assert restored.status == "proposed"
    assert restored.approved_at is None
    assert [row.action for row in repository.history(created.id)] == ["create", "approve", "update", "approve", "archive", "restore"]
    with pytest.raises(TranslationMemoryRevisionConflictError):
        repository.update_entry(created.id, TranslationMemoryEntryUpdate(expected_revision=1, note="stale"))
    reopened = SqliteTranslationMemoryRepository(repository.database_path)
    assert reopened.get_entry(created.id).revision == 6


def test_only_approved_exact_entries_are_used(repository: SqliteTranslationMemoryRepository) -> None:
    entry = repository.create_entry(payload())
    assert repository.exact_lookup(entry.source_text, "en", "sl", "construction").status == "none"
    approved = repository.transition(entry.id, 1, "approved", "approve")
    assert repository.exact_lookup(entry.source_text, "en", "sl", "construction").match.id == approved.id
    repository.transition(entry.id, 2, "rejected", "reject")
    assert repository.exact_lookup(entry.source_text, "en", "sl", "construction").status == "none"
    archived = repository.create_entry(payload(source_text="Archive me."))
    archived = repository.transition(archived.id, archived.revision, "approved", "approve")
    repository.transition(archived.id, archived.revision, "archived", "archive")
    assert repository.exact_lookup("Archive me.", "en", "sl", "construction").status == "none"


def test_exact_lookup_keeps_case_punctuation_and_numbers_distinct(repository: SqliteTranslationMemoryRepository) -> None:
    entry = repository.create_entry(payload(source_text="Strength is 2.5 MPa."))
    repository.transition(entry.id, 1, "approved", "approve")
    for different in ("strength is 2.5 MPa.", "Strength is 2.5 MPa", "Strength is 3.5 MPa."):
        assert repository.exact_lookup(different, "en", "sl", "construction").status == "none"


def test_domain_specific_match_wins_and_equal_conflict_is_explicit(repository: SqliteTranslationMemoryRepository) -> None:
    generic = repository.create_entry(payload(domain="", target_text="Generični prevod."))
    repository.transition(generic.id, 1, "approved", "approve")
    specific = repository.create_entry(payload(domain="construction", target_text="Gradbeni prevod."))
    repository.transition(specific.id, 1, "approved", "approve")
    result = repository.exact_lookup(specific.source_text, "en", "sl", "construction")
    assert result.status == "match" and result.match.id == specific.id
    conflict = repository.create_entry(payload(domain="construction", target_text="Drugi gradbeni prevod."))
    repository.transition(conflict.id, 1, "approved", "approve")
    result = repository.exact_lookup(specific.source_text, "en", "sl", "construction")
    assert result.status == "conflict" and result.match is None and len(result.candidates) == 2


def test_api_crud_lookup_usage_and_export(client: TestClient) -> None:
    created = client.post("/api/translation-memory/entries", json=payload().model_dump(mode="json"))
    assert created.status_code == 201
    entry = created.json()
    assert client.post(f"/api/translation-memory/entries/{entry['id']}/approve", json={"expected_revision": 1}).status_code == 200
    lookup = client.post("/api/translation-memory/lookup/exact", json={
        "source_text": entry["source_text"], "source_language": "en", "target_language": "sl", "domain": "construction",
    })
    assert lookup.json()["status"] == "match"
    assert client.post("/api/translation-memory/usage", json={"entry_ids": [entry["id"]]}).status_code == 204
    assert client.get("/api/translation-memory/stats").json()["total"] == 1
    exported = client.get("/api/translation-memory/export.json")
    assert exported.status_code == 200
    assert exported.json()["schema_version"] == 1
    assert exported.json()["active_set_hash"] == client.get("/api/translation-memory/stats").json()["active_set_hash"]


def test_concurrent_writers_are_serialized(repository: SqliteTranslationMemoryRepository) -> None:
    repository.initialize()

    def create(index: int) -> str:
        return repository.create_entry(payload(source_text=f"Concurrent sentence {index}.")).id

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(create, range(8)))
    assert len(set(ids)) == 8
    assert repository.stats().total == 8


def test_locked_database_returns_a_safe_api_error(repository: SqliteTranslationMemoryRepository) -> None:
    repository.initialize()
    lock = sqlite3.connect(repository.database_path, isolation_level=None)
    lock.execute("PRAGMA journal_mode = WAL")
    lock.execute("BEGIN IMMEDIATE")
    app.dependency_overrides[get_translation_memory_repository] = lambda: repository
    try:
        with TestClient(app) as test_client:
            response = test_client.post("/api/translation-memory/entries", json=payload().model_dump(mode="json"))
        assert response.status_code == 503
        assert response.json()["detail"] == "Lokalni prevajalski spomin ni na voljo."
    finally:
        lock.rollback()
        lock.close()
        app.dependency_overrides.clear()


def test_default_database_path_does_not_depend_on_working_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = (PROJECT_ROOT / "local-data" / "translation-memory.sqlite3").resolve()
    paths = []
    for working_directory in (PROJECT_ROOT, PROJECT_ROOT / "apps" / "api"):
        monkeypatch.chdir(working_directory)
        paths.append(Settings(_env_file=None).translation_memory_database_path)
    assert paths == [expected, expected]


def test_unavailable_memory_does_not_break_health(tmp_path: Path) -> None:
    bad = SqliteTranslationMemoryRepository(tmp_path / "directory-as-db")
    bad.database_path.mkdir()
    app.dependency_overrides[get_translation_memory_repository] = lambda: bad
    try:
        with TestClient(app) as test_client:
            assert test_client.get("/api/translation-memory/status").json()["available"] is False
            assert test_client.get("/health").json() == {"status": "ok"}
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Independent PDF analysis")
            pdf_bytes = document.tobytes()
            document.close()
            analyzed = test_client.post(
                "/api/pdf/analyze",
                files={"file": ("independent.pdf", pdf_bytes, "application/pdf")},
            )
            assert analyzed.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_corrupt_database_is_reported_without_breaking_health(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a sqlite database")
    corrupt = SqliteTranslationMemoryRepository(path)
    app.dependency_overrides[get_translation_memory_repository] = lambda: corrupt
    try:
        with TestClient(app) as test_client:
            assert test_client.get("/api/translation-memory/status").json()["available"] is False
            assert test_client.get("/health").status_code == 200
    finally:
        app.dependency_overrides.clear()
