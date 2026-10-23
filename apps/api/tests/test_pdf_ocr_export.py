import hashlib
import io
import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient

from app.pdf_export_models import PdfExportJob
from app.main import app
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_exporter import export_translated_pdf, plan_pdf_export
from app.services.ocr_review_registry import OcrReviewRegistry, ocr_review_registry
import app.services.pdf_exporter as pdf_exporter


OCR_TEXT = "Warranty period"
TRANSLATION = "Garancijsko obdobje"
client = TestClient(app)


@pytest.mark.parametrize("damage", [None, "unaccepted", "unknown", "document", "text", "bbox", "page", "id", "source", "reason"])
def test_import_verification_never_registers_renews_or_approves_ocr(tmp_path: Path, damage):
    source, bbox = _make_image_text_pdf(tmp_path / "verify.pdf")
    values = dict(document_sha256=_hash(source), review_id="synthetic-import", page_number=1,
        bbox=_bbox(bbox), text=OCR_TEXT, source="manual", reason="manual_selection")
    if damage == "document":
        values["document_sha256"] = "b" * 64
    revision = ocr_review_registry.register(**values)
    if damage != "unaccepted":
        ocr_review_registry.accept(revision.revision_id)
    candidate = {k: v for k, v in values.items() if k != "document_sha256"}
    candidate.update(bbox=bbox, revision_id=revision.revision_id)
    if damage == "unknown": candidate["revision_id"] = "unknown"
    if damage == "text": candidate["text"] += " changed"
    if damage == "bbox": candidate["bbox"] = {**bbox, "x0": bbox["x0"] + 1}
    if damage == "page": candidate["page_number"] = 2
    if damage == "id": candidate["review_id"] = "other-id"
    if damage == "source": candidate["source"] = "block"
    if damage == "reason": candidate["reason"] = "other-reason"
    before = dict(ocr_review_registry._records)
    response = client.post("/api/pdf/ocr-review/verify",
        files={"file": (source.name, source.read_bytes(), "application/pdf")},
        data={"sources": json.dumps({"sources": [candidate]})})
    assert response.status_code == 200, response.text
    result = response.json()["sources"][0]
    assert result["valid"] is (damage is None)
    assert (result["reason"] is None) is (damage is None)
    assert ocr_review_registry._records == before


@pytest.mark.parametrize("obstacle", ["none", "text", "same_span", "line", "image"])
def test_native_ocr_write_space_does_not_change_accepted_source(tmp_path: Path, obstacle: str) -> None:
    source = tmp_path / "write-region.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=200)
        page.insert_text((40, 60), "Warrantv period NEIGHBOR" if obstacle == "same_span" else "Warrantv period", fontsize=12)
        bbox = dict(zip(("x0", "y0", "x1", "y1"), page.search_for("Warrantv period")[0]))
        if obstacle == "text":
            page.insert_text((125, 60), "NEIGHBOR", fontsize=12)
        elif obstacle == "line":
            page.draw_line((125, 40), (125, 75), color=(0, 0, 0))
        elif obstacle == "image":
            stream = io.BytesIO();Image.new("RGB", (40, 40), "navy").save(stream, format="PNG")
            page.insert_image((125, 40, 160, 75), stream=stream.getvalue())
        document.save(source)
    payload = _job_payload(source, bbox)
    payload["document_identity"]["pages"][0]["width"] = 600
    target = "Dolgo garancijsko obdobje"
    payload["segments"][0]["edited_translation"] = target
    immutable = json.dumps(payload, sort_keys=True)
    artifact = export_translated_pdf(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "write"))
    item = artifact.plan.ocr_replacements[0]
    assert item.eligible is (obstacle == "none")
    assert item.source_bbox.model_dump() == bbox
    assert json.dumps(payload, sort_keys=True) == immutable
    if item.eligible:
        assert "verified_native_ocr_write_region" in item.typography_warnings
        assert item.redaction_rects == [item.source_bbox]
        assert item.fitted_bbox.x1 > bbox["x1"]
    with pymupdf.open(artifact.path) as output:
        assert (target in output[0].get_text()) is (obstacle == "none")
        if obstacle in {"text", "same_span"}:assert "NEIGHBOR" in output[0].get_text()


@pytest.mark.parametrize("reviewed", [False, True])
def test_registered_native_ocr_correction_requires_separate_translation_review(tmp_path: Path, reviewed: bool) -> None:
    source = tmp_path / "native-ocr.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((40, 60), "Warrantv period", fontsize=12)
        bbox = dict(zip(("x0", "y0", "x1", "y1"), page.search_for("Warrantv period")[0]))
        document.save(source)
    # Synthetic review state, not approval of any real document translation.
    payload = _job_payload(source, bbox)
    payload["segments"][0]["edited_translation"] = "Jamstvo čšž"
    payload["segments"][0]["status"] = "reviewed" if reviewed else "translated"
    artifact = export_translated_pdf(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "native"))
    assert artifact.applied_replacements == int(reviewed)
    with pymupdf.open(artifact.path) as output:
        assert ("Jamstvo čšž" in output[0].get_text()) is reviewed
        assert ("Warrantv period" in output[0].get_text()) is not reviewed
    assert _hash(source) == payload["document_identity"]["sha256"]


def test_api_registers_then_separately_accepts_verified_pdf_source(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "api-register.pdf")
    candidate = {
        "review_id": "api-review", "page_number": 1, "bbox": bbox, "text": OCR_TEXT,
        "source": "manual", "reason": "manual_selection",
    }
    registration = client.post(
        "/api/pdf/ocr-review/register",
        files={"file": (source.name, source.read_bytes(), "application/pdf")},
        data={"source": json.dumps(candidate)},
    )
    assert registration.status_code == 200
    assert registration.json()["accepted"] is False
    acceptance = client.post("/api/pdf/ocr-review/accept", json={"revision_id": registration.json()["revision_id"]})
    assert acceptance.status_code == 200
    assert acceptance.json()["accepted"] is True

    payload = _job_payload(source, bbox)
    segment = payload["segments"][0]
    segment["accepted_ocr_source"] = {
        "revision_id": registration.json()["revision_id"], "review_id": candidate["review_id"],
        "accepted_text": OCR_TEXT, "bbox": bbox, "source": "manual", "reason": "manual_selection",
    }
    exported = client.post(
        "/api/pdf/export-translated",
        files={"file": (source.name, source.read_bytes(), "application/pdf")},
        data={"translation_job": json.dumps(payload), "mode": "replacement_preview"},
    )
    assert exported.status_code == 200
    output = pymupdf.open(stream=exported.content, filetype="pdf")
    original = pymupdf.open(source)
    replacement = client.post(
        "/api/pdf/export-translated/plan",
        files={"file": (source.name, source.read_bytes(), "application/pdf")},
        data={"translation_job": json.dumps(payload), "mode": "replacement_preview"},
    ).json()["ocr_replacements"][0]
    assert replacement["eligible"] is True
    assert replacement["scale"] is None or replacement["scale"] >= 0.75
    assert TRANSLATION in output[0].get_text()
    assert _image_digests_from_document(output) == _image_digests_from_document(original)
    assert _hash(source) == payload["document_identity"]["sha256"]
    original_pixels = original[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).samples
    output_pixels = output[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).samples
    assert original_pixels != output_pixels
    output.close()
    original.close()


def test_api_rejects_ocr_bbox_outside_verified_pdf(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "api-bbox.pdf")
    bbox["x1"] = 500
    response = client.post(
        "/api/pdf/ocr-review/register",
        files={"file": (source.name, source.read_bytes(), "application/pdf")},
        data={"source": json.dumps({
            "review_id": "outside", "page_number": 1, "bbox": bbox, "text": OCR_TEXT,
            "source": "manual", "reason": "manual_selection",
        })},
    )
    assert response.status_code == 400
    assert "znotraj" in response.json()["detail"]


def test_ocr_export_requires_separate_translation_review(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "pending.pdf")
    payload = _job_payload(source, bbox)
    payload["segments"][0]["status"] = "translated"

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "pending"))

    assert plan.ocr_replacements[0].skip_reason == "ocr_not_accepted"
    assert plan.ocr_summary.eligible_regions == 0


def test_changed_ocr_source_or_bbox_is_stale(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "stale.pdf")
    for field in ("accepted_text", "bbox"):
        payload = _job_payload(source, bbox)
        if field == "accepted_text":
            payload["segments"][0]["accepted_ocr_source"][field] = "Changed source"
        else:
            payload["segments"][0]["accepted_ocr_source"][field]["x1"] -= 4
        plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, field))
        assert plan.ocr_replacements[0].skip_reason == "ocr_provenance_mismatch"


def test_coordinated_frontend_change_is_rejected_by_server_registry(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "coordinated.pdf")
    payload = _job_payload(source, bbox)
    payload["segments"][0]["source_text"] = "Changed together"
    payload["segments"][0]["accepted_ocr_source"]["accepted_text"] = "Changed together"
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "coordinated"))
    assert plan.ocr_replacements[0].skip_reason == "ocr_registry_mismatch"
    assert plan.ocr_summary.stale_regions == 1


@pytest.mark.parametrize(
    ("state", "reason"),
    [("unknown", "ocr_registry_missing"), ("unaccepted", "ocr_registry_unaccepted"), ("stale", "ocr_registry_stale")],
)
def test_export_rejects_non_current_server_revision(tmp_path: Path, state: str, reason: str) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / f"registry-{state}.pdf")
    payload = _job_payload(source, bbox)
    provenance = payload["segments"][0]["accepted_ocr_source"]
    if state == "unknown":
        provenance["revision_id"] = "missing-revision"
    elif state == "unaccepted":
        revision = ocr_review_registry.register(
            document_sha256=payload["document_identity"]["sha256"], review_id="review-1", page_number=1,
            bbox=_bbox(bbox), text=OCR_TEXT, source="manual", reason="manual_selection",
        )
        provenance["revision_id"] = revision.revision_id
    else:
        ocr_review_registry.register(
            document_sha256=payload["document_identity"]["sha256"], review_id="review-1", page_number=1,
            bbox=_bbox(bbox), text="New revision", source="manual", reason="manual_selection",
        )
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, state))
    assert plan.ocr_replacements[0].skip_reason == reason
    assert plan.ocr_summary.stale_regions == 1


def test_export_fails_closed_after_process_registry_is_lost(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "restart.pdf")
    payload = _job_payload(source, bbox)
    ocr_review_registry.clear()
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "restart"))
    assert plan.ocr_replacements[0].skip_reason == "ocr_registry_missing"


def test_uniform_ocr_region_exports_extractable_text_without_changing_source_image(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "uniform.pdf")
    source_hash = _hash(source)
    before = _image_digests(source)

    artifact = export_translated_pdf(
        source, PdfExportJob.model_validate(_job_payload(source, bbox)), "replacement_preview", _work(tmp_path, "uniform")
    )

    replacement = artifact.plan.ocr_replacements[0]
    assert replacement.eligible is True
    assert replacement.background_patch_used is True
    assert replacement.fit_strategy in {"exact", "scaled"}
    assert _hash(source) == source_hash
    assert _image_digests(artifact.path) == before
    with pymupdf.open(artifact.path) as output:
        assert TRANSLATION in output[0].get_text()


def test_graphic_ocr_region_is_explainably_skipped(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "graphic.pdf", complex_background=True)
    artifact = export_translated_pdf(
        source, PdfExportJob.model_validate(_job_payload(source, bbox)), "replacement_preview", _work(tmp_path, "graphic")
    )
    assert artifact.plan.ocr_replacements[0].skip_reason == "ocr_background_unsafe"
    assert _image_digests(artifact.path) == _image_digests(source)


def test_large_connected_graphic_inside_ocr_region_is_skipped(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "large-graphic.pdf", large_graphic=True)
    artifact = export_translated_pdf(
        source, PdfExportJob.model_validate(_job_payload(source, bbox)), "replacement_preview", _work(tmp_path, "large-graphic")
    )
    replacement = artifact.plan.ocr_replacements[0]
    assert replacement.skip_reason == "ocr_background_unsafe"
    assert "large_graphic_component_in_ocr_region" in replacement.image_overlap_warnings


def test_ocr_overflow_and_replacement_conflicts_keep_source_unchanged(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "conflicts.pdf")
    source_hash = _hash(source)
    payload = _job_payload(source, bbox)
    payload["segments"][0]["edited_translation"] = "zelo dolgo prevedeno besedilo " * 100
    overflow = export_translated_pdf(
        source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "overflow")
    )
    assert overflow.plan.ocr_replacements[0].skip_reason == "overflow"
    assert _page_digest(overflow.path) == _page_digest(source)

    payload = _job_payload(source, bbox)
    duplicate = {**payload["segments"][0], "segment_id": "ocr-review-2", "block_id": "ocr-review-2"}
    payload["segments"].append(duplicate)
    conflict = plan_pdf_export(
        source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "conflict")
    )
    assert all(item.skip_reason == "overlap_conflict" for item in conflict.ocr_replacements)
    assert _hash(source) == source_hash


def test_ocr_fitting_uses_common_75_percent_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "fit-boundary.pdf")
    scales: list[float] = []

    def boundary(*args, **kwargs):
        scale = args[-1]
        scales.append(scale)
        return (-1.0, scale, 1)

    monkeypatch.setattr(pdf_exporter, "_htmlbox_preflight", boundary)
    plan = plan_pdf_export(source, PdfExportJob.model_validate(_job_payload(source, bbox)), "replacement_preview", _work(tmp_path, "below"))
    assert scales == [1.0, 0.75]
    assert plan.ocr_replacements[0].skip_reason == "overflow"

    scales.clear()
    def exact_boundary(*args, **kwargs):
        scale = args[-1]
        scales.append(scale)
        return (-1.0, scale, 1) if scale == 1.0 else (0.0, scale, 1)

    monkeypatch.setattr(pdf_exporter, "_htmlbox_preflight", exact_boundary)
    plan = plan_pdf_export(source, PdfExportJob.model_validate(_job_payload(source, bbox)), "replacement_preview", _work(tmp_path, "boundary"))
    assert plan.ocr_replacements[0].eligible is True
    assert plan.ocr_replacements[0].scale == 0.75


def test_registry_rejects_unknown_unaccepted_stale_mismatched_and_restart_state() -> None:
    registry = OcrReviewRegistry()
    bbox = _bbox({"x0": 1, "y0": 2, "x1": 20, "y1": 10})
    values = dict(document_sha256="a" * 64, review_id="review", page_number=1, bbox=bbox, text="OCR")
    assert registry.validate(revision_id="missing", **values) == "unknown"
    first = registry.register(**values, source="manual", reason="manual_selection")
    assert registry.validate(revision_id=first.revision_id, **values) == "unaccepted"
    assert registry.accept(first.revision_id) is not None
    second = registry.register(**{**values, "text": "Changed"}, source="manual", reason="manual_selection")
    assert registry.validate(revision_id=first.revision_id, **values) == "stale"
    assert registry.accept(second.revision_id) is not None
    assert registry.validate(revision_id=second.revision_id, **{**values, "text": "Changed", "document_sha256": "b" * 64}) == "document_mismatch"
    assert registry.validate(revision_id=second.revision_id, **{**values, "text": "Changed", "bbox": _bbox({"x0": 2, "y0": 2, "x1": 20, "y1": 10})}) == "source_mismatch"
    assert OcrReviewRegistry().validate(revision_id=second.revision_id, **{**values, "text": "Changed"}) == "unknown"


def test_pdf_identity_mismatch_rejects_ocr_export(tmp_path: Path) -> None:
    source, bbox = _make_image_text_pdf(tmp_path / "identity.pdf")
    payload = _job_payload(source, bbox)
    payload["document_identity"]["sha256"] = "0" * 64
    try:
        plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, "identity"))
    except ValueError as error:
        assert "drugemu PDF" in str(error)
    else:
        raise AssertionError("Identity mismatch was accepted")


def _make_image_text_pdf(
    path: Path, *, complex_background: bool = False, large_graphic: bool = False
) -> tuple[Path, dict[str, float]]:
    image = Image.new("RGB", (400, 140), "white")
    draw = ImageDraw.Draw(image)
    if complex_background:
        for x in range(0, image.width, 8):
            draw.rectangle((x, 0, x + 3, image.height), fill=(70, 120, 180))
    if large_graphic:
        draw.rectangle((20, 20, 220, 120), outline="black", width=8)
    draw.text((30, 45), OCR_TEXT, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_image((30, 35, 270, 119), stream=stream.getvalue())
        document.save(path)
    # The accepted bbox is tight around only the synthetic text, with a small clean ring.
    return path, {"x0": 45.0, "y0": 55.0, "x1": 145.0, "y1": 85.0}


def _job_payload(path: Path, bbox: dict[str, float]) -> dict:
    analysis = analyze_pdf(path)
    revision = ocr_review_registry.register(
        document_sha256=analysis.sha256, review_id="review-1", page_number=1,
        bbox=_bbox(bbox), text=OCR_TEXT, source="manual", reason="manual_selection",
    )
    accepted = ocr_review_registry.accept(revision.revision_id)
    assert accepted is not None
    return {
        "schema_version": 4,
        "source_filename": path.name,
        "document_identity": {
            "sha256": analysis.sha256,
            "page_count": analysis.page_count,
            "document_model_version": analysis.document_model_version,
            "pages": [{"page_number": 1, "width": 300, "height": 200, "rotation": 0}],
        },
        "segments": [{
            "segment_id": "ocr-review-1",
            "page_number": 1,
            "block_id": "ocr-review-1",
            "block_type": "paragraph",
            "source_text": OCR_TEXT,
            "source_text_origin": "accepted_ocr",
            "source_bbox": bbox,
            "source_page_type": "digital",
            "source_rotation": 0,
            "source_style": None,
            "accepted_ocr_source": {
                "revision_id": revision.revision_id,
                "review_id": "review-1", "accepted_text": OCR_TEXT, "bbox": dict(bbox),
                "source": "manual", "reason": "manual_selection",
            },
            "edited_translation": TRANSLATION,
            "status": "reviewed",
            "translation_origin": "manual",
        }],
    }


def _bbox(value: dict[str, float]):
    from app.models import BBox
    return BBox(**value)


def _image_digests(path: Path) -> list[str]:
    with pymupdf.open(path) as document:
        return [hashlib.sha256(document.extract_image(xref)["image"]).hexdigest() for xref, *_ in document[0].get_images(full=True)]


def _image_digests_from_document(document: pymupdf.Document) -> list[str]:
    return [hashlib.sha256(document.extract_image(xref)["image"]).hexdigest() for xref, *_ in document[0].get_images(full=True)]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _page_digest(path: Path) -> str:
    with pymupdf.open(path) as document:
        return hashlib.sha256(document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).samples).hexdigest()


def _work(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path
