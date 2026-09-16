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
from app.pdf_export_models import PdfExportJob, PdfTextStyle
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_exporter import PdfExportValidationError, export_translated_pdf, plan_pdf_export
from app.services.pdf_typography import PdfTypographyResolver

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


def test_source_mismatch_precedes_image_overlap_analysis(tmp_path: Path) -> None:
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
    assert plan.replacements[0].skip_reason == "source_text_mismatch"


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
        ({"source_page_type": "scanned"}, "non_digital_source"),
        ({"block_type": "table_cell", "source_bbox": None}, "ambiguous_cell_membership"),
    ],
)
def test_ambiguous_sources_are_skipped(tmp_path: Path, changes: dict, reason: str) -> None:
    source = tmp_path / f"{reason}.pdf"
    _make_pdf(source)
    payload = _job_payload(source)
    payload["segments"][0].update(changes)
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, reason))
    assert plan.replacements[0].skip_reason == reason


def test_accepted_ocr_without_provenance_is_separately_skipped(tmp_path: Path) -> None:
    source = tmp_path / "ocr-without-provenance.pdf"
    _make_pdf(source)
    payload = _job_payload(source)
    payload["segments"][0]["source_text_origin"] = "accepted_ocr"
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", _work(tmp_path, "ocr-provenance"))
    assert plan.replacements == []
    assert plan.ocr_replacements[0].skip_reason == "ocr_provenance_mismatch"


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


@pytest.mark.parametrize(
    ("fontname", "weight", "style"),
    [("helv", "regular", "normal"), ("hebo", "bold", "normal"), ("heit", "regular", "italic"), ("hebi", "bold", "italic")],
)
def test_typography_resolver_maps_all_four_font_faces(tmp_path: Path, fontname: str, weight: str, style: str) -> None:
    source = tmp_path / f"{fontname}.pdf"
    _make_pdf(source, fontname=fontname, text_rect=(30, 30, 270, 100))
    plan = plan_pdf_export(source, _job(source), "diagnostic", _work(tmp_path, fontname))
    replacement = plan.replacements[0]
    assert replacement.font_family == "FiraGO"
    assert replacement.font_weight == weight
    assert replacement.font_style == style


def test_typography_preserves_color_and_extractable_unicode(tmp_path: Path) -> None:
    source = tmp_path / "color.pdf"
    _make_pdf(source, fontname="hebo", color=(0x12 / 255, 0x4A / 255, 0x7A / 255), text_rect=(30, 30, 270, 100))
    artifact = export_translated_pdf(source, _job(source), "replacement_preview", _work(tmp_path, "color"))
    replacement = artifact.plan.replacements[0]
    assert replacement.color == "#124a7a"
    assert replacement.font_weight == "bold"
    with pymupdf.open(artifact.path) as document:
        span = document[0].get_text("dict", clip=pymupdf.Rect(*replacement.fitted_bbox.model_dump().values()))["blocks"][0]["lines"][0]["spans"][0]
        assert span["color"] == 0x124A7A
        assert TRANSLATED_TEXT in document[0].get_text()


def test_firago_keeps_numbers_and_units_together_with_nonbreaking_spaces(tmp_path: Path) -> None:
    typography = PdfTypographyResolver(tmp_path)
    asset = typography.assets[(False, False)]
    text = "Temperaturna odpornost −40\u00a0°C do +90\u00a0°C, sušenje 10 do 20\u00a0minut pri 5\u00a0MPa in 290\u00a0ml."
    style = PdfTextStyle(font_size=10, line_height=1.2)
    html_text, css = typography.html_and_css(text, style, asset)

    assert "−40\u00a0°C" in html_text
    assert "+90\u00a0°C" in html_text
    assert typography.supports(asset, text)
    with pymupdf.open() as document:
        page = document.new_page(width=160, height=160)
        spare, _ = page.insert_htmlbox(
            pymupdf.Rect(20, 20, 140, 140), html_text, css=css, archive=typography.archive,
        )
        assert spare >= 0
        lines = [
            "".join(span["text"] for span in line["spans"])
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
        ]
        normalized = [line.replace("\u00a0", " ") for line in lines]
        assert any("−40 °C" in line for line in normalized)
        assert any("+90 °C" in line for line in normalized)
        assert any("5 MPa" in line for line in normalized)
        assert any("290 ml" in line for line in normalized)
        assert any("20 minut" in line for line in normalized)
        assert not any(line.rstrip().endswith(("−40", "+90", "20", "5", "290")) for line in normalized)


@pytest.mark.parametrize(("align", "expected"), [(0, "left"), (1, "center"), (2, "right")])
def test_typography_detects_alignment(tmp_path: Path, align: int, expected: str) -> None:
    source = tmp_path / f"align-{align}.pdf"
    text_rect = (30, 30, 270, 70)
    _make_pdf(source, align=align, source_text="Aligned", text_rect=text_rect)
    payload = _job_payload(source, source_text="Aligned", translated_text="Poravnano")
    payload["segments"][0]["source_bbox"] = {"x0": 30, "y0": 30, "x1": 270, "y1": 70}
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, f"align-{align}"))
    assert plan.replacements[0].alignment == expected


def test_preflight_reports_exact_expanded_and_scaled_strategies(tmp_path: Path) -> None:
    exact_source = tmp_path / "exact.pdf"
    _make_pdf(exact_source, source_text="Short", text_rect=(30, 30, 270, 100))
    exact_payload = _job_payload(exact_source, source_text="Short", translated_text="Kratek")
    exact_payload["segments"][0]["source_bbox"] = {"x0": 30, "y0": 30, "x1": 270, "y1": 100}
    exact = plan_pdf_export(exact_source, PdfExportJob.model_validate(exact_payload), "diagnostic", _work(tmp_path, "exact"))
    assert exact.replacements[0].fit_strategy == "exact"
    assert exact.replacements[0].scale == 1

    expanded_source = tmp_path / "expanded.pdf"
    _make_pdf(expanded_source, source_text="Short", text_rect=(30, 30, 90, 55))
    expanded = plan_pdf_export(
        expanded_source,
        PdfExportJob.model_validate(_job_payload(expanded_source, source_text="Short", translated_text="Prevedeni daljši naslov")),
        "diagnostic",
        _work(tmp_path, "expanded"),
    )
    assert expanded.replacements[0].fit_strategy == "expanded_bbox"
    assert expanded.replacements[0].scale == 1

    scaled_source = tmp_path / "scaled.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_textbox((30, 30, 80, 55), "Short", fontsize=10, fontname="helv")
        page.insert_textbox((125, 30, 180, 55), "Neighbor", fontsize=10, fontname="helv")
        page.insert_textbox((30, 65, 180, 85), "Below", fontsize=10, fontname="helv")
        document.save(scaled_source)
    scaled = plan_pdf_export(
        scaled_source,
        PdfExportJob.model_validate(_job_payload(scaled_source, source_text="Short", translated_text="Prevedeni nekoliko daljši naslov")),
        "diagnostic",
        _work(tmp_path, "scaled"),
    )
    assert scaled.replacements[0].fit_strategy == "scaled"
    assert 0.75 <= scaled.replacements[0].scale < 1
    heading_payload = _job_payload(scaled_source, source_text="Short", translated_text="Prevedeni nekoliko daljši naslov")
    heading_payload["segments"][0]["block_type"] = "heading"
    heading = plan_pdf_export(
        scaled_source,
        PdfExportJob.model_validate(heading_payload),
        "diagnostic",
        _work(tmp_path, "scaled-heading"),
    )
    assert heading.replacements[0].skip_reason == "overflow"


def test_multiline_style_and_safe_expansion_do_not_cross_neighbor(tmp_path: Path) -> None:
    source = tmp_path / "neighbor.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_textbox((30, 30, 110, 70), "Short source", fontsize=10, fontname="helv")
        page.insert_textbox((150, 30, 270, 70), "Neighbor", fontsize=10, fontname="helv")
        page.insert_textbox((30, 90, 140, 140), "Line one\nLine two", fontsize=10, fontname="helv")
        document.save(source)
    payload = _job_payload(source, source_text="Short source", translated_text="Daljši prevedeni naslov")
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, "neighbor"))
    replacement = plan.replacements[0]
    assert replacement.fitted_bbox is None or replacement.fitted_bbox.x1 < 150

    multiline = _job_payload(source, source_text="Line one\nLine two", translated_text="Prva vrstica\nDruga vrstica")
    multiline_plan = plan_pdf_export(source, PdfExportJob.model_validate(multiline), "diagnostic", _work(tmp_path, "multiline"))
    assert multiline_plan.replacements[0].original_line_count == 2


def test_missing_glyph_is_explicit_and_original_remains(tmp_path: Path) -> None:
    source = tmp_path / "glyph.pdf"
    _make_pdf(source)
    payload = _job_payload(source, translated_text="Besedilo z znakom \U0001fae0")
    job = PdfExportJob.model_validate(payload)
    artifact = export_translated_pdf(source, job, "replacement_preview", _work(tmp_path, "glyph"))
    assert artifact.plan.replacements[0].skip_reason == "missing_glyph"
    assert artifact.plan.replacements[0].fit_strategy == "missing_glyph"
    with pymupdf.open(artifact.path) as document:
        assert SOURCE_TEXT in document[0].get_text()


def test_mixed_style_is_reported_without_changing_text(tmp_path: Path) -> None:
    source = tmp_path / "mixed.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((30, 50), "Mixed ", fontsize=10, fontname="helv")
        page.insert_text((60, 50), "style", fontsize=10, fontname="hebo")
        document.save(source)
    with pymupdf.open(source) as document:
        extracted = document[0].get_text().strip()
    payload = _job_payload(source, source_text=extracted, translated_text="Mešani slog")
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", _work(tmp_path, "mixed"))
    assert "mixed_source_style" in plan.replacements[0].typography_warnings
    assert plan.replacements[0].source_text == extracted


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


def _make_pdf(
    path: Path,
    include_image: bool = False,
    rotation: int = 0,
    fontname: str = "helv",
    color: tuple[float, float, float] = (0, 0, 0),
    align: int = 0,
    source_text: str = SOURCE_TEXT,
    text_rect: tuple[float, float, float, float] = (30, 30, 270, 70),
) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.draw_rect(page.rect, color=(0.2, 0.7, 0.3), fill=(0.2, 0.7, 0.3))
        page.draw_line((20, 120), (280, 120), color=(0.1, 0.1, 0.1), width=1)
        page.insert_textbox(text_rect, source_text, fontsize=10, fontname=fontname, color=color, align=align)
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


def _job_payload(path: Path, source_text: str = SOURCE_TEXT, translated_text: str = TRANSLATED_TEXT) -> dict:
    analysis = analyze_pdf(path)
    with pymupdf.open(path) as document:
        matches = document[0].search_for(source_text.replace("\n", " "))
        if not matches:
            matches = [match for line in source_text.splitlines() for match in document[0].search_for(line)]
        block = pymupdf.Rect(matches[0])
        for match in matches[1:]:
            block |= match
    bbox = {"x0": block.x0, "y0": block.y0, "x1": block.x1, "y1": block.y1}
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
                "source_text": source_text,
                "source_text_origin": "native",
                "source_bbox": bbox,
                "source_page_type": "digital",
                "source_rotation": analysis.pages[0].rotation,
                "source_style": {"font": "Helvetica", "font_size": 10, "color": 0, "flags": 0, "bold": False, "italic": False},
                "edited_translation": translated_text,
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
