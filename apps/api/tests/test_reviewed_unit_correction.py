"""One synthetic unit spelling approval, never a real document's review."""
import hashlib
import json

from app.core.config import settings
from app.services.technical_integrity import validate_technical_integrity
from test_technical_integrity import request
from test_pdf_export import _make_pdf, _job_payload
import pytest


@pytest.fixture
def approved(tmp_path, monkeypatch):
    source = tmp_path / "quantity.pdf"
    _make_pdf(source, source_text="1.59 gr/ml")
    payload = _job_payload(source, "1.59 gr/ml", "1,59 g/ml")
    row = {"version": 1, "profile": "unit-test", "document_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "page_number": 1, "source_bbox": payload["segments"][0]["source_bbox"], "source_kind": "native",
        "segment_id": "segment-1", "source_text": "1.59 gr/ml", "target_unit": "g/ml",
        "approval_note": "Synthetic test-only approval", "recorded_at": "2026-11-03T00:00:00+00:00"}
    path = tmp_path / "one-approval.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr(settings, "local_data_profile", "unit-test")
    monkeypatch.setattr(settings, "reviewed_unit_correction_path", path)
    monkeypatch.setattr(settings, "reviewed_unit_correction_sha256", hashlib.sha256(path.read_bytes()).hexdigest())
    return source, payload, path, row


@pytest.mark.parametrize("endpoint", ["/plan", ""])
def test_one_pinned_whole_quantity_is_allowed_in_plan_and_writer(approved, endpoint):
    source, payload, _, _ = approved
    from test_technical_integrity import client
    environment = client.get("/api/environment")
    assert environment.status_code == 200
    assert environment.json()["reviewed_unit_correction"]["evidence_sha256"] == settings.reviewed_unit_correction_sha256
    response = request(source, payload, endpoint)
    assert response.status_code == 200, response.text
    if endpoint:
        assert response.json()["resolution_summary"]["replacement_planned"] == 1
    else:
        import pymupdf
        assert response.headers["X-PDF-Replacements-Applied"] == "1"
        with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
            assert "g/ml" in pdf[0].get_text() and "gr/ml" not in pdf[0].get_text()


@pytest.mark.parametrize("endpoint", ["/plan", ""])
@pytest.mark.parametrize("target", ["1,60 g/ml", "1,590 g/ml", "-1,59 g/ml", "+1,59 g/ml",
    "1,59 kg/ml", "1,59 g/L", "1,59 MPa", "1,59 g/ml and 20 L"])
def test_approval_never_changes_value_precision_sign_other_units_or_text(approved, endpoint, target):
    source, payload, _, _ = approved
    payload["segments"][0]["edited_translation"] = target
    response = request(source, payload, endpoint)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "protected_data_mismatch"


@pytest.mark.parametrize("endpoint", ["/plan", ""])
@pytest.mark.parametrize("change", ["segment", "document", "source", "profile", "evidence", "client-claim"])
def test_evidence_is_server_pinned_and_identity_bound(approved, endpoint, change, monkeypatch):
    source, payload, evidence, row = approved
    if change == "segment":
        payload["segments"][0]["segment_id"] = "another-segment"
    elif change == "document":
        import pymupdf
        with pymupdf.open(source) as pdf:
            pdf.set_metadata({"title": "Different PDF identity"})
            altered = source.with_name("altered.pdf"); pdf.save(altered)
        source = altered; payload = _job_payload(source, "1.59 gr/ml", "1,59 g/ml")
    elif change == "source":
        payload["segments"][0]["source_text"] = "1.59 g/ml"
    elif change == "profile":
        monkeypatch.setattr(settings, "local_data_profile", "other-profile")
    elif change == "evidence":
        evidence.write_text(json.dumps({**row, "target_unit": "kg/ml"}), encoding="utf-8")
    else:
        monkeypatch.setattr(settings, "reviewed_unit_correction_path", None)
        monkeypatch.setattr(settings, "reviewed_unit_correction_sha256", None)
        payload["reviewed_unit_correction"] = row
        payload["segments"][0]["current_qc"] = {"status": "passed"}
    response = request(source, payload, endpoint)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] in {"protected_data_mismatch", "reviewed_unit_evidence_invalid"}


def test_even_a_badly_reviewed_record_cannot_approve_different_digits(approved, monkeypatch):
    _, _, evidence, row = approved
    evidence.write_text(json.dumps({**row, "source_text": "1.60 gr/ml"}), encoding="utf-8")
    monkeypatch.setattr(settings, "reviewed_unit_correction_sha256", hashlib.sha256(evidence.read_bytes()).hexdigest())
    assert not validate_technical_integrity("1.60 gr/ml", "1,59 g/ml",
        document_sha256=row["document_sha256"], segment_id=row["segment_id"]).valid


@pytest.mark.parametrize("endpoint", ["/plan", ""])
def test_same_approved_id_cannot_be_reused_at_another_quantity_region(approved, monkeypatch, endpoint):
    import pymupdf
    source, _, evidence, row = approved
    with pymupdf.open(source) as pdf:
        pdf[0].insert_text((30, 130), "1.59 gr/ml", fontsize=11)
        altered = source.with_name("two-regions.pdf")
        pdf.save(altered)
    payload = _job_payload(altered, "1.59 gr/ml", "1,59 g/ml")
    with pymupdf.open(altered) as pdf:
        boxes = pdf[0].search_for("1.59 gr/ml")
        assert len(boxes) == 2
        second = boxes[1]
        payload["segments"][0]["source_bbox"] = dict(zip(("x0", "y0", "x1", "y1"), second))
    # Synthetic permission is for the first occurrence, never the second.
    row["document_sha256"] = hashlib.sha256(altered.read_bytes()).hexdigest()
    evidence.write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr(settings, "reviewed_unit_correction_sha256", hashlib.sha256(evidence.read_bytes()).hexdigest())
    response = request(altered, payload, endpoint)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "protected_data_mismatch"
