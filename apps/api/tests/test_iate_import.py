from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.services.terminology_import as terminology_import_service
from app.api.glossary import get_glossary_repository
from app.main import app
from app.services.glossary_repository import SqliteGlossaryRepository
from app.services.terminology_import import IATE_HEADERS, preview_registry

FIXTURE = Path(__file__).parent / "fixtures" / "terminology" / "sample-iate-en-sl.csv"
LOCAL_IATE = Path(__file__).parents[3] / "local-fixtures" / "IATE_building_en-sl_extended_2026-09-24.csv"


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


def options() -> str:
    return json.dumps({
        "source_language": "en", "target_language": "sl", "default_domain": "",
        "default_match_mode": "preferred_term", "external_source": "client value is not authoritative",
        "source_version": "2026", "license_info": "IATE reuse notice",
    })


def row(record_id: str, language: str, term: str, *, domain: str = "building", reliability: str = "Reliable",
        evaluation: str = "", primarity: str = "1", lifecycle: str = "", institution: str = "COM") -> str:
    return "|".join((record_id, domain, language, term, "Term", reliability, evaluation, primarity,
                     lifecycle, "IATE", institution, "2026-09-24"))


def content(*rows: str) -> str:
    return "|".join(IATE_HEADERS) + "\n" + "\n".join(rows) + "\n"


def quoted_content(rows: list[list[str]], *, line_ending: str = "\n") -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="|", quotechar='"', lineterminator=line_ending)
    writer.writerow(IATE_HEADERS)
    writer.writerows(rows)
    return output.getvalue()


def preview(client: TestClient, value: str | bytes):
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return client.post(
        "/api/glossary/imports/terminology/preview",
        data={"import_format": "iate_csv", "options_json": options()},
        files={"file": ("iate.csv", payload, "text/csv")},
    )


def test_iate_fixture_detects_exact_pipe_profile_groups_out_of_order_and_is_dry_run(
    client: TestClient, repository: SqliteGlossaryRepository,
) -> None:
    response = preview(client, FIXTURE.read_bytes())
    assert response.status_code == 200
    result = response.json()
    assert result["format"] == "iate_csv"
    assert result["input_row_count"] == 6
    assert result["concept_count"] == 3
    assert result["source_row_count"] == 4
    assert result["target_row_count"] == 2
    assert result["safe_candidate_count"] == 1
    assert result["missing_language_count"] == 1
    assert result["raw_multi_term_count"] == 1
    assert result["multi_term_count"] == 1
    assert (
        result["missing_language_count"]
        + result["multi_term_count"]
        + result["rejected_status_or_lifecycle_count"]
        + result["interconcept_conflict_count"]
        + result["safe_candidate_count"]
    ) == result["concept_count"]
    candidate = result["candidates"][0]
    assert (candidate["source_term"], candidate["target_term"]) == ("open time", "odprti čas")
    assert candidate["domain"] == "building|public works"
    assert candidate["external_source"] == "Download IATE, European Union, 2026"
    assert candidate["external_record_id"] == "100"
    assert candidate["source_confidence"] == "Reliable"
    assert candidate["external_evaluation"] == "Preferred"
    assert repository.list_entries()[1] == 0


def test_iate_requires_exact_header_supports_bom_line_endings_and_rejects_invalid_utf8(client: TestClient) -> None:
    valid = preview(client, b"\xef\xbb\xbf" + content(row("1", "en", "a"), row("1", "sl", "b")).encode())
    assert valid.status_code == 200
    crlf = preview(client, content(row("1", "en", "a"), row("1", "sl", "b")).replace("\n", "\r\n"))
    assert crlf.status_code == 200
    header = list(IATE_HEADERS)
    invalid_headers = (
        header[:-1],
        [*header, "EXTRA"],
        [header[0], *header],
        [header[1], header[0], *header[2:]],
    )
    for invalid_header in invalid_headers:
        wrong = preview(client, "|".join(invalid_header) + "\n")
        assert wrong.status_code == 422 and "glave" in wrong.json()["detail"]
    invalid = preview(client, b"|".join(name.encode() for name in IATE_HEADERS) + b"\n1|x|en|\xff")
    assert invalid.status_code == 422 and "UTF-8" in invalid.json()["detail"]


def test_iate_parses_quoted_delimiters_quotes_and_newlines_without_changing_terms(client: TestClient) -> None:
    rows = [
        ["1", "building|public works", "en", 'literal "pipe|term"\nline', "Term", "Reliable", "Preferred", "1", "", "IATE", "COM", "2026-09-24"],
        ["1", "building|public works", "sl", 'dobesedni "termin|cev"\nvrstica', "Term", "Reliable", "Preferred", "1", "", "IATE", "COM", "2026-09-24"],
    ]
    response = preview(client, quoted_content(rows, line_ending="\r\n"))
    assert response.status_code == 200
    candidate = response.json()["candidates"][0]
    assert candidate["source_term"] == rows[0][3]
    assert candidate["target_term"] == rows[1][3]
    assert candidate["domain"] == "building|public works"


def test_iate_reports_empty_identifier_language_and_term_without_guessing(client: TestClient) -> None:
    result = preview(client, content(
        row("", "en", "orphan"),
        row("1", "", "unknown language"), row("1", "sl", "slovensko"),
        row("2", "en", ""), row("2", "sl", "cilj"),
    )).json()
    assert result["concept_count"] == 2
    assert result["missing_language_count"] == 2
    assert result["safe_candidate_count"] == 0
    assert any(issue["kind"] == "invalid" and "E_ID" in issue["message"] for issue in result["issues"])


def test_iate_never_builds_cartesian_pairs_and_reports_duplicate_and_metadata_conflict(client: TestClient) -> None:
    duplicate = row("1", "en", "open time")
    result = preview(client, content(
        duplicate, duplicate, row("1", "sl", "odprti čas"), row("1", "sl", "čas odprtosti"),
        row("2", "en", "sealant"), row("2", "en", "sealant", reliability="Very reliable"), row("2", "sl", "tesnilo"),
    )).json()
    assert result["candidates"] == []
    assert result["duplicate_row_count"] == 1
    assert result["raw_multi_term_count"] == 1
    assert result["multi_term_count"] == 2
    assert {issue["kind"] for issue in result["issues"]} >= {"duplicate_row", "ambiguous", "metadata_conflict"}


def test_iate_rejects_status_lifecycle_unknowns_and_cross_concept_conflicts(client: TestClient) -> None:
    result = preview(client, content(
        row("1", "en", "a", evaluation="Proposed"), row("1", "sl", "aa", evaluation="Proposed"),
        row("2", "en", "b", lifecycle="historical"), row("2", "sl", "bb", lifecycle="historical"),
        row("3", "en", "c", evaluation="Unverified"), row("3", "sl", "cc", evaluation="Unverified"),
        row("4", "en", "same"), row("4", "sl", "prvi"),
        row("5", "en", "same"), row("5", "sl", "drugi"),
    )).json()
    assert result["safe_candidate_count"] == 0
    assert result["status_rejected_count"] == 2
    assert result["lifecycle_rejected_count"] == 1
    assert result["interconcept_conflict_count"] == 2
    assert result["interconcept_conflict_group_count"] == 1
    assert {issue["kind"] for issue in result["issues"]} >= {
        "status_rejected", "lifecycle_rejected", "unknown_status", "interconcept_conflict",
    }


@pytest.mark.parametrize(("constant", "limit_message"), [
    ("MAX_IATE_ROWS", "vhodnih vrstic"), ("MAX_IATE_CONCEPTS", "različnih E_ID"),
    ("MAX_IATE_CANDIDATES", "ustvariti največ"),
])
def test_iate_has_separate_bounded_limits(client: TestClient, monkeypatch: pytest.MonkeyPatch, constant: str, limit_message: str) -> None:
    monkeypatch.setattr(terminology_import_service, constant, 1)
    response = preview(client, content(
        row("1", "en", "a"), row("1", "sl", "aa"), row("2", "en", "b"), row("2", "sl", "bb"),
    ))
    assert response.status_code == 413
    assert limit_message in response.json()["detail"]


def test_iate_conflict_normalization_is_conservative_and_keeps_original_terms(client: TestClient) -> None:
    literal = "__TK_0001__ + RAL 9001"
    result = preview(client, content(
        row("1", "en", "same term"), row("1", "sl", "isti termin"),
        row("2", "en", "same term"), row("2", "sl", "isti termin"),
        row("3", "en", "Case Term"), row("3", "sl", "prvi"),
        row("4", "en", "case term"), row("4", "sl", "drugi"),
        row("5", "en", literal), row("5", "sl", "__PHX_0001__ + RAL 9001"),
    )).json()
    assert result["interconcept_conflict_count"] == 0
    assert result["safe_candidate_count"] == 5
    assert any(item["source_term"] == literal for item in result["candidates"])
    assert {item["source_term"] for item in result["candidates"]} >= {"Case Term", "case term"}


def test_iate_candidate_ids_are_preview_specific_and_cross_preview_selection_is_rejected(client: TestClient) -> None:
    value = content(row("1", "en", "open time"), row("1", "sl", "odprti čas"))
    first = preview(client, value).json()
    second = preview(client, value).json()
    first_id = first["candidates"][0]["candidate_id"]
    second_id = second["candidates"][0]["candidate_id"]
    assert first_id != second_id
    response = client.post("/api/glossary/imports/terminology/confirm", json={
        "preview_id": second["preview_id"], "file_sha256": second["file_sha256"], "candidate_ids": [first_id],
    })
    assert response.status_code == 409 and "neznanega kandidata" in response.json()["detail"]


def test_iate_confirm_with_no_selection_is_rejected_without_writing(
    client: TestClient, repository: SqliteGlossaryRepository,
) -> None:
    result = preview(client, content(row("1", "en", "a"), row("1", "sl", "b"))).json()
    payload = {"preview_id": result["preview_id"], "file_sha256": result["file_sha256"], "candidate_ids": []}
    response = client.post("/api/glossary/imports/terminology/confirm", json=payload)
    assert response.status_code == 422
    assert repository.list_entries()[1] == 0
    assert client.post("/api/glossary/imports/terminology/confirm", json=payload).status_code == 422


def test_iate_confirm_is_single_use_imports_only_selected_as_proposed_and_preserves_metadata(
    client: TestClient, repository: SqliteGlossaryRepository,
) -> None:
    result = preview(client, content(
        row("1", "en", "open time"), row("1", "sl", "odprti čas"),
        row("2", "en", "sealant"), row("2", "sl", "tesnilna masa"),
    )).json()
    selected = result["candidates"][0]
    payload = {"preview_id": result["preview_id"], "file_sha256": result["file_sha256"], "candidate_ids": [selected["candidate_id"]]}
    confirmed = client.post("/api/glossary/imports/terminology/confirm", json=payload)
    assert confirmed.status_code == 200 and confirmed.json()["created"] == 1
    assert confirmed.json()["skipped_unselected"] == 1
    entry = repository.list_entries()[0][0]
    assert entry.status == "proposed"
    assert entry.external_record_id == selected["external_record_id"]
    assert entry.external_evaluation == selected["external_evaluation"]
    assert entry.external_institution == "COM"
    assert entry.external_source == "Download IATE, European Union, 2026"
    assert entry.import_file_sha256 == result["file_sha256"]
    assert entry.revision == 1
    history = repository.history(entry.id)
    assert len(history) == 1 and history[0].action == "import" and history[0].revision == 1
    assert client.post("/api/glossary/imports/terminology/confirm", json=payload).status_code == 409


def test_iate_confirm_rechecks_database_toctou(client: TestClient, repository: SqliteGlossaryRepository) -> None:
    result = preview(client, content(row("1", "en", "open time"), row("1", "sl", "odprti čas"))).json()
    candidate = result["candidates"][0]
    from app.glossary_models import GlossaryEntryCreate
    repository.create_entry(GlossaryEntryCreate(
        source_term="open time", target_term="drug prevod", source_language="en", target_language="sl",
        domain="building", match_mode="preferred_term", status="approved", origin="manual",
    ))
    confirmed = client.post("/api/glossary/imports/terminology/confirm", json={
        "preview_id": result["preview_id"], "file_sha256": result["file_sha256"], "candidate_ids": [candidate["candidate_id"]],
    }).json()
    assert confirmed["created"] == 0 and confirmed["skipped_conflicts"] == 1


def test_iate_parser_does_not_open_network_connections(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    attempted: list[object] = []
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: attempted.append(args))
    assert preview(client, content(row("1", "en", "a"), row("1", "sl", "b"))).status_code == 200
    assert attempted == []


def test_iate_formula_like_terms_use_existing_safe_reversible_csv_export(
    client: TestClient, repository: SqliteGlossaryRepository,
) -> None:
    sources = ["=SUM(A1:A2)", "+CMD", "@SUM", "-40 °C", "-1,5"]
    rows = [row(str(index), language, term) for index, source in enumerate(sources, 1)
            for language, term in (("en", source), ("sl", f"cilj {index}"))]
    result = preview(client, content(*rows)).json()
    payload = {"preview_id": result["preview_id"], "file_sha256": result["file_sha256"],
               "candidate_ids": [item["candidate_id"] for item in result["candidates"]]}
    assert client.post("/api/glossary/imports/terminology/confirm", json=payload).json()["created"] == 5
    exported = client.get("/api/glossary/export.csv").content.decode("utf-8-sig")
    exported_rows = {item["source_term"]: item for item in csv.DictReader(io.StringIO(exported))}
    assert "'=SUM(A1:A2)" in exported_rows
    assert "'+CMD" in exported_rows
    assert "'@SUM" in exported_rows
    assert "-40 °C" in exported_rows
    assert "-1,5" in exported_rows


@pytest.mark.skipif(not LOCAL_IATE.exists(), reason="Zasebni lokalni IATE izvoz ni prisoten.")
def test_local_iate_export_has_an_exclusive_complete_concept_partition(client: TestClient) -> None:
    result = preview(client, LOCAL_IATE.read_bytes()).json()
    assert result["concept_count"] == 7_136
    assert result["raw_multi_term_count"] == 1_928
    assert result["missing_language_count"] == 6_644
    assert result["multi_term_count"] == 159
    assert result["rejected_status_or_lifecycle_count"] == 2
    assert result["interconcept_conflict_count"] == 2
    assert result["interconcept_conflict_group_count"] == 1
    assert result["safe_candidate_count"] == 329
    assert 6_644 + 159 + 2 + 2 + 329 == 7_136
