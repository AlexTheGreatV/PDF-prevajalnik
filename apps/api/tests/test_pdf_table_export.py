import copy
import hashlib
import io
import socket
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageChops, ImageDraw

from app.pdf_export_models import PdfExportJob
from app.services.pdf_exporter import (
    _balanced_table_layouts,
    _protect_table_layout_text,
    _table_minimum_scale,
    _verified_table_source_font_size,
    export_translated_pdf,
    plan_pdf_export,
)
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_table_export import PdfTableAnalyzer, is_technical_value
from app.services.pdf_typography import PdfTypographyResolver


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
    assert artifact.plan.table_summary.exported_cells == 2
    assert artifact.plan.resolution_summary.preserved_identical == 1
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


def test_unchanged_technical_value_is_preserved_but_changed_value_is_exported(tmp_path: Path) -> None:
    source = tmp_path / "technical.pdf"
    _make_table(source, values=("Density", "2.5 MPa", "Temperature 20 °C", "Stable"))
    job = _job(source, translations={(0, 0): "Gostota", (0, 1): "2,5\u00a0MPa", (1, 0): "Temperatura 20 °C"})
    plan = plan_pdf_export(source, job, "replacement_preview", tmp_path / "work")
    cells = {(cell.row_index, cell.column_index): cell for cell in plan.tables[0].cells}
    assert cells[(0, 1)].eligible is True
    assert cells[(0, 1)].resolution == "replacement_planned"
    assert cells[(0, 0)].eligible is True
    assert cells[(1, 0)].eligible is True

    unchanged = plan_pdf_export(
        source,
        _job(source, translations={(0, 1): "2.5 MPa"}),
        "replacement_preview",
        tmp_path / "unchanged",
    ).tables[0].cells[1]
    assert unchanged.eligible is False
    assert unchanged.skip_reason is None
    assert unchanged.resolution == "preserved_identical"


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
    assert cells[(1, 0)].eligible is False
    assert cells[(1, 0)].resolution == "preserved_identical"
    assert artifact.plan.tables[0].partial_export is True
    with pymupdf.open(artifact.path) as document:
        assert "Values" in document[0].get_text()


def test_source_proven_small_table_font_is_retained_but_never_reduced(tmp_path: Path) -> None:
    source = tmp_path / "small-source-font.pdf"
    _make_table(source, font_size=5.0, row_height=24)

    retained = plan_pdf_export(
        source, _job(source, translations={(0, 0): "Lastnost"}), "replacement_preview", tmp_path / "retained",
    ).tables[0].cells[0]
    assert retained.eligible is True
    assert retained.source_font_size == pytest.approx(5.0)
    assert retained.fitted_font_size == pytest.approx(5.0)
    assert _table_minimum_scale(retained) == pytest.approx(1.0)
    assert retained.source_font_size * 0.98 == pytest.approx(4.9)

    blocked = plan_pdf_export(
        source,
        _job(source, translations={(0, 0): "X" * 80}),
        "replacement_preview",
        tmp_path / "blocked",
    ).tables[0].cells[0]
    assert blocked.eligible is False
    assert blocked.skip_reason == "cell_overflow"


def test_table_font_above_minimum_never_scales_below_5_5_points(tmp_path: Path) -> None:
    source = tmp_path / "six-point-source.pdf"
    _make_table(source, font_size=6.0, row_height=24)
    cell = plan_pdf_export(
        source,
        _job(source, translations={(0, 0): "Daljši varen prevod lastnosti"}),
        "replacement_preview",
        tmp_path / "work",
    ).tables[0].cells[0]
    assert not cell.eligible or cell.fitted_font_size >= 5.5


def test_table_minimum_scale_uses_server_verified_source_size(tmp_path: Path) -> None:
    source = tmp_path / "verified-source-size.pdf"
    _make_table(source, font_size=8.0)
    cell = plan_pdf_export(
        source, _job(source, translations={(0, 0): "Lastnost"}), "replacement_preview", tmp_path / "work",
    ).tables[0].cells[0]
    cell.requested_font_size = 10.0

    assert cell.source_font_size == pytest.approx(8.0)
    assert _table_minimum_scale(cell) == pytest.approx(0.6)


@pytest.mark.parametrize("size", [-1.0, 0.0, float("nan"), float("inf"), 72.1])
def test_invalid_server_observed_source_font_size_blocks_provenance(tmp_path: Path, size: float) -> None:
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    resolver = PdfTypographyResolver(font_dir)

    class FakePage:
        def get_text(self, *_args, **_kwargs):
            return {"blocks": [{"lines": [{"spans": [{"text": "Source", "size": size}]}]}]}

    assert resolver.source_font_sizes(FakePage(), pymupdf.Rect(0, 0, 100, 20)) == []


def test_materially_mixed_source_font_sizes_are_not_collapsed_to_one_size() -> None:
    assert _verified_table_source_font_size([9.0, 9.03]) == pytest.approx(9.03)
    assert _verified_table_source_font_size([5.0, 9.0]) is None
    assert _verified_table_source_font_size([float("inf")]) is None


def test_materially_mixed_server_source_sizes_block_the_table_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed-source-fonts.pdf"
    _make_table(source)
    monkeypatch.setattr(PdfTypographyResolver, "source_font_sizes", lambda *_args: [5.0, 9.0])

    cell = plan_pdf_export(
        source, _job(source, translations={(0, 0): "Lastnost"}), "replacement_preview", tmp_path / "work",
    ).tables[0].cells[0]

    assert cell.eligible is False
    assert cell.skip_reason == "missing_table_provenance"
    assert cell.source_font_sizes == [5.0, 9.0]
    assert cell.source_font_size is None


def test_table_wrap_uses_two_lines_and_preserves_unbreakable_technical_tokens(tmp_path: Path) -> None:
    source = tmp_path / "safe-wrap.pdf"
    _make_table(source, row_height=34)
    translation = "Temperatura premaza RAL 9001 pri uporabi nad 20 °C"
    artifact = export_translated_pdf(
        source, _job(source, translations={(0, 0): translation}), "replacement_preview", tmp_path / "work",
    )
    cell = artifact.plan.tables[0].cells[0]
    assert cell.eligible is True
    assert cell.fitted_line_count is not None and cell.fitted_line_count >= 2
    assert "RAL\u00a09001" in cell.layout_text
    assert "20\u00a0°C" in cell.layout_text
    assert "RAL\n9001" not in cell.layout_text
    assert "20\n°C" not in cell.layout_text
    with pymupdf.open(artifact.path) as document:
        extracted = " ".join(document[0].get_text().split())
        assert translation in extracted


@pytest.mark.parametrize(
    "value",
    ["5 mm", "10 %", "RAL 9001", "SIST EN 1234", "-40 °C do +90 °C", "ACME MS65 Panel Bond"],
)
def test_table_layout_protects_technical_units_codes_ranges_and_product_names(value: str) -> None:
    protected = _protect_table_layout_text(value)
    assert " " not in protected
    assert "\u00a0" in protected


def test_table_layout_does_not_split_an_unspaced_technical_code() -> None:
    technical_code = "ABC-1234-X"

    assert _protect_table_layout_text(technical_code) == technical_code


@pytest.mark.parametrize(
    "token",
    ["https://example.test/a-b", "user@example.test", "__PHX_12__", "ABC-1234-X"],
)
def test_table_layout_never_splits_urls_email_placeholders_or_codes(token: str) -> None:
    layouts = _balanced_table_layouts(f"Predpona {token} pripona z dodatnim besedilom")

    assert all(layout.count(token) == 1 for layout in layouts)
    assert all(layout.replace("\n", " ") == layouts[0] for layout in layouts)


def test_client_source_style_cannot_forge_source_font_size(tmp_path: Path) -> None:
    source = tmp_path / "forged-source-style.pdf"
    _make_table(source, font_size=9.0)
    payload = _job_payload(source)
    payload["segments"][0]["source_style"] = {"font_size": 4.0}
    payload["segments"][0].update(status="reviewed", edited_translation="Lastnost")

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", tmp_path / "work")

    cell = plan.tables[0].cells[0]
    assert cell.source_font_size == pytest.approx(9.0)
    assert cell.requested_font_size == pytest.approx(9.0)


def test_table_preflight_and_writer_share_layout_without_clipping_diacritics(tmp_path: Path) -> None:
    source = tmp_path / "diacritics.pdf"
    _make_table(source, row_height=34)
    before_drawings = _drawing_signature(source)
    translation = "Čvrsta zaščita ščiti žleb"

    artifact = export_translated_pdf(
        source, _job(source, translations={(0, 0): translation}), "replacement_preview", tmp_path / "work",
    )

    cell = artifact.plan.tables[0].cells[0]
    assert cell.eligible is True
    assert cell.line_height >= 1.0
    assert cell.fitted_bbox.x0 >= cell.cell_bbox.x0
    assert cell.fitted_bbox.x1 <= cell.cell_bbox.x1
    assert cell.fitted_bbox.y0 >= cell.cell_bbox.y0
    assert cell.fitted_bbox.y1 <= cell.cell_bbox.y1
    assert _drawing_signature(artifact.path) == before_drawings
    with pymupdf.open(artifact.path) as document:
        assert translation in " ".join(document[0].get_text().split())


def test_empty_and_none_cells_remain_in_grid_but_not_in_replacement_plan(tmp_path: Path) -> None:
    source = tmp_path / "merged.pdf"
    _make_table(source, merged_header=True)
    before_hash = _hash(source)
    detected = _detected(source)
    assert detected.cells[0][0].column_span == 2
    payload = _job_payload(source)
    empty = next(segment for segment in payload["segments"] if segment["segment_id"].endswith("-c1-2"))
    empty.update(source_text="\u00a0\u200b", status="reviewed", edited_translation="ne sme biti izvoženo")
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    assert _hash(source) == before_hash
    assert plan.tables[0].row_count == 2
    assert plan.tables[0].column_count == 2
    assert not any(cell.row_index == 0 and cell.column_index == 1 for cell in plan.tables[0].cells)
    assert not any(item.segment_id == empty["segment_id"] for item in plan.replacements)


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
    duplicate_cells = [cell for cell in plan.replacements if cell.segment_id in {first["segment_id"], "duplicate-c1-1"}]
    assert len(duplicate_cells) == 2
    assert all(cell.skip_reason == "duplicate_table_content" for cell in duplicate_cells)


def test_wrong_source_text_is_rejected_per_cell(tmp_path: Path) -> None:
    source = tmp_path / "mismatch.pdf"
    _make_table(source)
    payload = _job_payload(source)
    payload["segments"][0]["source_text"] = "Wrong"
    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "diagnostic", tmp_path / "work")
    assert plan.tables[0].cells[0].skip_reason == "source_text_mismatch"


def test_legacy_table_cell_without_provenance_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "legacy.pdf"
    _make_table(source)
    payload = _job_payload(source)
    payload["schema_version"] = 8
    payload["segments"][0]["table_cell_provenance"] = None

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", tmp_path / "work")

    replacements = [item for item in plan.replacements if item.segment_id == payload["segments"][0]["segment_id"]]
    assert any(item.skip_reason == "missing_table_provenance" for item in replacements)
    assert any(item.resolution == "blocked_table_structure" for item in replacements)


def test_changed_client_table_provenance_is_rejected_as_stale(tmp_path: Path) -> None:
    source = tmp_path / "stale.pdf"
    _make_table(source)
    payload = _job_payload(source)
    payload["segments"][0]["table_cell_provenance"]["content_bbox"]["x1"] -= 1

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", tmp_path / "work")

    replacement = next(item for item in plan.replacements if item.segment_id == payload["segments"][0]["segment_id"])
    assert replacement.skip_reason == "stale_table_provenance"
    assert replacement.resolution == "blocked_table_structure"


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("table_id", lambda value: "foreign-table"),
        ("cell_id", lambda value: f"{value}-stale"),
        ("row_index", lambda value: value + 1),
        ("column_index", lambda value: value + 1),
        ("row_span", lambda value: value + 1),
        ("column_span", lambda value: value + 1),
        ("table_bbox", lambda value: {**value, "x1": value["x1"] + 1}),
        ("cell_bbox", lambda value: {**value, "x0": value["x0"] - 1}),
        ("source_bbox", lambda value: {**value, "y1": value["y1"] + 1}),
        ("content_bbox", lambda value: {**value, "x1": value["x1"] - 1}),
        ("source_span_ids", lambda value: value[:-1]),
        ("source_span_ids", lambda value: [*value, "foreign-span"]),
        ("proven_edges", lambda value: value[:-1]),
        ("ownership", lambda value: "ambiguous"),
        ("geometry_evidence", lambda value: "merged_cell"),
        ("rotated_text", lambda value: not value),
    ],
)
def test_each_client_provenance_mutation_is_rejected(
    tmp_path: Path,
    field: str,
    mutate,
) -> None:
    source = tmp_path / f"stale-{field}.pdf"
    _make_table(source)
    payload = _job_payload(source)
    provenance = payload["segments"][0]["table_cell_provenance"]
    provenance[field] = mutate(copy.deepcopy(provenance[field]))

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", tmp_path / "work")

    replacement = next(item for item in plan.replacements if item.segment_id == payload["segments"][0]["segment_id"])
    assert replacement.skip_reason == "stale_table_provenance"
    assert replacement.resolution == "blocked_table_structure"


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), -1_000_000.0, 1_000_000.0])
def test_nonfinite_or_out_of_page_client_geometry_fails_closed(tmp_path: Path, coordinate: float) -> None:
    source = tmp_path / "unsafe-geometry.pdf"
    _make_table(source)
    payload = _job_payload(source)
    payload["segments"][0]["table_cell_provenance"]["source_bbox"]["x0"] = coordinate

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", tmp_path / "work")

    replacement = next(item for item in plan.replacements if item.segment_id == payload["segments"][0]["segment_id"])
    assert replacement.skip_reason == "stale_table_provenance"
    assert replacement.resolution == "blocked_table_structure"


def test_reusing_one_cells_span_in_another_cell_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "duplicate-span.pdf"
    _make_table(source)
    payload = _job_payload(source)
    first, second = payload["segments"][:2]
    second["table_cell_provenance"]["source_span_ids"] = list(first["table_cell_provenance"]["source_span_ids"])

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", tmp_path / "work")

    first_result = next(item for item in plan.replacements if item.segment_id == first["segment_id"])
    second_result = next(item for item in plan.replacements if item.segment_id == second["segment_id"])
    assert first_result.skip_reason == "not_reviewed"
    assert second_result.skip_reason == "stale_table_provenance"


def test_provenance_from_another_pdf_cannot_be_exported(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    other = tmp_path / "other.pdf"
    _make_table(source)
    _make_table(other, values=("Other", "Values", "Material", "MS Polymer"))
    payload = _job_payload(source)
    payload["segments"][0]["table_cell_provenance"] = _job_payload(other)["segments"][0]["table_cell_provenance"]

    plan = plan_pdf_export(source, PdfExportJob.model_validate(payload), "replacement_preview", tmp_path / "work")

    replacement = next(item for item in plan.replacements if item.segment_id == payload["segments"][0]["segment_id"])
    assert replacement.skip_reason in {"source_text_mismatch", "stale_table_provenance"}
    assert replacement.resolution == "blocked_table_structure"


def test_future_export_schema_is_rejected() -> None:
    with pytest.raises(ValueError):
        PdfExportJob.model_validate({"schema_version": 10})


def test_graphic_table_with_internal_image_rows_is_unsupported(tmp_path: Path) -> None:
    source = tmp_path / "graphic.pdf"
    _make_graphic_table(source)
    detected = _detected(source)
    assert detected.reliable is False
    assert "graphic_table_with_images" in detected.warnings


@pytest.mark.parametrize("background", ["simple", "complex"])
def test_image_backed_cell_is_blocked_by_provenance(tmp_path: Path, background: str) -> None:
    source = tmp_path / f"image-{background}.pdf"
    _make_table(source, image_background=background)
    plan = plan_pdf_export(source, _job(source, translations={(0, 0): "Lastnost"}), "diagnostic", tmp_path / "work")
    cell = plan.tables[0].cells[0]
    assert cell.skip_reason == "graphic_cell"
    assert cell.provenance_block_reason == "graphic_overlap"


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


def test_gridless_color_table_exports_safe_text_without_changing_swatch_or_code(tmp_path: Path) -> None:
    source = tmp_path / "gridless-colors.pdf"
    _make_gridless_color_table(source)
    before_hash = _hash(source)
    before_drawings = _drawing_signature(source)
    before_swatch = _render_clip(source, pymupdf.Rect(40, 70, 60, 95))

    artifact = export_translated_pdf(
        source,
        _job(source, translations={(0, 0): "Črna"}),
        "replacement_preview",
        tmp_path / "work",
    )

    assert _hash(source) == before_hash
    assert _drawing_signature(artifact.path) == before_drawings
    assert ImageChops.difference(before_swatch, _render_clip(artifact.path, pymupdf.Rect(40, 70, 60, 95))).getbbox() is None
    with pymupdf.open(artifact.path) as document:
        text = document[0].get_text()
        assert "Črna" in text
        assert "Black" not in text
        assert "20" in text and "RAL 9021" in text and "22" in text


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
    font_size: float = 9,
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
            page.insert_textbox(text_rect, text, fontsize=font_size, fontname="hebo" if index < 2 else "helv", color=color, align=align)
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


def _make_gridless_color_table(path: Path) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=400, height=260)
        for index, (name, code) in enumerate((("Black", "20"), ("RAL 9021", "22"), ("Beige", "26"), ("Grey", "28"), ("White", "30"))):
            y = 88 + index * 24
            page.draw_rect((45, y - 11, 55, y - 1), fill=(0.3, 0.3, 0.3), color=None)
            page.insert_text((70, y), name, fontsize=9)
            page.insert_text((220, y), code, fontsize=7)
        document.save(path)


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
    analysis = analyze_pdf(path)
    table_blocks = [
        block
        for page in analysis.document.pages
        for region in page.regions
        for block in region.blocks
        if block.kind == "table" and block.table_provenance is not None
    ]
    block = table_blocks[table_index]
    table = block.table_provenance
    provenance_by_position = {(cell.row_index, cell.column_index): cell for cell in table.cells}
    page = analysis.pages[0]
    segments = []
    for row_index, row in enumerate(block.table_cells or []):
        for column_index, text in enumerate(row):
            provenance = provenance_by_position[(row_index, column_index)]
            segments.append({
                "segment_id": f"{block_id}-c{row_index + 1}-{column_index + 1}",
                "page_number": 1, "block_id": block_id, "block_type": "table_cell",
                "source_text": text, "source_text_origin": "native", "source_bbox": provenance.source_bbox.model_dump() if provenance.source_bbox else None,
                "source_page_type": "digital", "source_rotation": page.rotation, "source_style": None,
                "table_cell_provenance": {
                    **provenance.model_dump(),
                    "table_id": table.table_id,
                    "table_bbox": table.bbox.model_dump(),
                    "table_geometry_evidence": table.geometry_evidence,
                    "table_warnings": table.warnings,
                },
                "edited_translation": "", "status": "skipped" if not text else "pending",
                "translation_origin": None,
            })
    return {
        "schema_version": 9, "source_filename": path.name,
        "document_identity": {"sha256": analysis.sha256, "page_count": 1, "document_model_version": analysis.document_model_version,
                              "pages": [{"page_number": 1, "width": page.dimensions.width, "height": page.dimensions.height, "rotation": page.rotation}]},
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
