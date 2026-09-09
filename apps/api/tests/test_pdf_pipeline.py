from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.core.config import settings
from app.main import app
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_renderer import render_pdf_pages


client = TestClient(app)


def test_analyzes_digital_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "digital.pdf"
    _make_digital_pdf(pdf_path)

    result = analyze_pdf(pdf_path)

    assert result.page_count == 1
    assert result.kind == "digital"
    assert result.extracted_text_chars > 25
    assert result.pages[0].dimensions.width > 0
    assert result.pages[0].text_blocks
    assert result.pages[0].text_blocks[0].x0 >= 0


def test_analyzes_scanned_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(pdf_path, tmp_path / "scan.png")

    result = analyze_pdf(pdf_path)

    assert result.page_count == 1
    assert result.kind == "scanned"
    assert result.extracted_text_chars == 0
    assert result.pages[0].image_count == 1
    assert result.pages[0].text_blocks == []


def test_analyzes_mixed_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    scan_path = tmp_path / "scan.png"
    _make_mixed_pdf(pdf_path, scan_path)

    result = analyze_pdf(pdf_path)

    assert result.page_count == 2
    assert result.kind == "mixed"
    assert [page.kind for page in result.pages] == ["digital", "scanned"]


def test_analyzes_empty_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "empty.pdf"
    _make_empty_pdf(pdf_path)

    result = analyze_pdf(pdf_path)

    assert result.page_count == 1
    assert result.kind == "empty"
    assert result.extracted_text_chars == 0
    assert result.pages[0].text_blocks == []


def test_renders_pdf_pages_with_poppler(tmp_path: Path) -> None:
    pdf_path = tmp_path / "digital.pdf"
    output_root = tmp_path / "renders"
    _make_digital_pdf(pdf_path)

    result = render_pdf_pages(pdf_path, output_root=output_root, dpi=72)

    assert result.dpi == 72
    assert len(result.pages) == 1
    assert Path(result.pages[0].path).exists()
    assert result.pages[0].width_px is not None
    assert result.pages[0].height_px is not None


def test_api_rejects_non_pdf_upload() -> None:
    response = client.post(
        "/api/pdf/analyze",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_api_rejects_fake_pdf_upload() -> None:
    response = client.post(
        "/api/pdf/analyze",
        files={"file": ("fake.pdf", b"not a real pdf", "application/pdf")},
    )

    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_api_rejects_oversized_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "digital.pdf"
    _make_digital_pdf(pdf_path)
    monkeypatch.setattr(settings, "max_upload_bytes", 8)

    response = client.post(
        "/api/pdf/analyze",
        files={"file": ("digital.pdf", pdf_path.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 413


def test_api_renders_data_urls_and_cleans_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_path = tmp_path / "digital.pdf"
    processing_tmp = tmp_path / "processing"
    _make_digital_pdf(pdf_path)
    monkeypatch.setattr(settings, "processing_tmp_dir", processing_tmp)

    response = client.post(
        "/api/pdf/render",
        data={"dpi": "72"},
        files={"file": ("digital.pdf", pdf_path.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["pages"][0]["path"].startswith("data:image/png;base64,")
    assert list(processing_tmp.iterdir()) == []


def _make_digital_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text(
        (72, 72),
        "JUB KOOP internal PDF pipeline test document with selectable text.",
        fontsize=12,
    )
    document.save(path)
    document.close()


@pytest.mark.parametrize("dpi", [71, 401, "invalid"])
def test_render_rejects_invalid_dpi(dpi) -> None:
    response = client.post("/api/pdf/render", data={"dpi": dpi}, files={"file": ("a.pdf", b"%PDF-", "application/pdf")})
    assert response.status_code == 422


def test_render_selected_page_and_default_compatibility(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    with fitz.open() as document:
        document.new_page(width=144, height=72)
        document.new_page(width=72, height=144)
        document.save(pdf_path)
    files = {"file": ("two-pages.pdf", pdf_path.read_bytes(), "application/pdf")}
    default = client.post("/api/pdf/render", files=files)
    assert default.status_code == 200
    assert default.json()["dpi"] == 150
    assert [page["page_number"] for page in default.json()["pages"]] == [1, 2]
    selected = client.post("/api/pdf/render", files=files, data={"dpi": 300, "page_number": 2})
    assert selected.status_code == 200
    assert selected.json()["dpi"] == 300
    assert len(selected.json()["pages"]) == 1
    assert selected.json()["pages"][0]["page_number"] == 2
    assert selected.json()["pages"][0]["width_px"] == 300
    assert selected.json()["pages"][0]["height_px"] == 600
    assert client.post("/api/pdf/render", files=files, data={"page_number": 3}).status_code == 400
    assert client.post("/api/pdf/render", files=files, data={"page_number": 0}).status_code == 422


def _make_scanned_pdf(path: Path, image_path: Path) -> None:
    _make_scan_image(image_path)
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=str(image_path))
    document.save(path)
    document.close()


def _make_mixed_pdf(path: Path, image_path: Path) -> None:
    _make_scan_image(image_path)
    document = fitz.open()
    page_1 = document.new_page(width=595, height=842)
    page_1.insert_text(
        (72, 72),
        "Digital page with enough selectable text for pipeline classification.",
        fontsize=12,
    )
    page_2 = document.new_page(width=595, height=842)
    page_2.insert_image(page_2.rect, filename=str(image_path))
    document.save(path)
    document.close()


def _make_empty_pdf(path: Path) -> None:
    document = fitz.open()
    document.new_page(width=595, height=842)
    document.save(path)
    document.close()


def _make_scan_image(path: Path) -> None:
    image = Image.new("RGB", (800, 1100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 80, 740, 1020), outline="black", width=4)
    draw.line((90, 160, 710, 160), fill="black", width=3)
    draw.line((90, 230, 650, 230), fill="black", width=3)
    image.save(path)
