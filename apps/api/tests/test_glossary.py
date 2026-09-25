from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.services.glossary_repository as glossary_repository_module
from app.api.glossary import _csv_escaped_fields, _csv_restore, _csv_safe, get_glossary_repository
from app.core.config import PROJECT_ROOT, Settings
from app.glossary_models import GlossaryEntryCreate, GlossaryEntryUpdate
from app.main import app
from app.services.glossary_repository import (
    MIGRATIONS,
    GlossaryRepositoryError,
    GlossaryRevisionConflictError,
    SqliteGlossaryRepository,
)


def payload(**changes: object) -> GlossaryEntryCreate:
    values: dict[str, object] = {
        "source_term": "curing",
        "target_term": "strjevanje",
        "source_language": "en",
        "target_language": "sl",
        "domain": "gradbeništvo",
        "match_mode": "preferred_term",
        "case_sensitive": False,
        "status": "proposed",
        "origin": "manual",
        "note": "",
    }
    values.update(changes)
    return GlossaryEntryCreate.model_validate(values)


@pytest.fixture
def repository(tmp_path: Path) -> SqliteGlossaryRepository:
    return SqliteGlossaryRepository(tmp_path / "glossary.sqlite3")


@pytest.fixture
def client(repository: SqliteGlossaryRepository):
    app.dependency_overrides[get_glossary_repository] = lambda: repository
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_migration_is_idempotent_and_enables_safety_pragmas(repository: SqliteGlossaryRepository) -> None:
    repository.initialize()
    repository.initialize()
    assert repository.schema_version() == 3
    with repository._connection() as configured_connection:
        assert configured_connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert configured_connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert configured_connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert configured_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 1").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_schema_one_database_migrates_without_changing_existing_data(tmp_path: Path) -> None:
    database = tmp_path / "schema-one.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(MIGRATIONS[0][1])
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')")
        connection.execute(
            """INSERT INTO glossary_entries(
                id, source_term, target_term, source_language, target_language, domain, match_mode,
                case_sensitive, status, origin, note, source_label, document_hash, revision,
                created_at, updated_at, usage_count, last_used_at
            ) VALUES ('old', 'curing', 'strjevanje', 'en', 'sl', '', 'preferred_term', 0,
                      'archived', 'manual', 'legacy note', 'legacy source', NULL, 3, '2026-01-01T00:00:00Z',
                      '2026-02-01T00:00:00Z', 7, '2026-02-02T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO glossary_history(history_id, entry_id, revision, action, snapshot_json, created_at)
               VALUES ('history-old', 'old', 3, 'archive', '{"id":"old"}', '2026-02-01T00:00:00Z')"""
        )
        before_entry = connection.execute(
            """SELECT id, source_term, target_term, status, revision, usage_count, note, source_label,
                      created_at, updated_at, last_used_at FROM glossary_entries WHERE id = 'old'"""
        ).fetchone()
        before_history = connection.execute(
            "SELECT history_id, entry_id, revision, action, snapshot_json, created_at FROM glossary_history"
        ).fetchall()
    migrated = SqliteGlossaryRepository(database)
    assert migrated.schema_version() == 3
    assert SqliteGlossaryRepository(database).schema_version() == 3
    entry = migrated.get_entry("old")
    assert entry.target_term == "strjevanje"
    assert entry.status == "archived"
    assert entry.revision == 3
    assert entry.usage_count == 7
    assert entry.external_source is None
    with sqlite3.connect(database) as connection:
        after_entry = connection.execute(
            """SELECT id, source_term, target_term, status, revision, usage_count, note, source_label,
                      created_at, updated_at, last_used_at FROM glossary_entries WHERE id = 'old'"""
        ).fetchone()
        after_history = connection.execute(
            "SELECT history_id, entry_id, revision, action, snapshot_json, created_at FROM glossary_history"
        ).fetchall()
        assert before_entry == after_entry
        assert before_history == after_history
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 2").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 3").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_schema_two_database_migrates_to_three_without_changing_entries_or_history(tmp_path: Path) -> None:
    database = tmp_path / "schema-two.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(MIGRATIONS[0][1])
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')")
        connection.executescript(MIGRATIONS[1][1])
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (2, '2026-02-01T00:00:00Z')")
        connection.execute(
            """INSERT INTO glossary_entries(
                id, source_term, target_term, source_language, target_language, domain, match_mode,
                case_sensitive, status, origin, note, source_label, document_hash, revision,
                created_at, updated_at, usage_count, last_used_at, external_source, external_record_id
            ) VALUES ('schema-two', 'open time', 'odprti čas', 'en', 'sl', 'building', 'preferred_term',
                      0, 'approved', 'import', 'legacy', 'source', NULL, 4,
                      '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', 9, NULL, 'legacy import', '42')"""
        )
        connection.execute(
            """INSERT INTO glossary_history(history_id, entry_id, revision, action, snapshot_json, created_at)
               VALUES ('schema-two-history', 'schema-two', 4, 'approve', '{}', '2026-02-01T00:00:00Z')"""
        )
        before_entry = connection.execute("SELECT * FROM glossary_entries WHERE id = 'schema-two'").fetchone()
        before_history = connection.execute("SELECT * FROM glossary_history").fetchall()

    repository = SqliteGlossaryRepository(database)
    assert repository.schema_version() == 3
    assert SqliteGlossaryRepository(database).schema_version() == 3
    entry = repository.get_entry("schema-two")
    assert (entry.id, entry.status, entry.revision, entry.usage_count) == ("schema-two", "approved", 4, 9)
    assert all(value is None for value in (
        entry.external_evaluation, entry.external_primarity, entry.external_lifecycle,
        entry.external_institution, entry.external_origins, entry.external_modified_at,
    ))
    with sqlite3.connect(database) as connection:
        after_entry = connection.execute(
            "SELECT " + ", ".join(item[1] for item in connection.execute("PRAGMA table_info(glossary_entries)") if item[1] not in {
                "external_evaluation", "external_primarity", "external_lifecycle",
                "external_institution", "external_origins", "external_modified_at",
            }) + " FROM glossary_entries WHERE id = 'schema-two'"
        ).fetchone()
        assert tuple(before_entry) == tuple(after_entry)
        assert before_history == connection.execute("SELECT * FROM glossary_history").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 3").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_schema_migration_rolls_back_entire_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "failed-migration.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(MIGRATIONS[0][1])
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')")
    failing = MIGRATIONS + ((4, "ALTER TABLE glossary_entries ADD COLUMN migration_probe TEXT; INVALID SQL;"),)
    monkeypatch.setattr(glossary_repository_module, "MIGRATIONS", failing)

    with pytest.raises(GlossaryRepositoryError):
        SqliteGlossaryRepository(database).initialize()

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(glossary_entries)")}
        assert "migration_probe" not in columns
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 4").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_create_update_history_and_revision_conflict(repository: SqliteGlossaryRepository) -> None:
    created = repository.create_entry(payload())
    updated = repository.update_entry(created.id, GlossaryEntryUpdate(expected_revision=1, target_term="utrjevanje"))
    approved = repository.transition(created.id, updated.revision, "approved", "approve")
    assert approved.revision == 3
    assert [item.action for item in repository.history(created.id)] == ["create", "update", "approve"]
    with pytest.raises(GlossaryRevisionConflictError):
        repository.update_entry(created.id, GlossaryEntryUpdate(expected_revision=1, target_term="staro"))
    assert repository.get_entry(created.id).target_term == "utrjevanje"


def test_update_normalizes_terms_and_can_clear_optional_source_metadata(repository: SqliteGlossaryRepository) -> None:
    created = repository.create_entry(payload(source_label="neutral", document_hash="a" * 64))
    updated = repository.update_entry(created.id, GlossaryEntryUpdate(
        expected_revision=created.revision,
        source_term="  curing time  ",
        source_label=None,
        document_hash=None,
    ))
    assert updated.source_term == "curing time"
    assert updated.source_label is None
    assert updated.document_hash is None


def test_archive_and_restore_never_physically_delete(repository: SqliteGlossaryRepository) -> None:
    created = repository.create_entry(payload(status="approved"))
    archived = repository.transition(created.id, 1, "archived", "archive")
    restored = repository.transition(created.id, archived.revision, "proposed", "restore")
    assert restored.status == "proposed"
    assert [item.action for item in repository.history(created.id)] == ["create", "archive", "restore"]


def test_filters_stats_and_active_hash_only_follow_approved_entries(repository: SqliteGlossaryRepository) -> None:
    approved = repository.create_entry(payload(status="approved"))
    repository.create_entry(payload(source_term="sealant", target_term="tesnilna masa", status="rejected"))
    before = repository.active_set_hash()
    repository.update_entry(approved.id, GlossaryEntryUpdate(expected_revision=1, note="non-semantic note"))
    assert repository.active_set_hash() != before
    entries, total = repository.list_entries(query="seal", status="rejected")
    assert total == 1 and entries[0].source_term == "sealant"
    stats = repository.stats()
    assert stats.total == 2
    assert stats.by_status == {"approved": 1, "rejected": 1}


def test_api_crud_transitions_history_and_conflict(client: TestClient) -> None:
    created = client.post("/api/glossary/entries", json=payload().model_dump()).json()
    assert created["revision"] == 1
    approved = client.post(f"/api/glossary/entries/{created['id']}/approve", json={"expected_revision": 1})
    assert approved.status_code == 200
    conflict = client.put(
        f"/api/glossary/entries/{created['id']}",
        json={"expected_revision": 1, "target_term": "zastarel popravek"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current"]["revision"] == 2
    history = client.get(f"/api/glossary/entries/{created['id']}/history").json()
    assert [record["action"] for record in history] == ["create", "approve"]


def test_api_list_filters_search_and_stats(client: TestClient) -> None:
    client.post("/api/glossary/entries", json=payload(status="approved").model_dump())
    client.post("/api/glossary/entries", json=payload(source_term="sealant", target_term="tesnilna masa", domain="kemija").model_dump())
    response = client.get("/api/glossary/entries", params={"q": "seal", "domain": "kemija"})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert client.get("/api/glossary/stats").json()["total"] == 2


def test_usage_counts_only_approved_entries_once_per_request(client: TestClient) -> None:
    approved = client.post("/api/glossary/entries", json=payload(status="approved").model_dump()).json()
    proposed = client.post(
        "/api/glossary/entries",
        json=payload(source_term="sealant", target_term="tesnilna masa").model_dump(),
    ).json()
    response = client.post(
        "/api/glossary/usage",
        json={"entry_ids": [approved["id"], approved["id"], proposed["id"], "unknown-entry"]},
    )
    assert response.status_code == 204
    entries = {entry["id"]: entry for entry in client.get("/api/glossary/entries").json()["entries"]}
    assert entries[approved["id"]]["usage_count"] == 1
    assert entries[approved["id"]]["last_used_at"] is not None
    assert entries[proposed["id"]]["usage_count"] == 0


def test_csv_preview_reports_new_duplicate_conflict_invalid_and_override(client: TestClient) -> None:
    client.post("/api/glossary/entries", json=payload(status="approved").model_dump())
    content = "\n".join([
        "source_term,target_term,source_language,target_language,domain,match_mode,case_sensitive,status,origin,note,source_label,document_hash",
        "curing,strjevanje,en,sl,gradbeništvo,preferred_term,false,approved,import,,,",
        "curing,utrjevanje,en,sl,gradbeništvo,preferred_term,false,approved,import,,,",
        "sealant,tesnilna masa,en,sl,gradbeništvo,preferred_term,false,proposed,import,,,",
        ",bad,en,sl,,preferred_term,false,proposed,import,,,",
    ])
    response = client.post("/api/glossary/imports/csv/preview", files={"file": ("glossary.csv", content.encode(), "text/csv")})
    assert response.status_code == 200
    data = response.json()
    assert (data["duplicate_count"], data["override_count"], data["new_count"], data["invalid_count"]) == (1, 1, 1, 1)


def test_csv_preview_detects_duplicates_and_conflicts_inside_the_upload(client: TestClient) -> None:
    content = "\n".join([
        "source_term,target_term,source_language,target_language,domain,match_mode,case_sensitive,status,origin,note,source_label,document_hash",
        "curing,strjevanje,en,sl,gradbeništvo,preferred_term,false,proposed,import" + ",,,",
        "curing,strjevanje,en,sl,gradbeništvo,preferred_term,false,proposed,import" + ",,,",
        "curing,utrjevanje,en,sl,gradbeništvo,preferred_term,false,proposed,import" + ",,,",
    ])
    response = client.post("/api/glossary/imports/csv/preview", files={"file": ("glossary.csv", content.encode(), "text/csv")})
    assert response.status_code == 200
    data = response.json()
    assert data["duplicate_count"] == 1
    assert data["conflict_count"] == 1
    assert len(data["valid_rows"]) == 2


def test_legacy_csv_confirm_cannot_overwrite_existing_entries(client: TestClient) -> None:
    existing = client.post("/api/glossary/entries", json=payload(status="approved").model_dump()).json()
    row = payload(target_term="utrjevanje", status="approved", origin="import").model_dump() | {"row_number": 2}
    response = client.post("/api/glossary/imports/csv/confirm", json={"rows": [row], "overwrite_conflicts": True})
    assert response.status_code == 409
    history = client.get(f"/api/glossary/entries/{existing['id']}/history").json()
    assert [item["action"] for item in history] == ["create"]


def test_csv_export_escapes_spreadsheet_formulas_and_json_is_local(client: TestClient) -> None:
    created = client.post("/api/glossary/entries", json=payload(source_term="=1+1", target_term="@formula").model_dump()).json()
    csv_response = client.get("/api/glossary/export.csv")
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert rows[0]["source_term"] == "'=1+1"
    assert rows[0]["target_term"] == "'@formula"
    assert rows[0]["formula_escape_version"] == "1"
    assert set(rows[0]["formula_escaped_fields"].split("|")) == {"source_term", "target_term"}
    preview = client.post(
        "/api/glossary/imports/csv/preview",
        files={"file": ("glossary.csv", csv_response.content, "text/csv")},
    ).json()
    issue = next(item for item in preview["issues"] if item["existing_entry_id"] == created["id"])
    assert issue["kind"] == "duplicate"
    json_response = client.get("/api/glossary/export.json")
    assert json_response.json()["schema_version"] == 3


def test_csv_negative_technical_term_round_trips_without_formula_prefix(client: TestClient) -> None:
    original = client.post(
        "/api/glossary/entries",
        json=payload(
            source_term="-40 °C",
            target_term="−40 °C",
            note='Slovenski znaki, "narekovaji"\nin nova vrstica.',
        ).model_dump(),
    ).json()

    exported = client.get("/api/glossary/export.csv")
    assert exported.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    row = next(item for item in rows if item["source_term"] == "-40 °C")
    assert row["target_term"] == "−40 °C"
    assert row["note"] == 'Slovenski znaki, "narekovaji"\nin nova vrstica.'

    preview = client.post(
        "/api/glossary/imports/csv/preview",
        files={"file": ("glossary.csv", exported.content, "text/csv")},
    )
    assert preview.status_code == 200
    issue = next(item for item in preview.json()["issues"] if item["existing_entry_id"] == original["id"])
    assert issue["kind"] == "duplicate"


def test_csv_export_keeps_unrecognized_minus_prefix_formula_safe(client: TestClient) -> None:
    client.post("/api/glossary/entries", json=payload(source_term="-cmd|' /C calc'!A0").model_dump())
    exported = client.get("/api/glossary/export.csv")
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert rows[0]["source_term"].startswith("'-")


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("=SUM(1,2)", True),
        ("+CMD", True),
        ("-CMD", True),
        ("@SUM", True),
        (" =SUM(1,2)", True),
        ("\t+CMD", True),
        ("\r-CMD", True),
        ("\n@SUM", True),
        ("-40 °C", False),
        ("-1,5", False),
        ("-0.25", False),
        ("'=SUM(1,2)", False),
    ],
)
def test_csv_formula_codec_is_safe_reversible_and_preserves_user_markers(value: str, escaped: bool) -> None:
    encoded, was_escaped = _csv_safe(value)
    assert was_escaped is escaped
    fields = _csv_escaped_fields(
        {"formula_escaped_fields": "source_term" if was_escaped else ""},
        "1",
    )
    assert _csv_restore(str(encoded), "source_term", fields) == value


def test_csv_user_apostrophe_is_not_decoded_without_an_explicit_field_marker() -> None:
    value = "'=SUM(1,2)"
    assert _csv_restore(value, "source_term", set()) == value
    with pytest.raises(ValueError, match="poškodovana"):
        _csv_restore("'literal", "target_term", {"target_term"})


def test_import_rejects_unexpected_fields_and_limits_rows(client: TestClient) -> None:
    unexpected = b"source_term,target_term,secret\na,b,c\n"
    assert client.post("/api/glossary/imports/csv/preview", files={"file": ("x.csv", unexpected, "text/csv")}).status_code == 422
    rows = "source_term,target_term\n" + "\n".join(f"s{i},t{i}" for i in range(1001))
    assert client.post("/api/glossary/imports/csv/preview", files={"file": ("x.csv", rows.encode(), "text/csv")}).status_code == 413


def test_unavailable_database_does_not_break_health_or_pdf_api(tmp_path: Path) -> None:
    invalid = tmp_path / "database-directory"
    invalid.mkdir()
    broken = SqliteGlossaryRepository(invalid)
    app.dependency_overrides[get_glossary_repository] = lambda: broken
    try:
        with TestClient(app) as test_client:
            assert test_client.get("/health").status_code == 200
            status = test_client.get("/api/glossary/status")
            entries = test_client.get("/api/glossary/entries")
        assert status.json()["available"] is False
        assert entries.status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_repository_never_stores_document_paragraphs(repository: SqliteGlossaryRepository) -> None:
    entry = repository.create_entry(payload(source_label="interni vir", document_hash="a" * 64))
    assert entry.source_label == "interni vir"
    with sqlite3.connect(repository.database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(glossary_entries)")}
    assert "document_name" not in columns
    assert "document_text" not in columns


def test_entry_survives_repository_restart(tmp_path: Path) -> None:
    path = tmp_path / "persistent" / "glossary.sqlite3"
    first = SqliteGlossaryRepository(path)
    created = first.create_entry(payload(status="approved"))
    second = SqliteGlossaryRepository(path)
    restored = second.get_entry(created.id)
    assert restored.source_term == "curing"
    assert restored.status == "approved"


def test_default_database_path_is_independent_of_working_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = PROJECT_ROOT / "local-data" / "glossary.sqlite3"
    paths = []
    for working_directory in (PROJECT_ROOT, PROJECT_ROOT / "apps" / "api"):
        monkeypatch.chdir(working_directory)
        paths.append(Settings(_env_file=None).glossary_database_path)
    assert paths == [expected, expected]


def test_default_database_path_does_not_create_database_under_arbitrary_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert settings.glossary_database_path == PROJECT_ROOT / "local-data" / "glossary.sqlite3"
    assert not (tmp_path / "local-data" / "glossary.sqlite3").exists()
