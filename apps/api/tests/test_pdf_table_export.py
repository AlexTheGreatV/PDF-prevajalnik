import hashlib
import io
import socket
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageChops, ImageDraw

from app.pdf_export_models import PdfExportJob
from app.services.pdf_exporter import export_translated_pdf, plan_pdf_export
from app.services.pdf_table_export import PdfTableAnalyzer, is_technical_value


def test_regular_table_exports_cells_and_preserves_grid_and_fill(tmp_path: Path) -> None:
    source = tmp_path / "table.pdf"
    _make_table(source)
    before_hash = _hash(source)
    before_drawings = _drawing_signature(source)
    before_neighbor = _render_clip(source, pymupdf.Rect(161, 81, 299, 129))
    job = _job(source, translations={(0, 0): "Lastnost", (0, 1): "Vrednost", (1, 0): "Material"})

    artifact = export_translated_pdf(source, job, "replacement_preview", tmp_path / "work")

    assert _hash(source) == before_hash
    assert artifact.plan.table_summary.detected_tables == 1
    assert artifact.plan.table_summary.exported_cells == 3
    assert _drawing_signature(artifact.path) == before_drawings
    assert ImageChops.difference(before_neighbor, _render_clip(artifact.path, pymupdf.Rect(161, 81, 299, 129))).getbbox() is None
    with pymupdf.open(artifact.path) as document:
        text = document[0].get_text()
        assert "Lastnost" in text and "Vrednost" in text and "Material" in text
        assert "Properties" not in text and "Values" not in text
        assert "MS Polymer" in text


def test_header_typography_alignment_color_and_background_are_retained(tmp_path: Path) -> None:
    source = tmp_path / "styles.pdf"
    _make_table(source, alignments=(0, 1, 2), text_colors=((0.1, 0.2, 0.6), (0.2, 0.5, 0.1)))
    plan = plan_pdf_export(source, _job(source, translations={(0, 0): "Lastnost", (0, 1): "Vrednost"}), "diagnostic", tmp_path / "work")
    cells = plan.tables[0].cells
    assert cells[0].font_weight == "bold"
    assert cells[0].color == "#1a3399"
    assert cells[0].alignment == "left"
    assert cells[1].alignment == "center"
    assert cells[0].background_fill != cells[2].background_fill


@pytest.mark.parametrize(
    "value",
    [
        "2.5 MPa",
        "150 %",
        "20 °C",
        "RAL 9001",
        "290 ml",
        "20 L",
        "1.59 gr/ml",
        "10 min (20 °C /50 % Rh.)",
        "-40 °C to +90 °C",
    ],
)
def test_standalone_technical_values_are_classified(value: str) -> None:
    assert is_technical_value(value)


@pytest.mark.parametrize("value", ["Temperature 20 °C", "Version 2", "MS Polymer", "20 \ufffdC"])
def test_translatable_or_damaged_values_are_not_classified_as_standalone_technical(value: str) -> None:
    assert not is_technical_value(value)


def test_technical_value_is_skipped_but_mixed_text_is_exportable(tmp_path: Path) -> None:
    source = tmp_path / "technical.pdf"
    _make_table(source, values=("Density", "2.5 MPa", "Temperature 20 °C", "Stable"))
    job = _job(source, translations={(0, 0): "Gostota", (0, 1): "2.5 MPa", (1, 0): "Temperatura 20 °C"})
    plan = plan_pdf_export(source, job, "replacement_preview", tmp_path / "work")
    reasons = {(cell.row_index, cell.column_index): cell.skip_reason for cell in plan.tables[0].cells}
    assert reasons[(0, 1)] == "technical_value"
    assert reasons[(0, 0)] is None
    assert reasons[(1, 0)] is None


def test_multiline_scale_and_cell_overflow_are_isolated(tmp_path: Path) -> None:
    source = tmp_path / "overflow.pdf"
    _make_table(source, row_height=30)
    job = _job(source, translations={
        (0, 0): "Večvrstični prevod z zmerno dolžino",
        (0, 1): "besedilo " * 80,
        (1, 0): "Material",
    })
    artifact = export_translated_pdf(source, job, "replacement_preview", tmp_path / "work")
    cells = {(cell.row_index, cell.column_index): cell for cell in artifact.plan.tables[0].cells}
    assert cells[(0, 0)].fit_strategy in {"exact", "scaled"}
    assert cells[(0, 1)].skip_reason == "cell_overflow"
    assert cells[(1, 0)].eligible is True
    assert artifact.plan.tables[0].partial_export is True
    with pymupdf.open(artifact.path) as document:
        assert "Values" in document[0].get_text()


def test_empty_and_none_cells_remain_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "merged.pdf"
    _make_table(source, merged_header=True)
    detected = _detected(source)
    assert detected.cells[0][0].column_span == 2
    payload = _job_payload(source)
    empty = next(segment for segment in payload["segments"] if segment["segment_id"].endswith("-c1-2"))
    empty.update(status="reviewed", edited_translation="ne sme biti izvoženo")
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    cell = next(cell for cell in plan.tables[0].cells if cell.row_index == 0 and cell.column_index == 1)
    assert cell.skip_reason == "empty_cell"


def test_duplicate_container_and_cell_content_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.pdf"
    _make_table(source)
    payload = _job_payload(source)
    payload["segments"].append({
        **payload["segments"][0],
        "segment_id": "table-container",
        "block_type": "table",
        "source_bbox": {"x0": 20, "y0": 30, "x1": 300, "y1": 130},
    })
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    assert all(cell.skip_reason == "duplicate_table_content" for cell in plan.tables[0].cells)


def test_overlapping_table_replacements_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "overlap.pdf"
    _make_table(source, merged_header=True)
    payload = _job_payload(source)
    first = payload["segments"][0]
    payload["segments"].append({**first, "segment_id": "duplicate-c1-1"})
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    assert all(cell.skip_reason == "duplicate_table_content" for cell in plan.tables[-1].cells)


def test_wrong_source_text_is_rejected_per_cell(tmp_path: Path) -> None:
    source = tmp_path / "mismatch.pdf"
    _make_table(source)
    payload = _job_payload(source)
    payload["segments"][0]["source_text"] = "Wrong"
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    assert plan.tables[0].cells[0].skip_reason == "source_text_mismatch"


def test_graphic_table_with_internal_image_rows_is_unsupported(tmp_path: Path) -> None:
    source = tmp_path / "graphic.pdf"
    _make_graphic_table(source)
    detected = _detected(source)
    assert detected.reliable is False
    assert "graphic_table_with_images" in detected.warnings


@pytest.mark.parametrize("background", ["simple", "complex"])
def test_image_backed_cell_uses_existing_overlap_safety(tmp_path: Path, background: str) -> None:
    source = tmp_path / f"image-{background}.pdf"
    _make_table(source, image_background=background)
    plan = plan_pdf_export(source, _job(source, translations={(0, 0): "Lastnost"}), "diagnostic", tmp_path / "work")
    cell = plan.tables[0].cells[0]
    if background == "simple":
        assert cell.image_overlap_kind == "simple_background"
    else:
        assert cell.skip_reason == "image_overlap"


def test_opaque_cell_fill_above_image_allows_export_without_modifying_image(tmp_path: Path) -> None:
    source = tmp_path / "covered-image.pdf"
    _make_table(source, image_background="simple", opaque_vector_cover=True)
    before_images = _image_hashes(source)

    artifact = export_translated_pdf(
        source,
        _job(source, translations={(0, 0): "Lastnost"}),
        "replacement_preview",
        tmp_path / "work",
    )

    cell = artifact.plan.tables[0].cells[0]
    assert cell.eligible is True
    assert "overlapping_image_hidden_by_opaque_vector_fill" in cell.image_overlap_warnings
    assert _image_hashes(artifact.path) == before_images


def test_vector_line_above_opaque_cell_fill_still_blocks_export(tmp_path: Path) -> None:
    source = tmp_path / "covered-image-with-line.pdf"
    _make_table(source, image_background="simple", opaque_vector_cover=True, foreground_line=True)

    plan = plan_pdf_export(source, _job(source, translations={(0, 0): "Lastnost"}), "diagnostic", tmp_path / "work")

    assert plan.tables[0].cells[0].skip_reason == "image_overlap"


def test_two_tables_near_page_edges_are_matched_independently(tmp_path: Path) -> None:
    source = tmp_path / "two.pdf"
    _make_two_tables(source)
    payload = _job_payload(source, table_index=0)
    payload["segments"].extend(_job_payload(source, table_index=1, block_id="p1-r2-b2")["segments"])
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    assert plan.table_summary.detected_tables == 2
    assert len([table for table in plan.tables if table.cells]) == 2


def test_table_export_does_not_use_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "offline.pdf"
    _make_table(source)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    artifact = export_translated_pdf(source, _job(source, translations={(0, 0): "Lastnost"}), "replacement_preview", tmp_path / "work")
    assert artifact.path.exists()


def _make_table(
    path: Path,
    *,
    values: tuple[str, str, str, str] = ("Properties", "Values", "Material", "MS Polymer"),
    row_height: float = 50,
    merged_header: bool = False,
    alignments: tuple[int, int, int] = (0, 1, 2),
    text_colors: tuple[tuple[float, float, float], tuple[float, float, float]] = ((0, 0, 0), (0, 0, 0)),
    image_background: str | None = None,
    opaque_vector_cover: bool = False,
    foreground_line: bool = False,
    borders: bool = True,
) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=320, height=180)
        if image_background:
            image = Image.new("RGB", (560, 200), "white")
            if image_background == "complex":
                draw = ImageDraw.Draw(image)
                for x in range(image.width):
                    draw.line((x, 0, x, image.height), fill=(x % 255, 80, 180))
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            page.insert_image((20, 30, 300, 30 + row_height * 2), stream=stream.getvalue())
        fills = ((0.88, 0.91, 0.95), (1, 1, 1))
        for row in range(2):
            y0, y1 = 30 + row * row_height, 30 + (row + 1) * row_height
            if not image_background or opaque_vector_cover:
                page.draw_rect((20, y0, 300, y1), color=None, fill=fills[row])
        if borders:
            page.draw_rect((20, 30, 300, 30 + row_height * 2), color=(0.2, 0.2, 0.2), width=0.8)
            page.draw_line((20, 30 + row_height), (300, 30 + row_height), color=(0.2, 0.2, 0.2), width=0.8)
            if not merged_header:
                page.draw_line((160, 30), (160, 30 + row_height), color=(0.2, 0.2, 0.2), width=0.8)
            page.draw_line((160, 30 + row_height), (160, 30 + row_height * 2), color=(0.2, 0.2, 0.2), width=0.8)
        if foreground_line:
            page.draw_line((24, 42), (130, 42), color=(0.8, 0.1, 0.1), width=1.2)
        cells = [(20, 30, 160 if not merged_header else 300, 30 + row_height), (160, 30, 300, 30 + row_height),
                 (20, 30 + row_height, 160, 30 + row_height * 2), (160, 30 + row_height, 300, 30 + row_height * 2)]
        for index, (rect, text) in enumerate(zip(cells, values)):
            if merged_header and index == 1:
                continue
            align = alignments[min(index, len(alignments) - 1)]
            color = text_colors[0] if index < 2 else text_colors[1]
            text_rect = (rect[0] + 4, rect[1] + 3, rect[2] - 4, rect[3] - 3)
            page.insert_textbox(text_rect, text, fontsize=9, fontname="hebo" if index < 2 else "helv", color=color, align=align)
        document.save(path)


def _make_graphic_table(path: Path) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=320, height=220)
        for y in (30, 55, 110, 135, 190):
            page.draw_line((20, y), (300, y), color=(0, 0, 0))
        for x in (20, 160, 300):
            page.draw_line((x, 30), (x, 190), color=(0, 0, 0))
        page.insert_text((30, 47), "Cartridge 290 ml", fontsize=9)
        page.insert_text((170, 47), "Bucket 20 L", fontsize=9)
        page.insert_text((30, 127), "Foil pack 600 ml", fontsize=9)
        page.insert_text((170, 127), "Drum 200 L", fontsize=9)
        image = Image.new("RGB", (80, 40), "gray")
        stream = io.BytesIO(); image.save(stream, format="PNG")
        page.insert_image((30, 60, 140, 100), stream=stream.getvalue())
        page.insert_image((30, 140, 140, 180), stream=stream.getvalue())
        document.save(path)


def _make_two_tables(path: Path) -> None:
    _make_table(path)
    with pymupdf.open(path) as document:
        page = document[0]
        page.draw_rect((2, 140, 318, 178), color=(0, 0, 0))
        page.draw_line((160, 140), (160, 178), color=(0, 0, 0))
        page.insert_textbox((2, 140, 160, 178), "Second A", fontsize=8)
        page.insert_textbox((160, 140, 318, 178), "Second B", fontsize=8)
        document.save(path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)


def _detected(path: Path, table_index: int = 0):
    with pymupdf.open(path) as document:
        return PdfTableAnalyzer().detect(document[0], 1)[table_index]


def _job(path: Path, translations: dict[tuple[int, int], str]) -> PdfExportJob:
    payload = _job_payload(path)
    for segment in payload["segments"]:
        position = tuple(int(value) - 1 for value in segment["segment_id"].rsplit("-c", 1)[1].split("-"))
        if position in translations:
            segment.update(status="reviewed", edited_translation=translations[position], translation_origin="manual")
    return PdfExportJob.model_validate(payload)


def _job_payload(path: Path, table_index: int = 0, block_id: str = "p1-r1-b1") -> dict:
    with pymupdf.open(path) as document:
        page = document[0]
        table = PdfTableAnalyzer().detect(page, 1)[table_index]
        sha = _hash(path)
        page_width, page_height, rotation = page.rect.width, page.rect.height, page.rotation
    segments = []
    for row in table.cells:
        for cell in row:
            segments.append({
                "segment_id": f"{block_id}-c{cell.row_index + 1}-{cell.column_index + 1}",
                "page_number": 1, "block_id": block_id, "block_type": "table_cell",
                "source_text": cell.text, "source_text_origin": "native", "source_bbox": None,
                "source_page_type": "digital", "source_rotation": rotation, "source_style": None,
                "edited_translation": "", "status": "skipped" if not cell.text else "pending",
                "translation_origin": None,
            })
    return {
        "schema_version": 4, "source_filename": path.name,
        "document_identity": {"sha256": sha, "page_count": 1, "document_model_version": 1,
                              "pages": [{"page_number": 1, "width": page_width, "height": page_height, "rotation": rotation}]},
        "segments": segments,
    }


def _drawing_signature(path: Path):
    with pymupdf.open(path) as document:
        return [(tuple(round(v, 2) for v in drawing["rect"]), drawing.get("fill"), drawing.get("color")) for drawing in document[0].get_drawings()]


def _image_hashes(path: Path) -> list[str]:
    with pymupdf.open(path) as document:
        return [
            hashlib.sha256(document.extract_image(xref)["image"]).hexdigest()
            for page in document
            for xref, *_ in page.get_images(full=True)
        ]


def _render_clip(path: Path, clip: pymupdf.Rect) -> Image.Image:
    with pymupdf.open(path) as document:
        pixmap = document[0].get_pixmap(clip=clip, dpi=200, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
