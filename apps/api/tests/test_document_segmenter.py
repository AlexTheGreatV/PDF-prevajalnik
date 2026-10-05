from pathlib import Path

import pymupdf as fitz
import pytest

from app.api.pdf import _processing_tempdir, _safe_rmtree
from app.core.config import settings
from app.services.document_segmenter import (
    _cell_provenance,
    _extract_lines,
    normalize_lines,
    normalize_lines_with_boundaries,
    segment_pdf,
)

ZETTEX_FIXTURE = Path("local-fixtures/Zettex MS65 Panel Bond EN - TDS.pdf")


def test_normalization_preserves_units_and_symbols() -> None:
    text = normalize_lines(["CO₂ resistance at 23 °C is 12 %, 4 MPa, RAL 9001."])

    assert "CO₂" in text
    assert "°C" in text
    assert "12 %" in text
    assert "4 MPa" in text
    assert "RAL 9001" in text


def test_normalization_handles_soft_and_real_hyphens() -> None:
    assert normalize_lines(["appli\u00ad", "cations"]) == "applications"
    assert normalize_lines(["appli-", "cations"]) == "appli-cations"
    assert normalize_lines(["one-", "component sealant"]) == "one-component sealant"
    assert "water-based" in normalize_lines(["water-based and shrink-free"])
    assert "shrink-free" in normalize_lines(["water-based and shrink-free"])


def test_visible_line_end_hyphen_is_marked_ambiguous() -> None:
    text, boundaries = normalize_lines_with_boundaries(["moisture-", "curing polymer"])

    assert text == "moisture-curing polymer"
    assert boundaries[0].kind == "visible_hyphen"
    assert boundaries[0].warning == "hyphenation_ambiguous"


def test_segmenter_detects_headings_lists_and_suspicious_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "structure.pdf"
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "APPLICATIONS", fontsize=18, fontname="helv")
    page.insert_text((72, 112), "- Bonds facade panels", fontsize=11)
    page.insert_text((72, 132), "- Keeps one-component products water-based", fontsize=11)
    page.insert_text((72, 172), "qPPLICATIONS", fontsize=18)
    document.save(pdf_path)
    document.close()

    result = segment_pdf(pdf_path)
    blocks = _blocks(result.pages[0])

    assert any(block.kind == "heading" and block.original_text == "APPLICATIONS" for block in blocks)
    assert sum(1 for block in blocks if block.kind == "list_item") == 2
    suspicious = next(block for block in blocks if "qPPLICATIONS" in block.original_text)
    assert suspicious.needs_ocr_check is True
    assert "suspicious_text_low_confidence" in suspicious.warnings


def test_segmenter_detects_two_column_reading_order(tmp_path: Path) -> None:
    pdf_path = tmp_path / "columns.pdf"
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 90), "Left first", fontsize=11)
    page.insert_text((72, 118), "Left second", fontsize=11)
    page.insert_text((340, 90), "Right first", fontsize=11)
    page.insert_text((340, 118), "Right second", fontsize=11)
    document.save(pdf_path)
    document.close()

    result = segment_pdf(pdf_path)
    blocks = _blocks(result.pages[0])
    text_by_order = [block.normalized_text for block in blocks]

    assert text_by_order.index("Left first Left second") < text_by_order.index("Right first Right second")
    assert any(region.kind == "column" for region in result.pages[0].regions)


def test_segmenter_detects_basic_table(tmp_path: Path) -> None:
    pdf_path = tmp_path / "table.pdf"
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    xs = [72, 200, 328]
    ys = [90, 120, 150]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), width=0.8)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), width=0.8)
    page.insert_text((82, 110), "Property", fontsize=10)
    page.insert_text((210, 110), "Value", fontsize=10)
    page.insert_text((82, 140), "Strength", fontsize=10)
    page.insert_text((210, 140), "4 MPa", fontsize=10)
    document.save(pdf_path)
    document.close()

    result = segment_pdf(pdf_path)
    table_blocks = [block for block in _blocks(result.pages[0]) if block.kind == "table"]

    assert table_blocks
    assert any("4 MPa" in cell for row in (table_blocks[0].table_cells or []) for cell in row)
    provenance = table_blocks[0].table_provenance
    assert provenance is not None
    assert provenance.geometry_evidence == "vector_grid"
    assert all(cell.ownership == "owned" for cell in provenance.cells)
    assert all(cell.proven_edges == ["top", "right", "bottom", "left"] for cell in provenance.cells)


def test_table_provenance_keeps_multiple_spans_unique_and_stable(tmp_path: Path) -> None:
    pdf_path = tmp_path / "multiple-spans.pdf"
    document = fitz.open()
    page = document.new_page(width=360, height=220)
    _draw_grid(page, [40, 180, 320], [40, 100, 160])
    page.insert_text((52, 72), "Multi", fontsize=9, fontname="helv")
    page.insert_text((78, 72), "Span", fontsize=9, fontname="hebo")
    page.insert_text((192, 72), "Value", fontsize=9)
    page.insert_text((52, 132), "Second", fontsize=9)
    page.insert_text((192, 132), "4 MPa", fontsize=9)
    document.save(pdf_path)
    document.close()

    first = _first_table(segment_pdf(pdf_path))
    second = _first_table(segment_pdf(pdf_path))
    first_cell = first.table_provenance.cells[0]  # type: ignore[union-attr]
    second_cell = second.table_provenance.cells[0]  # type: ignore[union-attr]

    assert first_cell.ownership == "owned"
    assert len(first_cell.source_span_ids) >= 2
    assert len(set(first_cell.source_span_ids)) == len(first_cell.source_span_ids)
    assert first_cell.source_span_ids == second_cell.source_span_ids
    assert first_cell.cell_id == second_cell.cell_id


def test_table_provenance_preserves_empty_structural_column(tmp_path: Path) -> None:
    pdf_path = tmp_path / "empty-column.pdf"
    document = fitz.open()
    page = document.new_page(width=380, height=220)
    _draw_grid(page, [30, 140, 250, 350], [40, 100, 160])
    page.insert_text((42, 72), "Left", fontsize=9)
    page.insert_text((262, 72), "Right", fontsize=9)
    page.insert_text((42, 132), "A", fontsize=9)
    page.insert_text((262, 132), "B", fontsize=9)
    document.save(pdf_path)
    document.close()

    table = _first_table(segment_pdf(pdf_path))
    provenance = table.table_provenance
    assert provenance is not None
    assert provenance.column_count == 3
    assert (table.table_cells or [])[0][1] == ""
    empty_cells = [cell for cell in provenance.cells if cell.column_index == 1]
    assert len(empty_cells) == 2
    assert all(cell.ownership == "empty" and not cell.source_span_ids for cell in empty_cells)


def test_span_crossing_cell_border_is_not_owned(tmp_path: Path) -> None:
    pdf_path = tmp_path / "crossing.pdf"
    document = fitz.open()
    page = document.new_page(width=320, height=180)
    _draw_grid(page, [20, 160, 300], [30, 90, 150])
    page.insert_text((138, 66), "CROSSING", fontsize=10)
    page.insert_text((42, 126), "Left", fontsize=9)
    page.insert_text((182, 126), "Right", fontsize=9)
    document.save(pdf_path)
    document.close()

    table = _first_table(segment_pdf(pdf_path))
    crossing = next(cell for cell in table.table_provenance.cells if cell.source_span_ids)  # type: ignore[union-attr]
    assert crossing.ownership == "ambiguous"
    assert crossing.block_reason == "span_crosses_cell_boundary"


def test_missing_vector_edge_is_partial_and_blocked(tmp_path: Path) -> None:
    pdf_path = tmp_path / "missing-edge.pdf"
    document = fitz.open()
    page = document.new_page(width=220, height=160)
    page.draw_line((20, 30), (180, 30), color=(0, 0, 0))
    page.draw_line((20, 100), (180, 100), color=(0, 0, 0))
    page.draw_line((20, 30), (20, 100), color=(0, 0, 0))
    page.insert_text((32, 68), "Unclosed cell", fontsize=9)
    document.save(pdf_path)
    document.close()

    with fitz.open(pdf_path) as reopened:
        page = reopened[0]
        provenance = _cell_provenance(
            page, _extract_lines(page, 1), 1, 0, 0, "Unclosed cell",
            fitz.Rect(20, 30, 180, 100), "vector_grid",
        )

    assert provenance.geometry_evidence == "partial_grid"
    assert provenance.proven_edges == ["top", "bottom", "left"]
    assert provenance.ownership == "ambiguous"
    assert provenance.block_reason == "missing_cell_edge"


def test_merged_cell_is_explicit_and_blocked(tmp_path: Path) -> None:
    pdf_path = tmp_path / "merged.pdf"
    document = fitz.open()
    page = document.new_page(width=320, height=180)
    page.draw_rect((20, 30, 300, 150), color=(0, 0, 0))
    page.draw_line((20, 90), (300, 90), color=(0, 0, 0))
    page.draw_line((160, 90), (160, 150), color=(0, 0, 0))
    page.insert_text((32, 66), "Merged heading", fontsize=9)
    page.insert_text((32, 126), "Left", fontsize=9)
    page.insert_text((172, 126), "Right", fontsize=9)
    document.save(pdf_path)
    document.close()

    table = _first_table(segment_pdf(pdf_path))
    merged = next(cell for cell in table.table_provenance.cells if cell.column_span > 1)  # type: ignore[union-attr]
    assert merged.geometry_evidence == "merged_cell"
    assert merged.ownership == "unsupported"
    assert merged.block_reason == "merged_cell"


def test_rotated_table_text_is_explicitly_unsupported(tmp_path: Path) -> None:
    pdf_path = tmp_path / "rotated.pdf"
    document = fitz.open()
    page = document.new_page(width=320, height=220)
    _draw_grid(page, [20, 160, 300], [30, 110, 190])
    page.insert_text((70, 96), "Rotated", fontsize=9, rotate=90)
    page.insert_text((172, 72), "Value", fontsize=9)
    page.insert_text((32, 152), "Normal", fontsize=9)
    page.insert_text((172, 152), "Other", fontsize=9)
    document.save(pdf_path)
    document.close()

    table = _first_table(segment_pdf(pdf_path))
    rotated = [cell for cell in table.table_provenance.cells if cell.rotated_text]  # type: ignore[union-attr]
    assert rotated
    assert all(cell.ownership == "unsupported" and cell.block_reason == "rotated_text" for cell in rotated)


def test_uniform_cell_fill_is_recorded_without_becoming_graphic_overlap(tmp_path: Path) -> None:
    pdf_path = tmp_path / "filled.pdf"
    document = fitz.open()
    page = document.new_page(width=320, height=180)
    page.draw_rect((20, 30, 300, 90), color=None, fill=(0.8, 0.9, 1.0))
    page.draw_rect((20, 90, 300, 150), color=None, fill=(1.0, 1.0, 1.0))
    _draw_grid(page, [20, 160, 300], [30, 90, 150])
    for x, y, text in ((32, 66, "Property"), (172, 66, "Value"), (32, 126, "Type"), (172, 126, "A")):
        page.insert_text((x, y), text, fontsize=9)
    document.save(pdf_path)
    document.close()

    table = _first_table(segment_pdf(pdf_path))
    first = table.table_provenance.cells[0]  # type: ignore[union-attr]
    assert first.background_fill == "#cce5ff"
    assert first.has_graphic_overlap is False
    assert first.ownership == "owned"


def test_tiny_gap_between_gridless_cells_remains_ambiguous(tmp_path: Path) -> None:
    pdf_path = tmp_path / "tiny-gap.pdf"
    document = fitz.open()
    page = document.new_page(width=340, height=260)
    for index in range(5):
        y = 70 + index * 28
        page.insert_text((40, y), f"Long property value {index}", fontsize=9)
        page.insert_text((120, y), f"{index}A", fontsize=7)
    document.save(pdf_path)
    document.close()

    table = _first_table(segment_pdf(pdf_path))
    provenance = table.table_provenance
    assert provenance is not None
    assert provenance.geometry_evidence == "aligned_grid"
    assert any(cell.block_reason == "ambiguous_cell_boundary" for cell in provenance.cells)
    assert not any(cell.ownership == "owned" for cell in provenance.cells)


def test_segmenter_detects_aligned_gridless_cells_without_joining_adjacent_codes(tmp_path: Path) -> None:
    pdf_path = tmp_path / "gridless-colors.pdf"
    document = fitz.open()
    page = document.new_page(width=400, height=300)
    rows = [
        ("Black", "20"),
        ("RAL 9021", "22"),
        ("", "24"),
        ("Beige", "26"),
        ("Grey", "28"),
    ]
    for index, (name, code) in enumerate(rows):
        y = 90 + index * 20
        page.draw_rect(fitz.Rect(45, y - 9, 55, y + 1), fill=(0.3, 0.3, 0.3), color=None)
        if name:
            page.insert_text((70, y), name, fontsize=9)
        page.insert_text((220, y), code, fontsize=7)
    page.insert_text((70, 210), "Following paragraph", fontsize=9)
    document.save(pdf_path)
    document.close()

    result = segment_pdf(pdf_path)
    table = next(block for block in _blocks(result.pages[0]) if block.kind == "table")
    cells = table.table_cells or []

    assert ["Black", "20"] in cells
    assert ["RAL 9021", "22"] in cells
    assert ["", "24"] in cells
    assert ["Beige", "26"] in cells
    assert ["Grey", "28"] in cells
    assert all("Black 20" not in cell and "RAL 9021 22" not in cell for row in cells for cell in row)
    provenance = table.table_provenance
    assert provenance is not None
    ral = provenance.cells[2]
    code = provenance.cells[3]
    assert len(ral.source_span_ids) == 1 and ral.ownership == "owned"
    assert len(code.source_span_ids) == 1 and code.ownership == "owned"
    assert set(ral.source_span_ids).isdisjoint(code.source_span_ids)
    names = [cell for cell in provenance.cells if cell.column_index == 0 and cell.source_span_ids]
    assert all(not cell.has_graphic_overlap for cell in names)
    following = next(block for block in _blocks(result.pages[0]) if block.original_text == "Following paragraph")
    assert following.reading_order == 4


def test_segmenter_does_not_treat_four_parallel_text_rows_as_a_gridless_table(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two-column-copy.pdf"
    document = fitz.open()
    page = document.new_page(width=500, height=300)
    for index in range(4):
        y = 80 + index * 20
        page.insert_text((45, y), f"Left paragraph {index + 1}", fontsize=9)
        page.insert_text((280, y), f"Right paragraph {index + 1}", fontsize=9)
    document.save(pdf_path)
    document.close()

    result = segment_pdf(pdf_path)

    assert all(block.kind != "table" for block in _blocks(result.pages[0]))


def test_image_relevance_separates_text_candidates_decorations_and_export_safety(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    from PIL import Image
    Image.new("RGB", (80, 40), "white").save(image_path)
    pdf_path = tmp_path / "images.pdf"
    document = fitz.open()
    for page_number in range(2):
        page = document.new_page(width=300, height=200)
        page.insert_image(fitz.Rect(0, 0, 100, 40), filename=str(image_path))
        page.insert_image(fitz.Rect(220, 100, 280, 140), filename=str(image_path))
        page.insert_text((30, 90), f"Digital body text on page {page_number + 1} with enough characters.")
    document.save(pdf_path)
    document.close()

    result = segment_pdf(pdf_path)
    roles = [image.ocr_relevance for page in result.pages for image in page.images]
    assert "decorative" in roles
    assert "export_safety_only" in roles
    assert all(not image.needs_ocr_check for page in result.pages for image in page.images)


def test_scanned_page_image_is_a_text_candidate(tmp_path: Path) -> None:
    image_path = tmp_path / "scan.png"
    from PIL import Image
    Image.new("RGB", (300, 200), "white").save(image_path)
    pdf_path = tmp_path / "scan.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_image(page.rect, filename=str(image_path))
    document.save(pdf_path)
    document.close()

    image = segment_pdf(pdf_path).pages[0].images[0]
    assert image.ocr_relevance == "text_candidate"
    assert image.needs_ocr_check is True


def test_processing_tempdir_is_unique_and_cleaned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processing_root = tmp_path / "processing"
    monkeypatch.setattr(settings, "processing_tmp_dir", processing_root)

    with _processing_tempdir() as first:
        first_path = Path(first)
        assert first_path.exists()
        assert processing_root.resolve() in first_path.resolve().parents
    with _processing_tempdir() as second:
        second_path = Path(second)
        assert second_path.exists()

    assert first_path != second_path
    assert not first_path.exists()
    assert not second_path.exists()
    assert list(processing_root.iterdir()) == []


def test_safe_rmtree_refuses_paths_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(RuntimeError):
        _safe_rmtree(outside, root)

    assert outside.exists()


def _draw_grid(page: fitz.Page, xs: list[float], ys: list[float]) -> None:
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), color=(0, 0, 0), width=0.8)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), color=(0, 0, 0), width=0.8)


def _first_table(model):
    return next(block for block in _blocks(model.pages[0]) if block.kind == "table")


@pytest.mark.skipif(not ZETTEX_FIXTURE.exists(), reason="Private Zettex PDF fixture is not present in local-fixtures/.")
def test_zettex_marks_suspicious_applications() -> None:
    page = segment_pdf(ZETTEX_FIXTURE).pages[1]
    block = _find_block(page, "qPPLICATIONS")

    assert block is not None
    assert block.needs_ocr_check is True
    assert "suspicious_text_low_confidence" in block.warnings


@pytest.mark.skipif(not ZETTEX_FIXTURE.exists(), reason="Private Zettex PDF fixture is not present in local-fixtures/.")
def test_zettex_advantages_and_limitations_list_items() -> None:
    page = segment_pdf(ZETTEX_FIXTURE).pages[0]

    advantages = _blocks_between(page, "ADVANTAGES", "LIMITATIONS")
    limitations = _blocks_between(page, "LIMITATIONS", "TEST METHODS")

    assert sum(1 for block in advantages if block.kind == "list_item") == 9
    assert sum(1 for block in limitations if block.kind == "list_item") == 2
    assert any("paint systems" in block.normalized_text for block in advantages)


@pytest.mark.skipif(not ZETTEX_FIXTURE.exists(), reason="Private Zettex PDF fixture is not present in local-fixtures/.")
def test_zettex_technical_table_keeps_property_value_pairs() -> None:
    page = segment_pdf(ZETTEX_FIXTURE).pages[0]
    table = next(block for block in _blocks(page) if block.kind == "table")

    assert ["Density", "1.59 gr/ml"] in (table.table_cells or [])
    assert ["Tensile strength at break", "2.5 MPa"] in (table.table_cells or [])


@pytest.mark.skipif(not ZETTEX_FIXTURE.exists(), reason="Private Zettex PDF fixture is not present in local-fixtures/.")
def test_zettex_sustainability_is_one_paragraph() -> None:
    page = segment_pdf(ZETTEX_FIXTURE).pages[2]
    block = _find_block(page, "Zettex Europe produces sealant")

    assert block is not None
    assert block.kind == "paragraph"
    assert "solvents and isocyanates. We also offer sustainable" in block.normalized_text


@pytest.mark.skipif(not ZETTEX_FIXTURE.exists(), reason="Private Zettex PDF fixture is not present in local-fixtures/.")
def test_zettex_packaging_table_contains_four_values() -> None:
    page = segment_pdf(ZETTEX_FIXTURE).pages[2]
    tables = [block for block in _blocks(page) if block.kind == "table"]
    values = {cell for table in tables for row in (table.table_cells or []) for cell in row if cell}

    assert {"Cartridge 290 ml", "Foil pack 600 ml", "Bucket 20 L", "Drum 200 L"}.issubset(values)


@pytest.mark.skipif(not ZETTEX_FIXTURE.exists(), reason="Private Zettex PDF fixture is not present in local-fixtures/.")
def test_zettex_preserves_original_text_and_ambiguous_hyphen() -> None:
    page = segment_pdf(ZETTEX_FIXTURE).pages[0]
    block = _find_block(page, "moisture-curing")

    assert block is not None
    assert "moisture-\ncuring" in block.original_text
    assert "moisture-curing" in block.normalized_text
    assert any(boundary.warning == "hyphenation_ambiguous" for boundary in block.line_boundaries)


def _blocks(page):
    return [block for region in page.regions for block in region.blocks]


def _find_block(page, text: str):
    return next((block for block in _blocks(page) if text in block.original_text or text in block.normalized_text), None)


def _blocks_between(page, start_text: str, end_text: str):
    blocks = _blocks(page)
    start = next(index for index, block in enumerate(blocks) if start_text in block.normalized_text)
    end = next(index for index, block in enumerate(blocks[start + 1 :], start + 1) if end_text in block.normalized_text)
    return blocks[start + 1 : end]
