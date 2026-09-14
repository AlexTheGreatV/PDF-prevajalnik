import hashlib
import io
import random
import socket
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

from app.pdf_export_models import PdfExportJob
from app.services.pdf_exporter import export_translated_pdf, plan_pdf_export
from app.services.pdf_image_overlap import DEFAULT_THRESHOLDS, PdfImageOverlapAnalyzer


SOURCE = "SOURCE DUPLICATE LONG"
TRANSLATION = "PREVOD"
TEXT_POINT = (30, 50)
PAGE_SIZE = (300, 200)


@pytest.mark.parametrize("background", ["white", "solid", "jpeg"])
def test_uniform_image_backgrounds_are_patchable(tmp_path: Path, background: str) -> None:
    source = tmp_path / f"{background}.pdf"
    _make_layered_pdf(source, background)
    assessment = _assessment(source)
    assert assessment.kind == "simple_background"
    assert assessment.safe_for_background_patch is True
    assert assessment.metrics is not None
    assert assessment.metrics.dpi == 300
    assert assessment.metrics.near_threshold is False
    assert assessment.patch_rects


@pytest.mark.parametrize("background", ["gradient", "photo", "texture", "line", "icon"])
def test_complex_image_backgrounds_are_not_patchable(tmp_path: Path, background: str) -> None:
    source = tmp_path / f"{background}.pdf"
    _make_layered_pdf(source, background)
    assessment = _assessment(source)
    assert assessment.safe_for_background_patch is False
    assert assessment.kind in {"complex_background", "uncertain"}


def test_image_only_text_is_classified_without_export(tmp_path: Path) -> None:
    source = tmp_path / "image-only.pdf"
    _make_layered_pdf(source, "white", native_text=False)
    with pymupdf.open(source) as document:
        assessment = PdfImageOverlapAnalyzer().analyze(document[0], pymupdf.Rect(25, 35, 180, 55), native_text=False)
    assert assessment.kind == "image_only_text"
    assert assessment.safe_for_background_patch is False


def test_neighboring_native_text_blocks_safe_expansion(tmp_path: Path) -> None:
    source = tmp_path / "neighbor.pdf"
    _make_layered_pdf(source, "white", neighbor=True)
    payload = _job_payload(source, translated_text="PREVOD Z VEČ BESEDAMI, KI POTREBUJE PROSTOR")
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    replacement = plan.replacements[0]
    assert replacement.fitted_bbox is None or replacement.fitted_bbox.x1 < 185


def test_expanded_bbox_that_leaves_safe_background_is_skipped(tmp_path: Path) -> None:
    source = tmp_path / "split.pdf"
    _make_layered_pdf(source, "split")
    payload = _job_payload(source, translated_text="ZELO DOLG PREVOD, KI ZA PRILEGANJE POTREBUJE ŠIRŠE OBMOČJE")
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    replacement = plan.replacements[0]
    assert replacement.background_patch_used is False
    assert replacement.skip_reason == "image_overlap"
    assert "expanded_background_not_safe" in replacement.image_overlap_warnings


def test_metrics_near_threshold_are_uncertain(tmp_path: Path) -> None:
    source = tmp_path / "near.pdf"
    _make_layered_pdf(source, "jpeg")
    initial = _assessment(source)
    assert initial.metrics and initial.metrics.color_variance > 0
    thresholds = replace(
        DEFAULT_THRESHOLDS,
        maximum_color_variance=initial.metrics.color_variance / 0.9,
        maximum_edge_density=1,
        maximum_texture_score=1,
        maximum_side_color_delta=255,
    )
    with pymupdf.open(source) as document:
        rect = document[0].search_for(SOURCE)[0]
        assessment = PdfImageOverlapAnalyzer(thresholds).analyze(document[0], rect)
    assert assessment.kind == "uncertain"
    assert assessment.metrics and assessment.metrics.near_threshold is True


def test_missing_render_is_uncertain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "render.pdf"
    _make_layered_pdf(source, "white")
    with pymupdf.open(source) as document:
        page = document[0]
        rect = page.search_for(SOURCE)[0]
        monkeypatch.setattr(page, "get_pixmap", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("render")))
        assessment = PdfImageOverlapAnalyzer().analyze(page, rect)
    assert assessment.kind == "uncertain"
    assert "background_render_failed" in assessment.warnings


def test_safe_patch_removes_image_and_native_copy_but_preserves_objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "safe-export.pdf"
    _make_layered_pdf(source, "solid", vector=True)
    source_hash = _hash(source)
    image_hashes = _image_hashes(source)
    original_crop = _render_crop(source, pymupdf.Rect(90, 38, 155, 55))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))

    artifact = export_translated_pdf(source, PdfExportJob.model_validate(_job_payload(source)), "replacement_preview", tmp_path / "work")
    replacement = artifact.plan.replacements[0]

    assert _hash(source) == source_hash
    assert replacement.background_patch_used is True
    assert replacement.image_overlap_kind == "simple_background"
    assert artifact.plan.summary.background_patches == 1
    assert _image_hashes(artifact.path) == image_hashes
    with pymupdf.open(artifact.path) as document:
        text = document[0].get_text()
        assert SOURCE not in text
        assert TRANSLATION in text
        assert len(document[0].get_drawings()) >= 1
    output_crop = _render_crop(artifact.path, pymupdf.Rect(90, 38, 155, 55))
    assert _dark_pixels(output_crop) < _dark_pixels(original_crop) * 0.2


def test_complex_background_skip_keeps_both_source_layers(tmp_path: Path) -> None:
    source = tmp_path / "complex-export.pdf"
    _make_layered_pdf(source, "gradient")
    before = _render_crop(source, pymupdf.Rect(25, 35, 165, 56))
    artifact = export_translated_pdf(
        source, PdfExportJob.model_validate(_job_payload(source)), "replacement_preview", tmp_path / "work"
    )
    replacement = artifact.plan.replacements[0]
    assert replacement.skip_reason == "image_overlap"
    assert replacement.background_patch_used is False
    with pymupdf.open(artifact.path) as document:
        assert SOURCE in document[0].get_text()
    after = _render_crop(artifact.path, pymupdf.Rect(25, 35, 165, 56))
    assert _pixel_difference(before, after) < 0.01


def test_list_marker_is_not_in_patch_or_redaction(tmp_path: Path) -> None:
    source = tmp_path / "list.pdf"
    _make_layered_pdf(source, "white", text="- List item")
    payload = _job_payload(source, source_text="- List item", translated_text="Prevedena postavka", block_type="list_item")
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    replacement = plan.replacements[0]
    with pymupdf.open(source) as document:
        marker = document[0].search_for("-")[0]
    assert replacement.patch_rects
    assert all(rect.x0 > marker.x1 for rect in replacement.patch_rects)
    assert all(rect.x0 > marker.x1 for rect in replacement.redaction_rects)


def _assessment(path: Path):
    with pymupdf.open(path) as document:
        rect = document[0].search_for(SOURCE)[0]
        return PdfImageOverlapAnalyzer().analyze(document[0], rect)


def _make_layered_pdf(
    path: Path,
    background: str,
    *,
    native_text: bool = True,
    neighbor: bool = False,
    vector: bool = False,
    text: str = SOURCE,
) -> None:
    base = _background(background)
    with pymupdf.open() as flattened:
        page = flattened.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
        stream = io.BytesIO()
        base.save(stream, format="PNG")
        page.insert_image(page.rect, stream=stream.getvalue())
        page.insert_text(TEXT_POINT, text, fontsize=10, fontname="helv", color=(0, 0, 0))
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        image_stream = pixmap.tobytes("jpeg", jpg_quality=82) if background == "jpeg" else pixmap.tobytes("png")
    with pymupdf.open() as document:
        page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
        page.insert_image(page.rect, stream=image_stream)
        if native_text:
            page.insert_text(TEXT_POINT, text, fontsize=10, fontname="helv", color=(0, 0, 0))
        if neighbor:
            page.insert_text((185, 50), "NEIGHBOR", fontsize=10, fontname="helv")
        if vector:
            page.draw_line((20, 120), (280, 120), color=(1, 0, 0), width=2)
        document.save(path)


def _background(kind: str) -> Image.Image:
    image = Image.new("RGB", (600, 400), (255, 255, 255) if kind != "solid" else (224, 238, 244))
    draw = ImageDraw.Draw(image)
    if kind == "gradient":
        for x in range(image.width):
            value = round(255 - 100 * x / image.width)
            draw.line((x, 0, x, image.height), fill=(value, 220, 240))
    elif kind == "split":
        for x in range(350, image.width):
            value = round(255 - 120 * (x - 350) / (image.width - 350))
            draw.line((x, 0, x, image.height), fill=(value, 210, 235))
    elif kind == "photo":
        for y in range(0, image.height, 20):
            for x in range(0, image.width, 20):
                draw.rectangle((x, y, x + 19, y + 19), fill=((x * 7) % 256, (y * 11) % 256, ((x + y) * 5) % 256))
    elif kind == "texture":
        rng = random.Random(7)
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                value = rng.randint(185, 255)
                pixels[x, y] = (value, value, value)
    elif kind == "line":
        draw.line((40, 100, 360, 100), fill=(0, 0, 0), width=8)
    elif kind == "icon":
        draw.ellipse((44, 72, 86, 114), outline=(0, 0, 0), width=8)
    return image


def _job_payload(path: Path, *, source_text: str = SOURCE, translated_text: str = TRANSLATION, block_type: str = "heading") -> dict:
    with pymupdf.open(path) as document:
        matches = document[0].search_for(source_text)
        rect = pymupdf.Rect(matches[0])
        for match in matches[1:]:
            rect |= match
    return {
        "schema_version": 4,
        "source_filename": path.name,
        "document_identity": {
            "sha256": _hash(path),
            "page_count": 1,
            "document_model_version": 1,
            "pages": [{"page_number": 1, "width": 300, "height": 200, "rotation": 0}],
        },
        "segments": [{
            "segment_id": "segment-1",
            "page_number": 1,
            "block_id": "block-1",
            "block_type": block_type,
            "source_text": source_text,
            "source_text_origin": "native",
            "source_bbox": {"x0": rect.x0, "y0": rect.y0, "x1": rect.x1, "y1": rect.y1},
            "source_page_type": "digital",
            "source_rotation": 0,
            "source_style": {"font": "Helvetica", "font_size": 10, "color": 0, "flags": 0, "bold": False, "italic": False},
            "edited_translation": translated_text,
            "status": "reviewed",
            "translation_origin": "manual",
        }],
    }


def _render_crop(path: Path, rect: pymupdf.Rect) -> Image.Image:
    with pymupdf.open(path) as document:
        pixmap = document[0].get_pixmap(clip=rect, matrix=pymupdf.Matrix(2, 2), alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _dark_pixels(image: Image.Image) -> int:
    return sum(max(pixel) < 170 for pixel in image.getdata())


def _pixel_difference(left: Image.Image, right: Image.Image) -> float:
    pairs = zip(left.getdata(), right.getdata())
    differences = [max(abs(a - b) for a, b in zip(first, second)) for first, second in pairs]
    return sum(value > 8 for value in differences) / max(1, len(differences))


def _image_hashes(path: Path) -> list[str]:
    with pymupdf.open(path) as document:
        return sorted(hashlib.sha256(document.extract_image(item[0])["image"]).hexdigest() for item in document[0].get_images(full=True))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
