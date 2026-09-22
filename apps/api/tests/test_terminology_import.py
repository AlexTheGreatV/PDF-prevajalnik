from __future__ import annotations

import asyncio
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

import app.api.terminology_import as terminology_import_api
import app.services.terminology_import as terminology_import_service
from app.api.terminology_import import preview_terminology_import
from app.api.glossary import get_glossary_repository
from app.core.config import settings
from app.glossary_models import GlossaryEntryCreate, TerminologyImportCandidate
from app.main import app
from app.services.glossary_repository import SqliteGlossaryRepository
from app.services.terminology_import import preview_registry

FIXTURES = Path(__file__).parent / "fixtures" / "terminology"


@pytest.fixture
def repository(tmp_path: Path) -> SqliteGlossaryRepository:
    return SqliteGlossaryRepository(tmp_path / "glossary.sqlite3")


@pytest.fixture
def client(repository: SqliteGlossaryRepository):
    preview_registry.clear()
    app.dependency_overrides[get_glossary_repository] = lambda: repository
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        preview_registry.clear()


def options(*, csv: bool = False, domain: str = "lepila") -> str:
    value: dict[str, object] = {
        "source_language": "en",
        "target_language": "sl",
        "default_domain": domain,
        "default_match_mode": "preferred_term",
        "external_source": "Sintetični lokalni terminološki vir",
        "source_version": "2026-09-test",
        "license_info": "Testni podatki, brez zunanjega nabora",
    }
    if csv:
        value["csv_mapping"] = {
            "source_term": "source",
            "target_term": "target",
            "domain": "subject",
            "external_record_id": "record_id",
            "definition": "definition",
        }
    return json.dumps(value, ensure_ascii=False)


def preview_csv(client: TestClient, content: str):
    return client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "csv", "options_json": options(csv=True)},
        files={"file": ("terms.csv", content.encode("utf-8"), "text/csv")},
    )


def preview_tbx(client: TestClient, content: str):
    return client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "tbx", "options_json": options()},
        files={"file": ("terms.tbx", content.encode("utf-8"), "application/xml")},
    )


def simple_tbx(*, extra: str = "", target: str = "strjevanje", entry_id: str = "term-1") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<tbx xmlns="urn:iso:std:iso:30042:ed-2"><text><body><termEntry id="{entry_id}">
  <descrip type="subjectField">lepila</descrip>
  <descrip type="definition">Proces prehoda materiala v trdno stanje.</descrip>
  {extra}
  <langSet xml:lang="en"><tig><term>curing</term></tig></langSet>
  <langSet xml:lang="sl"><tig><term>{target}</term></tig></langSet>
</termEntry></body></text></tbx>"""


def test_tbx_preview_is_dry_run_and_commit_creates_proposed_traceable_candidate(
    client: TestClient,
    repository: SqliteGlossaryRepository,
) -> None:
    response = preview_tbx(client, simple_tbx(extra='<descrip type="reliabilityCode">8</descrip>'))
    assert response.status_code == 200
    preview = response.json()
    assert preview["valid_count"] == 1
    assert preview["new_count"] == 1
    assert len(preview["file_sha256"]) == 64
    assert repository.list_entries()[1] == 0

    candidate = preview["candidates"][0]
    confirmed = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={
            "preview_id": preview["preview_id"],
            "file_sha256": preview["file_sha256"],
            "candidate_ids": [candidate["candidate_id"]],
        },
    )
    assert confirmed.status_code == 200
    entry = repository.list_entries()[0][0]
    assert entry.status == "proposed"
    assert entry.origin == "import"
    assert entry.external_source == "Sintetični lokalni terminološki vir"
    assert entry.external_record_id == "term-1"
    assert entry.source_confidence == "8"
    assert entry.definition == "Proces prehoda materiala v trdno stanje."
    assert entry.import_file_sha256 == preview["file_sha256"]
    assert entry.import_batch_id == preview["import_batch_id"]
    assert entry.imported_at is not None
    assert repository.active_set_hash() == SqliteGlossaryRepository(repository.database_path).active_set_hash()
    assert repository.list_entries(status="approved")[1] == 0

    approved = repository.transition(entry.id, entry.revision, "approved", "approve")
    assert approved.revision == 2
    assert approved.external_source == entry.external_source
    assert [item.action for item in repository.history(entry.id)] == ["import", "approve"]


def test_csv_requires_explicit_mapping_and_preserves_multiple_domains(client: TestClient) -> None:
    content = (
        "source,target,subject,record_id,definition,ignored\n"
        "open time,odprti čas,lepila,csv-1,Čas obdelave,x\n"
        "open time,odprti čas,premazi,csv-2,Čas obdelave,y\n"
    )
    preview = preview_csv(client, content).json()
    assert preview["new_count"] == 2
    assert preview["domains"] == ["lepila", "premazi"]
    assert preview["ignored_fields"] == ["ignored"]
    assert {item["external_record_id"] for item in preview["candidates"]} == {"csv-1", "csv-2"}

    no_mapping = client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "csv", "options_json": options(csv=False)},
        files={"file": ("terms.csv", b"source,target\na,b\n", "text/csv")},
    )
    assert no_mapping.status_code == 422
    assert "preslikava" in no_mapping.json()["detail"]


def test_server_preview_binds_mapping_languages_domain_and_source(client: TestClient) -> None:
    content = "source,target,subject,record_id,definition,ignored\ncuring,strjevanje,lepila,1,d,x\n"
    first = preview_csv(client, content).json()
    first_stored = preview_registry.get(first["preview_id"])
    assert len(first_stored.options_sha256) == 64
    first_candidate = first_stored.candidates[0]
    assert (first_candidate.source_language, first_candidate.target_language, first_candidate.domain) == ("en", "sl", "lepila")
    assert first_candidate.external_source == "Sintetični lokalni terminološki vir"

    changed_options = json.loads(options(csv=True))
    changed_options["default_domain"] = "premazi"
    changed_options["external_source"] = "Drug lokalni vir"
    changed = client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "csv", "options_json": json.dumps(changed_options)},
        files={"file": ("terms.csv", content.encode("utf-8"), "text/csv")},
    ).json()
    changed_stored = preview_registry.get(changed["preview_id"])
    assert changed_stored.options_sha256 != first_stored.options_sha256
    assert changed_stored.candidates[0].external_source == "Drug lokalni vir"


def test_small_local_csv_and_tbx_fixtures_preview_without_writes(client: TestClient, repository: SqliteGlossaryRepository) -> None:
    csv_response = client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "csv", "options_json": options(csv=True)},
        files={"file": ("sample-en-sl.csv", (FIXTURES / "sample-en-sl.csv").read_bytes(), "text/csv")},
    )
    tbx_response = client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "tbx", "options_json": options()},
        files={"file": ("sample-en-sl.tbx", (FIXTURES / "sample-en-sl.tbx").read_bytes(), "application/xml")},
    )
    assert csv_response.status_code == 200 and csv_response.json()["valid_count"] == 2
    assert tbx_response.status_code == 200 and tbx_response.json()["valid_count"] == 1
    assert repository.list_entries()[1] == 0


def test_preview_reports_duplicate_conflict_missing_translation_and_unknown_tbx_field(
    client: TestClient,
    repository: SqliteGlossaryRepository,
) -> None:
    repository.create_entry(GlossaryEntryCreate(
        source_term="curing", target_term="strjevanje", source_language="en", target_language="sl",
        domain="lepila", match_mode="preferred_term", status="approved", origin="manual",
    ))
    duplicate = preview_tbx(client, simple_tbx()).json()
    assert duplicate["duplicate_count"] == 1

    conflict = preview_tbx(client, simple_tbx(target="utrjevanje", extra="<customField>ignored</customField>" )).json()
    assert conflict["conflict_count"] == 1
    assert "customField" in conflict["ignored_fields"]

    missing = preview_tbx(client, """<tbx><text><body><termEntry id="x">
      <langSet xml:lang="en"><tig><term>sealant</term></tig></langSet>
    </termEntry></body></text></tbx>""").json()
    assert missing["missing_language_count"] == 1
    assert missing["valid_count"] == 0


def test_tbx_with_multiple_terms_and_statuses_is_reported_as_ambiguous(client: TestClient) -> None:
    response = preview_tbx(client, """<tbx xmlns="urn:iso:std:iso:30042:ed-2"><text><body><termEntry id="ambiguous">
      <langSet xml:lang="en"><tig><term>sealant</term><termNote type="administrativeStatus">preferredTerm-admn-sts</termNote></tig><tig><term>sealing compound</term><termNote type="administrativeStatus">admittedTerm-admn-sts</termNote></tig></langSet>
      <langSet xml:lang="sl"><ntig><termGrp><term>tesnilna masa</term></termGrp></ntig><tig><term>tesnilo</term></tig></langSet>
    </termEntry></body></text></tbx>""")
    assert response.status_code == 200
    preview = response.json()
    assert preview["valid_count"] == 0
    assert preview["invalid_count"] == 1
    assert preview["candidates"] == []
    issue = next(item for item in preview["issues"] if item["kind"] == "ambiguous")
    assert "2 izvornih in 2 ciljnih" in issue["message"]
    assert "administrativeStatus" in issue["message"]


def test_tbx_rejects_doctype_and_external_entity_without_network_access(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    attempted: list[object] = []
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: attempted.append(args) or (_ for _ in ()).throw(AssertionError("network")))
    malicious = """<?xml version="1.0"?>
<!DOCTYPE tbx [<!ENTITY xxe SYSTEM "http://example.invalid/secret">]>
<tbx><text><body><termEntry id="x"><langSet xml:lang="en"><tig><term>&xxe;</term></tig></langSet>
<langSet xml:lang="sl"><tig><term>x</term></tig></langSet></termEntry></body></text></tbx>"""
    response = preview_tbx(client, malicious)
    assert response.status_code == 422
    assert "DTD ali ENTITY" in response.json()["detail"]
    assert attempted == []
    encoded_bypass = client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "tbx", "options_json": options()},
        files={"file": ("terms.tbx", malicious.encode("utf-16"), "application/xml")},
    )
    assert encoded_bypass.status_code == 422
    assert "UTF-8" in encoded_bypass.json()["detail"]
    assert attempted == []
    wrong_root = preview_tbx(client, "<dictionary><termEntry /></dictionary>")
    assert wrong_root.status_code == 422
    assert "korenskega elementa" in wrong_root.json()["detail"]


def test_uploaded_source_file_is_removed_after_success_and_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temporary_root = tmp_path / "application-temp"
    monkeypatch.setattr(settings, "processing_tmp_dir", temporary_root)
    assert preview_tbx(client, simple_tbx()).status_code == 200
    import_root = temporary_root / "terminology-import"
    assert import_root.exists()
    assert list(import_root.iterdir()) == []
    assert preview_tbx(client, "<!DOCTYPE tbx><tbx />").status_code == 422
    assert list(import_root.iterdir()) == []


def test_uploaded_source_file_is_removed_on_cancellation(
    repository: SqliteGlossaryRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temporary_root = tmp_path / "application-temp"
    monkeypatch.setattr(settings, "processing_tmp_dir", temporary_root)

    async def cancel_copy(_file, destination: Path, **_kwargs):
        destination.write_bytes(b"partial")
        raise asyncio.CancelledError()

    monkeypatch.setattr(terminology_import_api, "_copy_upload", cancel_copy)
    upload = UploadFile(io.BytesIO(b"source,target\na,b\n"), filename="cancel.csv")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(preview_terminology_import(
            file=upload,
            import_format="csv",
            options_json=options(csv=True),
            repository=repository,
        ))
    import_root = temporary_root / "terminology-import"
    assert import_root.exists()
    assert list(import_root.iterdir()) == []


def test_import_limits_file_size_and_record_count(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.terminology_import as import_api

    monkeypatch.setattr(import_api, "MAX_IMPORT_BYTES", 32)
    oversized = preview_csv(client, "source,target\n" + "a,b\n" * 20)
    assert oversized.status_code == 413
    assert "omejitev" in oversized.json()["detail"]

    monkeypatch.setattr(import_api, "MAX_IMPORT_BYTES", 5 * 1024 * 1024)
    too_many = "source,target,subject,record_id,definition,ignored\n" + "\n".join(
        f"s{i},t{i},lepila,{i},d,x" for i in range(1_001)
    )
    response = preview_csv(client, too_many)
    assert response.status_code == 413
    assert "največ 1000" in response.json()["detail"]

    long_field = preview_csv(
        client,
        "source,target,subject,record_id,definition,ignored\ncuring,strjevanje,lepila,1," + "x" * 2_001 + ",x\n",
    )
    assert long_field.status_code == 200
    assert long_field.json()["invalid_count"] == 1


def test_confirm_rejects_tampering_unknown_and_expired_preview(client: TestClient) -> None:
    preview = preview_csv(client, "source,target,subject,record_id,definition,ignored\nsealant,tesnilna masa,lepila,1,d,x\n").json()
    candidate_id = preview["candidates"][0]["candidate_id"]
    changed_hash = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={"preview_id": preview["preview_id"], "file_sha256": "0" * 64, "candidate_ids": [candidate_id]},
    )
    assert changed_hash.status_code == 409
    unknown = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={"preview_id": preview["preview_id"], "file_sha256": preview["file_sha256"], "candidate_ids": ["unknown"]},
    )
    assert unknown.status_code == 409
    tampered_metadata = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={
            "preview_id": preview["preview_id"],
            "file_sha256": preview["file_sha256"],
            "candidate_ids": [candidate_id],
            "external_source": "client replacement",
        },
    )
    assert tampered_metadata.status_code == 422
    preview_registry.clear()
    expired = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={"preview_id": preview["preview_id"], "file_sha256": preview["file_sha256"], "candidate_ids": [candidate_id]},
    )
    assert expired.status_code == 409
    assert "potekel" in expired.json()["detail"]


def test_confirm_rechecks_database_for_new_duplicate_and_conflict(
    client: TestClient,
    repository: SqliteGlossaryRepository,
) -> None:
    duplicate_preview = preview_csv(
        client,
        "source,target,subject,record_id,definition,ignored\nsealant,tesnilna masa,lepila,1,d,x\n",
    ).json()
    repository.create_entry(GlossaryEntryCreate(
        source_term="sealant", target_term="tesnilna masa", source_language="en", target_language="sl",
        domain="lepila", match_mode="preferred_term", status="approved", origin="manual",
    ))
    duplicate_result = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={
            "preview_id": duplicate_preview["preview_id"],
            "file_sha256": duplicate_preview["file_sha256"],
            "candidate_ids": [duplicate_preview["candidates"][0]["candidate_id"]],
        },
    ).json()
    assert duplicate_result["created"] == 0
    assert duplicate_result["skipped_duplicates"] == 1
    assert duplicate_result["skipped_conflicts"] == 0

    conflict_preview = preview_csv(
        client,
        "source,target,subject,record_id,definition,ignored\nopen time,odprti čas,lepila,2,d,x\n",
    ).json()
    repository.create_entry(GlossaryEntryCreate(
        source_term="open time", target_term="čas obdelave", source_language="en", target_language="sl",
        domain="lepila", match_mode="preferred_term", status="approved", origin="manual",
    ))
    conflict_result = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={
            "preview_id": conflict_preview["preview_id"],
            "file_sha256": conflict_preview["file_sha256"],
            "candidate_ids": [conflict_preview["candidates"][0]["candidate_id"]],
        },
    ).json()
    assert conflict_result["created"] == 0
    assert conflict_result["skipped_duplicates"] == 0
    assert conflict_result["skipped_conflicts"] == 1
    assert repository.list_entries()[1] == 2


def test_expected_preview_conflict_can_be_imported_but_changed_revision_is_stale(
    client: TestClient,
    repository: SqliteGlossaryRepository,
) -> None:
    existing = repository.create_entry(GlossaryEntryCreate(
        source_term="curing", target_term="strjevanje", source_language="en", target_language="sl",
        domain="lepila", match_mode="preferred_term", status="approved", origin="manual",
    ))
    preview = preview_tbx(client, simple_tbx(target="utrjevanje")).json()
    candidate = preview["candidates"][0]
    assert candidate["classification"] == "conflict"
    unchanged_result = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={"preview_id": preview["preview_id"], "file_sha256": preview["file_sha256"], "candidate_ids": [candidate["candidate_id"]]},
    ).json()
    assert unchanged_result["created"] == 1
    assert unchanged_result["skipped_conflicts"] == 0

    second = repository.create_entry(GlossaryEntryCreate(
        source_term="sealant", target_term="tesnilna masa", source_language="en", target_language="sl",
        domain="lepila", match_mode="preferred_term", status="approved", origin="manual",
    ))
    stale_preview = preview_tbx(client, simple_tbx(entry_id="term-2", target="tesnilo").replace("curing", "sealant")).json()
    stale_candidate = stale_preview["candidates"][0]
    repository.transition(second.id, second.revision, "archived", "archive")
    stale_result = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={"preview_id": stale_preview["preview_id"], "file_sha256": stale_preview["file_sha256"], "candidate_ids": [stale_candidate["candidate_id"]]},
    ).json()
    assert stale_result["created"] == 0
    assert stale_result["skipped_conflicts"] == 1


def test_preview_registry_is_bounded_expires_and_consumes_tokens_once(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminology_import_service, "MAX_ACTIVE_PREVIEWS", 1)
    first = preview_csv(client, "source,target,subject,record_id,definition,ignored\na,b,d,1,x,x\n")
    assert first.status_code == 200
    assert preview_registry.usage()[0] == 1
    full = preview_csv(client, "source,target,subject,record_id,definition,ignored\nc,d,d,2,x,x\n")
    assert full.status_code == 413

    preview_registry.clear()
    monkeypatch.setattr(terminology_import_service, "MAX_ACTIVE_PREVIEWS", 16)
    monkeypatch.setattr(terminology_import_service, "MAX_PREVIEW_REGISTRY_BYTES", 1)
    too_large = preview_csv(client, "source,target,subject,record_id,definition,ignored\na,b,d,1,x,x\n")
    assert too_large.status_code == 413

    preview_registry.clear()
    monkeypatch.setattr(terminology_import_service, "MAX_PREVIEW_REGISTRY_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(terminology_import_service, "PREVIEW_TTL", timedelta(seconds=-1))
    expired = preview_csv(client, "source,target,subject,record_id,definition,ignored\na,b,d,1,x,x\n").json()
    assert preview_registry.usage() == (0, 0)
    expired_confirm = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={"preview_id": expired["preview_id"], "file_sha256": expired["file_sha256"], "candidate_ids": [expired["candidates"][0]["candidate_id"]]},
    )
    assert expired_confirm.status_code == 409

    monkeypatch.setattr(terminology_import_service, "PREVIEW_TTL", timedelta(minutes=30))
    active = preview_csv(client, "source,target,subject,record_id,definition,ignored\na,b,d,1,x,x\n").json()
    payload = {"preview_id": active["preview_id"], "file_sha256": active["file_sha256"], "candidate_ids": [active["candidates"][0]["candidate_id"]]}
    assert client.post("/api/glossary/imports/terminology/confirm", json=payload).status_code == 200
    assert preview_registry.usage() == (0, 0)
    assert client.post("/api/glossary/imports/terminology/confirm", json=payload).status_code == 409


def test_csv_accepts_utf8_bom_and_crlf_and_rejects_invalid_encoding(client: TestClient) -> None:
    valid = client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "csv", "options_json": options(csv=True)},
        files={"file": ("terms.csv", b"\xef\xbb\xbfsource,target,subject,record_id,definition,ignored\r\ncuring,strjevanje,lepila,1,d,x\r\n", "text/csv")},
    )
    assert valid.status_code == 200
    assert valid.json()["valid_count"] == 1
    invalid = client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "csv", "options_json": options(csv=True)},
        files={"file": ("terms.csv", b"source,target\r\n\xff,broken\r\n", "text/csv")},
    )
    assert invalid.status_code == 422
    assert "UTF-8" in invalid.json()["detail"]


def test_imported_formula_like_term_is_stored_exactly_and_exported_safely(client: TestClient, repository: SqliteGlossaryRepository) -> None:
    response = preview_csv(
        client,
        "source,target,subject,record_id,definition,ignored\n=SUM(A1:A2),formula,testing,formula-1,d,x\n",
    )
    preview = response.json()
    candidate_id = preview["candidates"][0]["candidate_id"]
    confirmed = client.post(
        "/api/glossary/imports/terminology/confirm",
        json={"preview_id": preview["preview_id"], "file_sha256": preview["file_sha256"], "candidate_ids": [candidate_id]},
    )
    assert confirmed.status_code == 200
    assert repository.list_entries()[0][0].source_term == "=SUM(A1:A2)"
    exported = client.get("/api/glossary/export.csv").text
    assert "'=SUM(A1:A2)" in exported
    assert "source_term" in exported


def test_import_candidates_roll_back_as_one_transaction(repository: SqliteGlossaryRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = [
        TerminologyImportCandidate(
            candidate_id=f"candidate-{index}", row_number=index, source_term=f"source {index}",
            target_term=f"target {index}", source_language="en", target_language="sl",
            external_source="test",
        ) for index in (1, 2)
    ]
    calls = 0
    original = repository._append_history

    def fail_second(connection, entry, action):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic failure")
        original(connection, entry, action)

    monkeypatch.setattr(repository, "_append_history", fail_second)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        repository.import_candidates(candidates, import_batch_id="batch", file_sha256="a" * 64)
    assert repository.list_entries()[1] == 0


def test_proposed_external_candidate_does_not_enter_translation_snapshot(repository: SqliteGlossaryRepository) -> None:
    candidate = TerminologyImportCandidate(
        candidate_id="candidate", row_number=1, source_term="open time", target_term="odprti čas",
        source_language="en", target_language="sl", external_source="test",
    )
    repository.import_candidates([candidate], import_batch_id="batch", file_sha256="b" * 64)
    assert repository.list_entries(status="approved")[1] == 0
    before = repository.active_set_hash()
    entry = repository.list_entries()[0][0]
    repository.transition(entry.id, entry.revision, "approved", "approve")
    assert repository.active_set_hash() != before
