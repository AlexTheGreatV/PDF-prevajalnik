from pathlib import Path
import shutil
import subprocess

import pymupdf as fitz
import pytest

from app.models import BBox, OcrResult
from app.services.ocr import (
    MockOcrProvider,
    OcrTimeoutError,
    TesseractCliOcrProvider,
    map_pixel_bbox_to_pdf_bbox,
    ocr_status,
    plan_ocr_candidates,
    run_selective_ocr,
)
from app.services.document_segmenter import segment_pdf

ZETTEX_FIXTURE = Path("local-fixtures/Zettex MS65 Panel Bond EN - TDS.pdf")
TESSERACT_AVAILABLE = ocr_status().available


def test_plans_full_page_ocr_for_scanned_page(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scanned.pdf"
    image_path = tmp_path / "scan.png"
    _make_image_pdf(pdf_path, image_path)

    candidates = plan_ocr_candidates(segment_pdf(pdf_path))

    assert any(candidate.source == "full_page" and "scanned_page" in candidate.reasons for candidate in candidates)


def test_digital_pdf_without_anomalies_does_not_need_ocr(tmp_path: Path) -> None:
    pdf_path = tmp_path / "digital.pdf"
    _make_text_pdf(pdf_path, ["APPLICATIONS", "Normal selectable text with one-component sealant."])

    candidates = plan_ocr_candidates(segment_pdf(pdf_path))

    assert candidates == []


def test_plans_suspicious_heading_ocr(tmp_path: Path) -> None:
    pdf_path = tmp_path / "suspicious.pdf"
    _make_text_pdf(pdf_path, ["qPPLICATIONS", "Normal selectable paragraph with enough native text for classification."])

    candidates = plan_ocr_candidates(segment_pdf(pdf_path))

    assert any(candidate.source == "suspicious_block" and "suspicious_glyph" in candidate.reasons for candidate in candidates)


def test_plans_image_text_candidate_near_storage_heading(tmp_path: Path) -> None:
    pdf_path = tmp_path / "image-text.pdf"
    image_path = tmp_path / "storage.png"
    _make_text_image_candidate_pdf(pdf_path, image_path)

    candidates = plan_ocr_candidates(segment_pdf(pdf_path))

    assert any(candidate.source == "image_region" and candidate.linked_heading == "Storage" for candidate in candidates)


def test_missing_tesseract_returns_controlled_unavailable_status() -> None:
    status = TesseractCliOcrProvider(executable="definitely-missing-tesseract").status()

    assert status.available is False
    assert "tesseract" in (status.message or "")


def test_tesseract_timeout_is_controlled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = TesseractCliOcrProvider(timeout_seconds=1)
    monkeypatch.setattr(shutil, "which", lambda _: "tesseract")

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(subprocess, "run", raise_timeout)

    with pytest.raises(OcrTimeoutError):
        provider.recognize(
            tmp_path / "missing.png",
            bbox=BBox(x0=0, y0=0, x1=100, y1=50),
            language="eng",
            source="suspicious_block",
            dpi=180,
            page_bbox=BBox(x0=0, y0=0, x1=100, y1=100),
        )


def test_mock_ocr_low_confidence_requires_review(tmp_path: Path) -> None:
    pdf_path = tmp_path / "suspicious.pdf"
    _make_text_pdf(pdf_path, ["qPPLICATIONS", "Normal selectable paragraph with enough native text for classification."])
    provider = MockOcrProvider(
        {"suspicious_block": _ocr_result("APPLICATIONS", 70, BBox(x0=0, y0=0, x1=100, y1=40))}
    )

    response = run_selective_ocr(pdf_path, filename="suspicious.pdf", temp_root=tmp_path / "ocr", provider=provider)

    candidate = response.candidates[0]
    assert candidate.ocr_text == "APPLICATIONS"
    assert candidate.requires_review is True
    assert candidate.ocr_applied is False


def test_mock_ocr_high_confidence_can_apply_local_suspicious_fix(tmp_path: Path) -> None:
    pdf_path = tmp_path / "suspicious.pdf"
    _make_text_pdf(pdf_path, ["qPPLICATIONS", "Normal selectable paragraph with enough native text for classification."])
    provider = MockOcrProvider(
        {"suspicious_block": _ocr_result("APPLICATIONS", 96, BBox(x0=0, y0=0, x1=100, y1=40))}
    )

    response = run_selective_ocr(pdf_path, filename="suspicious.pdf", temp_root=tmp_path / "ocr", provider=provider)

    candidate = response.candidates[0]
    assert candidate.ocr_applied is True
    assert candidate.selected_text_source == "ocr"
    assert candidate.native_text == "qPPLICATIONS"


def test_pixel_bbox_maps_back_to_pdf_points() -> None:
    mapped = map_pixel_bbox_to_pdf_bbox(
        (18, 36, 90, 72),
        BBox(x0=10, y0=20, x1=110, y1=120),
        2,
        BBox(x0=0, y0=0, x1=200, y1=200),
    )

    assert mapped == BBox(x0=19, y0=38, x1=55, y1=56)


def test_selective_ocr_cleans_temp_files(tmp_path: Path) -> None:
    pdf_path = tmp_path / "suspicious.pdf"
    temp_root = tmp_path / "ocr-root"
    _make_text_pdf(pdf_path, ["qPPLICATIONS", "Normal text"])
    provider = MockOcrProvider(
        {"suspicious_block": _ocr_result("APPLICATIONS", 96, BBox(x0=0, y0=0, x1=100, y1=40))}
    )

    run_selective_ocr(pdf_path, filename="suspicious.pdf", temp_root=temp_root, provider=provider)

    assert list(temp_root.iterdir()) == []


@pytest.mark.skipif(not ZETTEX_FIXTURE.exists(), reason="Private Zettex PDF fixture is not present in local-fixtures/.")
def test_zettex_ambiguous_hyphen_does_not_trigger_ocr() -> None:
    candidates = plan_ocr_candidates(segment_pdf(ZETTEX_FIXTURE))
    reasons = {reason for candidate in candidates for reason in candidate.reasons}

    assert "ambiguous_hyphen" not in reasons


@pytest.mark.skipif(
    not ZETTEX_FIXTURE.exists() or not TESSERACT_AVAILABLE,
    reason="Private Zettex PDF fixture and local Tesseract are required for real OCR regression.",
)
def test_zettex_real_ocr_regression() -> None:
    response = run_selective_ocr(
        ZETTEX_FIXTURE,
        filename=ZETTEX_FIXTURE.name,
        temp_root=Path("tmp") / "zettex-ocr-test",
    )
    text = "\n".join(candidate.ocr_text or "" for candidate in response.candidates)

    assert "APPLICATIONS" in text
    assert "+5" in text and "+25" in text
    assert "18 months" in text


def _make_text_pdf(path: Path, lines: list[str]) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    y = 90
    for line in lines:
        is_heading = line.upper() == line or line.startswith("qPPLICATIONS")
        page.insert_text((72, y), line, fontsize=16 if is_heading else 11)
        y += 28
    document.save(path)
    document.close()


def _make_image_pdf(path: Path, image_path: Path) -> None:
    _make_png(image_path)
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=str(image_path))
    document.save(path)
    document.close()


def _make_text_image_candidate_pdf(path: Path, image_path: Path) -> None:
    _make_png(image_path)
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 90), "Storage", fontsize=18)
    page.insert_image(fitz.Rect(72, 110, 210, 150), filename=str(image_path))
    page.insert_text((72, 190), "Digital body text with enough selectable characters for image OCR planning.")
    document.save(path)
    document.close()


def _make_png(path: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 25), "+5 C to +25 C", fill="black")
    image.save(path)


def _ocr_result(text: str, confidence: float, bbox: BBox) -> OcrResult:
    return OcrResult(
        text=text,
        words=[],
        confidence=confidence,
        bbox=bbox,
        language="eng",
        provider="mock",
        processing_time_ms=1,
        source="suspicious_block",
    )
