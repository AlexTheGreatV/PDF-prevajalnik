import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.technical_integrity import validate_technical_integrity
from test_pdf_export import _job_payload, _make_pdf

CASES = json.loads((Path(__file__).parent / "fixtures/protected-data-cases.json").read_text(encoding="utf-8"))
client = TestClient(app)


@pytest.mark.parametrize("case", CASES, ids=lambda row: row["id"])
def test_shared_protected_data_contract(case):
    assert validate_technical_integrity(case["source"], case["target"]).valid is case["valid"]


def request(path, payload, endpoint):
    return client.post("/api/pdf/export-translated" + endpoint,
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"mode": "replacement_preview", "translation_job": json.dumps(payload)})


@pytest.mark.parametrize("endpoint", ["/plan", ""])
@pytest.mark.parametrize("profile", ["original", "document-role-typography-pilot-v1"])
@pytest.mark.parametrize("source,target", [
    ("-40 °C to +90 °C", "od −41 do +90 °C"),
    ("-40 °C to +90 °C", "od −40 do +90 K"),
    ("-40 °C to +90 °C", "od 40 do +90 °C"),
    ("-40 °C to +90 °C", "od +90 do −40 °C"),
    ("Use 2K paint.", "Uporabite dvokomponentno barvo."),
])
def test_direct_api_rejects_false_reviewed_and_passed_qc(tmp_path, endpoint, profile, source, target):
    path = tmp_path / "technical.pdf"
    _make_pdf(path, source_text=source)
    payload = _job_payload(path, source, target)
    payload["typography_profile"] = profile
    segment = payload["segments"][0]
    segment.update(protected_tokens=[], current_qc={"status": "passed"}, review_state={"origin": "forged"})
    response = request(path, payload, endpoint)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "protected_data_mismatch"
    assert detail["segment_id"] == "segment-1"
    assert detail["expected"] != detail["detected"]
    assert detail["stage"] == "target"


@pytest.mark.parametrize("endpoint", ["/plan", ""])
def test_tampered_source_cannot_hide_changed_target_or_tokens(tmp_path, endpoint):
    path = tmp_path / "source.pdf"
    source = "Pressure resistance 2.5 MPa and coating 2K."
    _make_pdf(path, source_text=source)
    payload = _job_payload(path, source, "Tlak 2,6 MPa, premaz 3K.")
    payload["segments"][0]["source_text"] = source.replace("2.5", "2.6").replace("2K", "3K")
    payload["segments"][0]["protected_tokens"] = [{"value": "2.6 MPa"}, {"value": "3K"}]
    response = request(path, payload, endpoint)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["stage"] == "source"
    assert "2,5 MPa" in response.json()["detail"]["expected"]


@pytest.mark.parametrize("source,target", [
    ("-40 °C to +90 °C", "od −40 do +90 °C"),
    ("+5 °C to 35 °C", "od 5 do 35 °C"),
    ("Use 2K paint.", "Uporabite dvokomponentno barvo (2K)."),
])
def test_valid_slovenian_forms_plan_and_write_extractable_pdf(tmp_path, source, target):
    import pymupdf
    from app.services.pdf_exporter import _written_text_matches
    path = tmp_path / "valid.pdf"
    _make_pdf(path, source_text=source)
    payload = _job_payload(path, source, target)
    plan = request(path, payload, "/plan")
    assert plan.status_code == 200, plan.text
    assert plan.json()["resolution_summary"]["replacement_planned"] == 1
    response = request(path, payload, "")
    assert response.status_code == 200, response.text
    assert response.headers["X-PDF-Replacements-Applied"] == "1"
    with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
        assert _written_text_matches(target, pdf[0].get_text())


def test_export_recomputes_integrity_after_valid_plan(tmp_path):
    path = tmp_path / "changed.pdf"
    source = "-40 °C to +90 °C"
    _make_pdf(path, source_text=source)
    payload = _job_payload(path, source, "od −40 do +90 °C")
    assert request(path, payload, "/plan").status_code == 200
    payload["segments"][0]["edited_translation"] = "od −41 do +90 °C"
    response = request(path, payload, "")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "protected_data_mismatch"


@pytest.mark.parametrize("endpoint", ["/plan", ""])
def test_provenance_cell_uses_server_cell_text_not_client_token_list(tmp_path, endpoint):
    from test_pdf_table_export import _make_baseline_table, _job
    path = tmp_path / "table.pdf"
    _make_baseline_table(path)
    job = _job(path, translations={(0, 0): "Trdnost 2,6 MPa"})
    payload = job.model_dump(mode="json")
    payload["segments"][0]["protected_tokens"] = []
    response = request(path, payload, endpoint)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["expected"] == ["2,5 MPa"]


def test_ocr_integrity_is_bound_to_accepted_server_revision(tmp_path):
    from app.services.ocr_review_registry import ocr_review_registry
    path = tmp_path / "ocr.pdf"
    source = "Pressure 2.5 MPa"
    _make_pdf(path, source_text=source)
    payload = _job_payload(path, source, "Tlak 2,5 MPa")
    s = payload["segments"][0]
    registration = {"review_id": "synthetic-review", "page_number": 1,
                    "bbox": s["source_bbox"], "text": source, "source": "manual", "reason": "manual_selection"}
    response = client.post("/api/pdf/ocr-review/register", files={"file": (path.name, path.read_bytes(), "application/pdf")}, data={"source": json.dumps(registration)})
    assert response.status_code == 200, response.text
    revision = response.json()["revision_id"]
    assert client.post("/api/pdf/ocr-review/accept", json={"revision_id": revision}).status_code == 200
    s["source_text_origin"] = "accepted_ocr"
    s["accepted_ocr_source"] = {"revision_id": revision, "review_id": "synthetic-review", "accepted_text": source,
                                "bbox": s["source_bbox"], "source": "manual", "reason": "manual_selection"}
    # Synthetic acceptance is test setup, never approval of a real user's text.
    for endpoint in ["/plan", ""]:
        invalid = copy.deepcopy(payload)
        invalid["segments"][0]["edited_translation"] = "Tlak 2,6 MPa"
        response = request(path, invalid, endpoint)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "protected_data_mismatch"
    forged = copy.deepcopy(payload)
    forged["segments"][0]["source_text"] = "Pressure 2.6 MPa"
    forged["segments"][0]["accepted_ocr_source"]["accepted_text"] = "Pressure 2.6 MPa"
    plan = request(path, forged, "/plan")
    assert plan.status_code == 200
    assert plan.json()["ocr_replacements"][0]["skip_reason"] == "ocr_registry_mismatch"
    assert plan.json()["ocr_summary"]["eligible_regions"] == 0
    output = request(path, forged, "")
    assert output.status_code == 200
    assert output.headers["X-PDF-Replacements-Applied"] == "0"
    del s["accepted_ocr_source"]["revision_id"]
    # Missing server evidence remains blocked even for an otherwise valid target.
    unlinked = request(path, payload, "/plan")
    assert unlinked.status_code == 200
    assert unlinked.json()["ocr_replacements"][0]["skip_reason"] == "ocr_registry_missing"
    ocr_review_registry.clear()
