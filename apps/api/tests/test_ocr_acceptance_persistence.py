from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings, settings
from app.main import app
from app.models import BBox
from app.services.ocr_acceptance_repository import OcrAcceptanceStorageError, SqliteOcrAcceptanceRepository
from app.services import ocr_review_registry as registry_module
from app.services.ocr_review_registry import OcrReviewRegistry, _configured_repository, ocr_review_registry
from test_pdf_ocr_export import _make_image_text_pdf, _job_payload


def values(**updates):
    return {"document_sha256": "a" * 64, "review_id": "review-1", "page_number": 1,
        "bbox": BBox(x0=1, y0=2, x1=30, y1=20), "text": "Accepted source", "source": "manual",
        "reason": "manual_selection", **updates}


def durable(path: Path, profile="test", ttl=1):
    return OcrReviewRegistry(ttl, repository_factory=lambda: SqliteOcrAcceptanceRepository(path, profile))


def accepted(registry, **updates):
    revision = registry.register(**values(**updates))
    assert registry.accept(revision.revision_id).revision_id == revision.revision_id
    return revision


def test_restart_and_registration_expiry_keep_same_accepted_reference(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(registry_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    path = tmp_path / "ocr.sqlite3"
    registry = durable(path)
    start = datetime.now(UTC)
    revision = accepted(registry)
    record, current = SqliteOcrAcceptanceRepository(path, "test").lookup(revision.revision_id)
    assert current and start <= datetime.fromisoformat(record.accepted_at) <= datetime.now(UTC)
    clock[0] += 1801
    assert registry.validate(revision_id=revision.revision_id, **values()) is None
    assert registry.accept(revision.revision_id) is None  # Pending registration capability expired.
    assert not registry._records
    assert durable(path).validate(revision_id=revision.revision_id, **values()) is None
    assert SqliteOcrAcceptanceRepository(path, "test").lookup(revision.revision_id)[0] == record
    script = """import json, sys
from pathlib import Path
from app.models import BBox
from app.services.ocr_review_registry import OcrReviewRegistry
from app.services.ocr_acceptance_repository import SqliteOcrAcceptanceRepository
data = json.loads(sys.argv[3]); data['bbox'] = BBox.model_validate(data['bbox'])
registry = OcrReviewRegistry(repository_factory=lambda: SqliteOcrAcceptanceRepository(Path(sys.argv[1]), 'test'))
print(json.dumps(registry.validate(revision_id=sys.argv[2], **data)))
"""
    data = {**values(), "bbox": values()["bbox"].model_dump()}
    child = subprocess.run([sys.executable, "-B", "-c", script, str(path), revision.revision_id, json.dumps(data)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(child.stdout) is None


def test_pending_recognition_does_not_supersede_until_explicit_accept(tmp_path):
    path = tmp_path / "ocr.sqlite3"
    old_worker, new_worker = durable(path), durable(path)
    old = accepted(old_worker)
    new = new_worker.register(**values(text="Corrected source"))
    assert old_worker.validate(revision_id=old.revision_id, **values()) is None
    assert new_worker.validate(revision_id=new.revision_id, **values(text="Corrected source")) == "unaccepted"
    assert new_worker.accept(new.revision_id)
    assert old_worker.validate(revision_id=old.revision_id, **values()) == "stale"
    assert old_worker.accept(old.revision_id) is None  # Cannot reactivate it from another worker.
    assert durable(path).validate(revision_id=new.revision_id, **values(text="Corrected source")) is None
    # Even an identical recognition remains a new unaccepted registration.
    pending = new_worker.register(**values(text="Corrected source"))
    assert new_worker.validate(revision_id=new.revision_id, **values(text="Corrected source")) is None
    assert pending.revision_id != new.revision_id


@pytest.mark.parametrize("change, expected", [
    ({"document_sha256": "b" * 64}, "document_mismatch"),
    ({"page_number": 2}, "source_mismatch"), ({"review_id": "other"}, "source_mismatch"),
    ({"text": "Changed"}, "source_mismatch"), ({"source": "block"}, "source_mismatch"),
    ({"reason": "other"}, "source_mismatch"),
    ({"bbox": BBox(x0=1.001, y0=2, x1=30, y1=20)}, "source_mismatch"),
])
def test_restarted_validation_checks_every_source_field(tmp_path, change, expected):
    path = tmp_path / "ocr.sqlite3"
    revision = accepted(durable(path))
    assert durable(path).validate(revision_id=revision.revision_id, **values(**change)) == expected
    assert durable(path).validate(revision_id="forged-reference", **values()) == "unknown"


def test_profiles_are_isolated_even_in_same_database(tmp_path):
    path = tmp_path / "shared.sqlite3"
    revision = accepted(durable(path, "alpha"))
    assert durable(path, "beta").validate(revision_id=revision.revision_id, **values()) == "unknown"
    accepted(durable(path, "beta"), text="Other profile")
    assert durable(path, "alpha").validate(revision_id=revision.revision_id, **values()) is None


def test_repeat_accept_keeps_original_utc_metadata(tmp_path):
    path = tmp_path / "ocr.sqlite3"
    registry = durable(path)
    revision = accepted(registry)
    repository = SqliteOcrAcceptanceRepository(path, "test")
    original = repository.lookup(revision.revision_id)
    assert registry.accept(revision.revision_id)
    assert repository.lookup(revision.revision_id) == original


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_bbox_cannot_become_durable(tmp_path, coordinate):
    path = tmp_path / "ocr.sqlite3"
    registry = durable(path)
    revision = registry.register(**values(bbox=BBox(x0=coordinate, y0=2, x1=30, y1=20)))
    with pytest.raises(OcrAcceptanceStorageError):
        registry.accept(revision.revision_id)
    assert not registry._records[revision.revision_id].accepted
    assert not path.exists()


def test_pending_expiry_and_legacy_acceptance_never_become_durable(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(registry_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    path = tmp_path / "ocr.sqlite3"
    registry = durable(path)
    accepted(registry, review_id="seed")
    pending = registry.register(**values())
    assert registry.validate(revision_id=pending.revision_id, **values()) == "unaccepted"
    clock[0] += 2
    assert registry.accept(pending.revision_id) is None
    assert registry.validate(revision_id=pending.revision_id, **values()) == "unknown"
    legacy = OcrReviewRegistry()
    old = accepted(legacy)
    legacy._repository_factory = lambda: SqliteOcrAcceptanceRepository(path, "test")
    assert legacy.accept(old.revision_id) is None
    assert legacy.validate(revision_id=old.revision_id, **values()) == "unknown"


def test_dynamic_config_isolates_pending_and_durable_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "glossary_database_path", tmp_path / "glossary.sqlite3")
    monkeypatch.setattr(settings, "ocr_database_path", None)
    monkeypatch.setattr(settings, "local_data_profile", "alpha")
    first_path = settings.resolved_ocr_database_path
    assert first_path.parent == tmp_path
    monkeypatch.setattr(settings, "local_data_profile", "beta")
    assert settings.resolved_ocr_database_path != first_path
    monkeypatch.setattr(settings, "ocr_database_path", tmp_path / "shared.sqlite3")
    monkeypatch.setattr(ocr_review_registry, "_repository_factory", _configured_repository)
    revision = accepted(ocr_review_registry)
    pending = ocr_review_registry.register(**values(review_id="pending"))
    monkeypatch.setattr(settings, "local_data_profile", "alpha")
    assert ocr_review_registry.accept(pending.revision_id) is None
    assert ocr_review_registry.validate(revision_id=revision.revision_id, **values()) == "unknown"
    monkeypatch.setattr(settings, "local_data_profile", "beta")
    assert ocr_review_registry.validate(revision_id=revision.revision_id, **values()) is None
    monkeypatch.setattr(settings, "ocr_database_path", tmp_path / "different.sqlite3")
    assert ocr_review_registry.validate(revision_id=revision.revision_id, **values()) == "unknown"
    assert Settings(_env_file=None, ocr_database_path=tmp_path / "explicit.sqlite3").resolved_ocr_database_path == tmp_path / "explicit.sqlite3"


def test_verification_is_read_only_and_missing_or_corrupt_storage_fails_closed(tmp_path):
    path = tmp_path / "missing-parent" / "ocr.sqlite3"
    registry = durable(path)
    pending = registry.register(**values())
    assert not path.parent.exists()
    assert registry.validate(revision_id=pending.revision_id, **values()) == "unaccepted"
    assert durable(path).validate(revision_id="forged", **values()) == "unknown"
    assert not path.parent.exists()
    revision = accepted(registry)
    snapshot = path.read_bytes()
    files = set(path.parent.iterdir())
    for _ in range(2):
        assert durable(path).validate(revision_id=revision.revision_id, **values()) is None
    assert path.read_bytes() == snapshot and set(path.parent.iterdir()) == files
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(OcrAcceptanceStorageError):
        registry.validate(revision_id=revision.revision_id, **values())


def test_accept_transaction_rolls_back_if_head_write_fails(tmp_path):
    path = tmp_path / "ocr.sqlite3"
    registry = durable(path)
    old = accepted(registry)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_head BEFORE UPDATE ON ocr_acceptance_heads BEGIN SELECT RAISE(ABORT, 'failure'); END")
    new = registry.register(**values(text="New source"))
    with pytest.raises(OcrAcceptanceStorageError):
        registry.accept(new.revision_id)
    assert not registry._records[new.revision_id].accepted
    assert durable(path).validate(revision_id=old.revision_id, **values()) is None
    assert SqliteOcrAcceptanceRepository(path, "test").lookup(new.revision_id)[0] is None


def test_api_verify_and_export_survive_expiry_and_restart_without_approving_translation(tmp_path, monkeypatch):
    path = tmp_path / "ocr.sqlite3"
    clock = [100.0]
    monkeypatch.setattr(registry_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(ocr_review_registry, "_repository_factory", lambda: SqliteOcrAcceptanceRepository(path, "test"))
    source, bbox = _make_image_text_pdf(tmp_path / "source.pdf")
    payload = _job_payload(source, bbox)  # Explicit synthetic source acceptance only.
    provenance = payload["segments"][0]["accepted_ocr_source"]
    candidate = {"revision_id": provenance["revision_id"], "review_id": provenance["review_id"],
        "page_number": 1, "bbox": bbox, "text": provenance["accepted_text"],
        "source": provenance["source"], "reason": provenance["reason"]}
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    clock[0] += 1801
    assert ocr_review_registry.accept(provenance["revision_id"]) is None
    # Expiry alone must not revoke the durable record, even before restart.
    with TestClient(app) as client:
        response = client.post("/api/pdf/ocr-review/verify", files={"file": (source.name, source.read_bytes(), "application/pdf")},
            data={"sources": json.dumps({"sources": [candidate]})})
        assert response.status_code == 200 and response.json()["sources"][0]["valid"]
        for endpoint in ("/plan", ""):
            response = client.post("/api/pdf/export-translated" + endpoint,
                files={"file": (source.name, source.read_bytes(), "application/pdf")},
                data={"translation_job": json.dumps(payload), "mode": "replacement_preview"})
            assert response.status_code == 200, response.text
            if endpoint:
                assert response.json()["ocr_replacements"][0]["eligible"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    ocr_review_registry.clear()  # Fresh-process memory; persistent acceptance remains authoritative.
    with TestClient(app) as client:
        response = client.post("/api/pdf/ocr-review/verify", files={"file": (source.name, source.read_bytes(), "application/pdf")},
            data={"sources": json.dumps({"sources": [candidate]})})
        assert response.status_code == 200 and response.json()["sources"] == [
            {"revision_id": provenance["revision_id"], "valid": True, "reason": None}]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        response = client.post("/api/pdf/export-translated", files={"file": (source.name, source.read_bytes(), "application/pdf")},
            data={"translation_job": json.dumps(payload), "mode": "replacement_preview"})
        assert response.status_code == 200, response.text
        payload["segments"][0]["status"] = "translated"
        response = client.post("/api/pdf/export-translated/plan", files={"file": (source.name, source.read_bytes(), "application/pdf")},
            data={"translation_job": json.dumps(payload), "mode": "replacement_preview"})
        assert response.status_code == 200
        assert not response.json()["ocr_replacements"][0]["eligible"]


def test_api_returns_503_without_false_acceptance_or_database_creation(tmp_path, monkeypatch):
    path = tmp_path / "ocr.sqlite3"
    monkeypatch.setattr(ocr_review_registry, "_repository_factory", lambda: SqliteOcrAcceptanceRepository(path, "test"))
    source, bbox = _make_image_text_pdf(tmp_path / "source.pdf")
    candidate = {"review_id": "synthetic", "page_number": 1, "bbox": bbox, "text": "Source",
                 "source": "manual", "reason": "manual_selection"}
    with TestClient(app) as client:
        response = client.post("/api/pdf/ocr-review/verify", files={"file": (source.name, source.read_bytes(), "application/pdf")},
            data={"sources": json.dumps({"sources": [{**candidate, "revision_id": "forged"}]})})
        assert response.status_code == 200 and response.json()["sources"][0]["reason"] == "unknown"
        assert not path.exists()
        registration = client.post("/api/pdf/ocr-review/register", files={"file": (source.name, source.read_bytes(), "application/pdf")},
            data={"source": json.dumps(candidate)})
        assert registration.status_code == 200 and not path.exists()
        revision_id = registration.json()["revision_id"]
        path.write_bytes(b"corrupt")
        response = client.post("/api/pdf/ocr-review/accept", json={"revision_id": revision_id})
        assert response.status_code == 503 and response.json()["detail"]["code"] == "ocr_storage_unavailable"
        assert not ocr_review_registry._records[revision_id].accepted


@pytest.mark.parametrize("state", ["pending", "superseded", "corrupt", "missing"])
def test_export_uses_durable_current_revision_and_propagates_storage_failure(tmp_path, monkeypatch, state):
    path = tmp_path / "ocr.sqlite3"
    monkeypatch.setattr(ocr_review_registry, "_repository_factory", lambda: SqliteOcrAcceptanceRepository(path, "test"))
    source, bbox = _make_image_text_pdf(tmp_path / "source.pdf")
    payload = _job_payload(source, bbox)
    p = payload["segments"][0]["accepted_ocr_source"]
    new = ocr_review_registry.register(document_sha256=payload["document_identity"]["sha256"],
        review_id=p["review_id"], page_number=1, bbox=BBox.model_validate(bbox), text="Changed source",
        source=p["source"], reason=p["reason"])
    if state == "superseded":
        assert ocr_review_registry.accept(new.revision_id)
    elif state == "corrupt":
        path.write_bytes(b"corrupt existing database")
    elif state == "missing":
        path.unlink()
    with TestClient(app) as client:
        for endpoint in ("plan", "") if state == "corrupt" else ("plan",):
            response = client.post("/api/pdf/export-translated" + ("/" + endpoint if endpoint else ""),
                files={"file": (source.name, source.read_bytes(), "application/pdf")},
                data={"translation_job": json.dumps(payload), "mode": "replacement_preview"})
            if state == "corrupt":
                assert response.status_code == 503 and response.json()["detail"]["code"] == "ocr_storage_unavailable"
            else:
                assert response.status_code == 200, response.text
                item = response.json()["ocr_replacements"][0]
                assert item["eligible"] is (state == "pending")
                if state != "pending":
                    assert item["skip_reason"] == ("ocr_registry_stale" if state == "superseded" else "ocr_registry_missing")
