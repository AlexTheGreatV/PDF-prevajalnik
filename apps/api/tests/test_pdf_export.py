import hashlib
import io
import json
import socket
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import settings
from app.main import app
from app.pdf_export_models import PdfExportJob
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_exporter import PdfExportValidationError, export_translated_pdf, plan_pdf_export

SOURCE_TEXT = "Digital source text for a safe translated PDF preview."
TRANSLATED_TEXT = "Varen prevod čšžČŠŽ, CO₂ in +5 °C."
client = TestClient(app)


def test_replacement_preserves_original_file_images_vectors_background_and_unicode(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_pdf(source, include_image=True)
    original_hash = _hash(source)
    before = _content_counts(source)
    job = _job(source)

    artifact = export_translated_pdf(source, job, "replacement_preview", tmp_path / "work")

    assert _hash(source) == original_hash
    assert artifact.path != source
    assert artifact.path.name == "source-sl-preview.pdf"
    with pymupdf.open(artifact.path) as output:
        text = output[0].get_text()
        assert SOURCE_TEXT not in text
        assert TRANSLATED_TEXT in text
        assert len(output[0].get_images(full=True)) == before[0]
        assert len(output[0].get_drawings()) >= before[1]
        pixel = output[0].get_pixmap().pixel(32, 32)
        assert pixel[1] > pixel[0] and pixel[1] > pixel[2]


def test_segment_overlapping_embedded_image_is_skipped(tmp_path: Path) -> None:
    source = tmp_path / "image-overlap.pdf"
    _make_pdf(source, include_image=True)
    payload = _job_payload(source)
    payload["segments"][0]["source_bbox"] = {"x0": 240, "y0": 150, "x1": 270, "y1": 180}

    plan = plan_pdf_export(
        source,
        PdfExportJob.model_validate(payload),
        "replacement_preview",
        _work(tmp_path, "image-overlap"),
    )

    assert plan.replacements[0].eligible is False
    assert plan.replacements[0].skip_reason == "image_overlap"


def test_diagnostic_mode_keeps_source_text(tmp_path: Path) -> None:
    source = tmp_path / "diagnostic.pdf"
    _make_pdf(source)
    artifact = export_translated_pdf(source, _job(source), "diagnostic", tmp_path / "work")
    with pymupdf.open(artifact.path) as output:
        text = output[0].get_text()
        assert SOURCE_TEXT in text
        assert "segment-1" in text


def test_identity_rejects_hash_page_count_and_geometry_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "identity.pdf"
    _make_pdf(source)
    payload = _job_payload(source)

    payload["document_identity"]["sha256"] = "0" * 64
    with pytest.raises(PdfExportValidationError, match="drugemu PDF"):
        plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, "hash"))

    payload = _job_payload(source)
    payload["document_identity"]["page_count"] = 2
    with pytest.raises(PdfExportValidationError, match="Število strani"):
        plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, "count"))

    payload = _job_payload(source)
    payload["document_identity"]["pages"][0]["width"] += 10
    with pytest.raises(PdfExportValidationError, match="Geometrija"):
        plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, "geometry"))


def test_plan_exports_only_reviewed_and_reports_invalid_regions(tmp_path: Path) -> None:
    source = tmp_path / "eligibility.pdf"
    _make_pdf(source)
    payload = _job_payload(source)
    pending = {**payload["segments"][0], "segment_id": "pending", "status": "translated"}
    outside = {**payload["segments"][0], "segment_id": "outside", "source_bbox": {"x0": -5, "y0": 0, "x1": 20, "y1": 20}}
    mismatch = {**payload["segments"][0], "segment_id": "mismatch", "source_text": "Completely unrelated source"}
    payload["segments"].extend((pending, outside, mismatch))

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "plan"))

    assert plan.summary.reviewed == 3
    assert plan.summary.eligible == 1
    reasons = {item.segment_id: item.skip_reason for item in plan.replacements}
    assert reasons == {"segment-1": None, "pending": "not_reviewed", "outside": "bbox_outside_page", "mismatch": "source_text_mismatch"}


def test_overlapping_replacements_are_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "overlap.pdf"
    _make_pdf(source)
    payload = _job_payload(source)
    payload["segments"].append({**payload["segments"][0], "segment_id": "segment-2"})
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "overlap"))
    assert plan.summary.conflicts == 2
    assert all(item.skip_reason == "overlap_conflict" for item in plan.replacements)


def test_bbox_touching_page_edge_remains_valid(tmp_path: Path) -> None:
    source = tmp_path / "edge.pdf"
    _make_pdf(source)
    payload = _job_payload(source)
    payload["segments"][0]["source_bbox"]["x0"] = 0
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, "edge"))
    assert plan.summary.eligible == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"source_text_origin": "accepted_ocr"}, "ocr_only_source"),
        ({"source_page_type": "scanned"}, "non_digital_source"),
        ({"block_type": "table_cell", "source_bbox": None}, "complex_table"),
    ],
)
def test_ambiguous_sources_are_skipped(tmp_path: Path, changes: dict, reason: str) -> None:
    source = tmp_path / f"{reason}.pdf"
    _make_pdf(source)
    payload = _job_payload(source)
    payload["segments"][0].update(changes)
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, reason))
    assert plan.replacements[0].skip_reason == reason


def test_overflow_keeps_original_text(tmp_path: Path) -> None:
    source = tmp_path / "overflow.pdf"
    _make_pdf(source)
    payload = _job_payload(source)
    payload["segments"][0]["edited_translation"] = "zelo dolgo besedilo " * 200
    job = PdfExportJob.model_validate(payload)
    artifact = export_translated_pdf(source, job, "replacement_preview", _work(tmp_path, "overflow"))
    assert artifact.plan.replacements[0].skip_reason == "overflow"
    with pymupdf.open(artifact.path) as output:
        assert SOURCE_TEXT in output[0].get_text()


def test_rotated_page_is_explainably_skipped(tmp_path: Path) -> None:
    source = tmp_path / "rotated.pdf"
    _make_pdf(source, rotation=90)
    plan = plan_pdf_export(source, _job(source), "replacement_preview", _work(tmp_path, "rotated"))
    assert plan.summary.eligible == 0
    assert plan.replacements[0].skip_reason == "rotated_page"


def test_export_does_not_use_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "offline.pdf"
    _make_pdf(source)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    artifact = export_translated_pdf(source, _job(source), "diagnostic", _work(tmp_path, "offline"))
    assert artifact.path.exists()


def test_export_endpoint_returns_new_pdf_and_cleans_temp_on_success_and_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "endpoint.pdf"
    processing = tmp_path / "processing"
    _make_pdf(source)
    monkeypatch.setattr(settings, "processing_tmp_dir", processing)
    job = _job_payload(source)
    response = client.post(
        "/api/pdf/export-translated",
        data={"translation_job": json.dumps(job), "mode": "replacement_preview"},
        files={"file": (source.name, source.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert "endpoint-sl-preview.pdf" in response.headers["content-disposition"]
    assert list(processing.iterdir()) == []

    job["document_identity"]["sha256"] = "0" * 64
    rejected = client.post(
        "/api/pdf/export-translated/plan",
        data={"translation_job": json.dumps(job), "mode": "diagnostic"},
        files={"file": (source.name, source.read_bytes(), "application/pdf")},
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "pdf_hash_mismatch"
    assert list(processing.iterdir()) == []


def _make_pdf(path: Path, include_image: bool = False, rotation: int = 0) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.draw_rect(page.rect, color=(0.2, 0.7, 0.3), fill=(0.2, 0.7, 0.3))
        page.draw_line((20, 120), (280, 120), color=(0.1, 0.1, 0.1), width=1)
        page.insert_textbox((30, 30, 270, 70), SOURCE_TEXT, fontsize=10, fontname="helv")
        if include_image:
            image = Image.new("RGB", (20, 20), "navy")
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            page.insert_image((240, 150, 270, 180), stream=stream.getvalue())
        if rotation:
            page.set_rotation(rotation)
        document.save(path)


def _job(path: Path) -> PdfExportJob:
    return PdfExportJob.model_validate(_job_payload(path))


def _job_payload(path: Path) -> dict:
    analysis = analyze_pdf(path)
    with pymupdf.open(path) as document:
        block = next(item for item in document[0].get_text("blocks") if SOURCE_TEXT in item[4])
    bbox = {"x0": block[0], "y0": block[1], "x1": block[2], "y1": block[3]}
    return {
        "schema_version": 4,
        "source_filename": path.name,
        "document_identity": {
            "sha256": analysis.sha256,
            "page_count": analysis.page_count,
            "document_model_version": analysis.document_model_version,
            "pages": [
                {
                    "page_number": page.page_number,
                    "width": page.dimensions.width,
                    "height": page.dimensions.height,
                    "rotation": page.rotation,
                }
                for page in analysis.pages
            ],
        },
        "segments": [
            {
                "segment_id": "segment-1",
                "page_number": 1,
                "block_id": "block-1",
                "block_type": "paragraph",
                "source_text": SOURCE_TEXT,
                "source_text_origin": "native",
                "source_bbox": bbox,
                "source_page_type": "digital",
                "source_rotation": analysis.pages[0].rotation,
                "source_style": {"font": "Helvetica", "font_size": 10, "color": 0, "flags": 0, "bold": False, "italic": False},
                "edited_translation": TRANSLATED_TEXT,
                "status": "reviewed",
                "translation_origin": "manual",
            }
        ],
    }


def _content_counts(path: Path) -> tuple[int, int]:
    with pymupdf.open(path) as document:
        return len(document[0].get_images(full=True)), len(document[0].get_drawings())


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _work(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path
