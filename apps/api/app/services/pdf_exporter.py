from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf

from app.models import BBox
from app.pdf_export_models import (
    ExportMode,
    FitStrategy,
    PdfExportJob,
    PdfExportPlan,
    PdfExportResolutionSummary,
    PdfExportSegment,
    PdfExportSummary,
    PdfOcrExportSummary,
    PdfOcrReplacement,
    PdfReplacement,
    PdfTableCellReplacement,
    PdfTableExportPlan,
    PdfTableExportSummary,
    SkipReason,
)
from app.services.pdf_image_overlap import ImageOverlapAssessment, PdfImageOverlapAnalyzer
from app.services.pdf_table_export import DetectedTable, PdfTableAnalyzer, canonical_text, is_technical_value, parse_cell_position
from app.services.pdf_typography import FontAsset, PdfTypographyResolver
from app.services.ocr_review_registry import ocr_review_registry

LOGGER = logging.getLogger(__name__)

DOCUMENT_MODEL_VERSION = 1
MIN_FONT_SIZE = 6.0
GEOMETRY_TOLERANCE = 0.5
OVERLAP_TOLERANCE = 0.1
HEADING_MIN_SCALE = 0.85
BODY_MIN_SCALE = 0.75
TABLE_MIN_FONT_SIZE = 5.5
TABLE_BODY_MIN_SCALE = 0.75
TABLE_TITLE_MIN_SCALE = 0.85
SCALE_NUMERIC_TOLERANCE = 1e-9
INVISIBLE_STRUCTURAL_TEXT = frozenset(
    "\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b\u200c\u200d\u200e\u200f"
    "\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\u3164\ufeff\uffa0"
)
EXTRACTION_LIGATURES = str.maketrans({
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
})
MAX_HYPHEN_LINE_BREAK_VARIANTS = 1_024
MAX_HYPHEN_LINE_BREAK_BOUNDARIES = 10
HYPHEN_LINE_BREAK = re.compile(r"(?<=-)[ \t]*(?:\r\n|\r|\n)(?:[ \t]*(?:\r\n|\r|\n))*[ \t]*")


class PdfExportValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    plan: PdfExportPlan
    applied_replacements: int


def plan_pdf_export(path: Path, job: PdfExportJob, mode: ExportMode, temp_dir: Path) -> PdfExportPlan:
    temp_dir.mkdir(parents=True, exist_ok=True)
    typography = PdfTypographyResolver(temp_dir)
    overlap_analyzer = PdfImageOverlapAnalyzer()
    with pymupdf.open(path) as document:
        _validate_document_identity(path, document, job)
        regular_segments = [
            segment for segment in job.segments
            if segment.block_type not in {"table", "table_cell"} and segment.source_text_origin == "native"
        ]
        ocr_segments = [
            segment for segment in job.segments
            if segment.block_type not in {"table", "table_cell"} and segment.source_text_origin == "accepted_ocr"
        ]
        replacements = [_plan_segment(document, segment, typography, overlap_analyzer) for segment in regular_segments]
        ocr_replacements = [_plan_ocr_segment(document, segment, typography, overlap_analyzer) for segment in ocr_segments]
        tables, table_replacements = _plan_tables(document, job, typography, overlap_analyzer)
        replacements.extend(table_replacements)
        _mark_overlap_conflicts([*replacements, *ocr_replacements])
        for replacement in replacements:
            if replacement.skip_reason is not None or replacement.resolution != "blocked_other":
                continue
            if isinstance(replacement, PdfTableCellReplacement):
                _fit_table_cell(document[replacement.page_number - 1], replacement, typography, overlap_analyzer)
                continue
            page = document[replacement.page_number - 1]
            _fit_replacement(page, replacement, replacements, typography)
            if replacement.eligible and replacement.background_patch_used:
                _validate_expanded_patch_area(page, replacement, overlap_analyzer)
        for replacement in ocr_replacements:
            if replacement.skip_reason is None and replacement.resolution == "blocked_other":
                _fit_ocr_replacement(document[replacement.page_number - 1], replacement, typography)
        _refresh_table_status(tables)

    return PdfExportPlan(
        filename=_output_filename(job.source_filename, mode),
        mode=mode,
        document_sha256=job.document_identity.sha256,
        summary=_summarize(replacements),
        replacements=replacements,
        table_summary=_summarize_tables(tables),
        tables=tables,
        ocr_summary=_summarize_ocr(ocr_replacements),
        ocr_replacements=ocr_replacements,
        resolution_summary=_summarize_resolutions([*replacements, *ocr_replacements]),
    )


def export_translated_pdf(path: Path, job: PdfExportJob, mode: ExportMode, temp_dir: Path) -> ExportArtifact:
    plan = plan_pdf_export(path, job, mode, temp_dir)
    output_path = temp_dir / plan.filename
    with pymupdf.open(path) as document:
        if mode == "diagnostic":
            _write_diagnostics(document, plan)
            applied_replacements = 0
        else:
            expected_replacements = sum(
                item.eligible for item in [*plan.replacements, *plan.ocr_replacements]
            )
            applied_replacements = _write_replacements(document, plan, PdfTypographyResolver(temp_dir))
            if applied_replacements != expected_replacements:
                raise PdfExportValidationError(
                    "output_replacement_count_mismatch",
                    "Končni PDF ne vsebuje pričakovanega števila varnih zamenjav.",
                )
        document.subset_fonts()
        document.save(output_path, garbage=4, deflate=True)
    if mode == "replacement_preview":
        _verify_inserted_text(output_path, plan)
    return ExportArtifact(path=output_path, plan=plan, applied_replacements=applied_replacements)


def _validate_document_identity(path: Path, document: pymupdf.Document, job: PdfExportJob) -> None:
    identity = job.document_identity
    if identity.document_model_version != DOCUMENT_MODEL_VERSION:
        raise PdfExportValidationError("document_model_version_mismatch", "Različica dokumentnega modela se ne ujema.")
    if _sha256(path) != identity.sha256:
        raise PdfExportValidationError("pdf_hash_mismatch", "Prevajalsko opravilo pripada drugemu PDF-ju.")
    if document.page_count != identity.page_count or len(identity.pages) != document.page_count:
        raise PdfExportValidationError("page_count_mismatch", "Število strani PDF-ja se ne ujema s prevajalskim opravilom.")
    expected_pages = {page.page_number: page for page in identity.pages}
    if len(expected_pages) != document.page_count:
        raise PdfExportValidationError("page_geometry_mismatch", "Identiteta strani vsebuje podvojene ali manjkajoče strani.")
    for page_number, page in enumerate(document, start=1):
        expected = expected_pages.get(page_number)
        if expected is None or expected.rotation != page.rotation or not _close(expected.width, page.rect.width) or not _close(expected.height, page.rect.height):
            raise PdfExportValidationError("page_geometry_mismatch", f"Geometrija strani {page_number} se ne ujema.")


def _plan_segment(
    document: pymupdf.Document,
    segment: PdfExportSegment,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
) -> PdfReplacement:
    replacement = _new_replacement(segment)
    if segment.status == "skipped":
        return _not_applicable(replacement, "Segment ni prevajalska enota za PDF izvoz.")
    if segment.status != "reviewed":
        return _skip(replacement, "not_reviewed", "Izvozi se samo ročno potrjene prevode.")
    if not segment.edited_translation.strip():
        return _skip(replacement, "empty_translation", "Potrjeni segment nima prevoda.")
    if segment.source_bbox is None:
        return _skip(replacement, "missing_bbox", "Segment nima zanesljivega območja na strani.")
    if not 1 <= segment.page_number <= document.page_count:
        return _skip(replacement, "invalid_page", "Segment kaže na neobstoječo stran.")
    page = document[segment.page_number - 1]
    if page.rotation != 0 or segment.source_rotation not in {0, None}:
        return _skip(replacement, "rotated_page", "Rotirana stran je v prototipu varno preskočena.")
    rect = _rect(segment.source_bbox)
    if rect.is_empty or rect.is_infinite or not _inside(rect, page.rect):
        return _skip(replacement, "bbox_outside_page", "Območje segmenta ni v celoti znotraj strani.")
    if segment.source_text_origin != "native":
        assessment = overlap_analyzer.analyze(page, rect, native_text=False)
        _apply_overlap_assessment(replacement, assessment)
        return _skip(replacement, "ocr_only_source", "OCR območje nima varne povezave z digitalnim besedilom.")
    if segment.source_page_type != "digital":
        return _skip(replacement, "non_digital_source", "Segment ni iz digitalne besedilne strani.")
    extracted = page.get_text("text", clip=rect, sort=True)
    if not _text_matches(segment.source_text, extracted):
        return _skip(replacement, "source_text_mismatch", "Besedilo v navedenem območju se ne ujema z izvornim segmentom.")
    if _strict_display_text(segment.source_text) == _strict_display_text(segment.edited_translation):
        return _preserve_identical(replacement)
    assessment = overlap_analyzer.analyze(page, rect, preserve_list_marker=segment.block_type == "list_item")
    _apply_overlap_assessment(replacement, assessment)
    if assessment.kind != "none" and not assessment.safe_for_background_patch:
        return _skip(replacement, "image_overlap", _overlap_skip_message(assessment))
    style, asset, warnings, line_count = typography.resolve(page, rect, segment)
    replacement.typography = style
    replacement.typography_warnings.extend(warnings)
    replacement.requested_font_size = style.font_size
    replacement.proposed_font_size = style.font_size
    replacement.text_color = _style_color(style.color)
    replacement.color = style.color
    replacement.alignment = style.alignment
    replacement.line_height = style.line_height
    replacement.original_line_count = line_count
    replacement.font_weight = "bold" if style.bold else "regular"
    replacement.font_style = "italic" if style.italic else "normal"
    if asset is None:
        replacement.fit_strategy = "missing_glyph"
        return _skip(replacement, "missing_glyph", "Lokalni pisavi nimata vseh znakov prevoda.")
    replacement.font_family = asset.family
    replacement.proposed_font = asset.family
    return replacement


def _plan_ocr_segment(
    document: pymupdf.Document,
    segment: PdfExportSegment,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
) -> PdfOcrReplacement:
    replacement = PdfOcrReplacement(**_new_replacement(segment).model_dump())
    provenance = segment.accepted_ocr_source
    if segment.status != "reviewed":
        return _skip(replacement, "ocr_not_accepted", "OCR je sprejet, vendar prevod še ni ročno potrjen.")
    if not segment.edited_translation.strip():
        return _skip(replacement, "empty_translation", "Potrjeni OCR segment nima prevoda.")
    if provenance is None or segment.source_bbox is None:
        return _skip(replacement, "ocr_provenance_mismatch", "OCR segment nima preverljivega sprejetega vira in območja.")
    replacement.ocr_review_id = provenance.review_id
    replacement.ocr_source = provenance.source
    replacement.ocr_reason = provenance.reason
    registry_error = "unknown" if not provenance.revision_id else ocr_review_registry.validate(
        revision_id=provenance.revision_id,
        document_sha256=_sha256(Path(document.name)),
        review_id=provenance.review_id,
        page_number=segment.page_number,
        bbox=segment.source_bbox,
        text=segment.source_text,
    )
    if registry_error is not None:
        reason = {
            "unknown": "ocr_registry_missing",
            "unaccepted": "ocr_registry_unaccepted",
            "stale": "ocr_registry_stale",
        }.get(registry_error, "ocr_registry_mismatch")
        message = (
            "Strežniška OCR potrditev ni več veljavna: revizija je potekla ali je bil API ponovno zagnan. "
            "Ponovno registrirajte in sprejmite OCR, nato ponovno potrdite ohranjeni prevod."
            if registry_error == "unknown" else
            "Strežniška OCR revizija ni sprejeta, je zastarela ali se ne ujema. Ponovno registrirajte in sprejmite aktualni vir ter potrdite prevod."
        )
        return _skip(replacement, reason, message)
    if _strict_display_text(segment.source_text) == _strict_display_text(segment.edited_translation):
        return _preserve_identical(replacement)
    if segment.source_text != provenance.accepted_text or not _same_bbox(segment.source_bbox, provenance.bbox):
        return _skip(replacement, "ocr_provenance_mismatch", "Sprejeto OCR besedilo ali območje se je po potrditvi spremenilo.")
    if not 1 <= segment.page_number <= document.page_count:
        return _skip(replacement, "invalid_page", "OCR segment kaže na neobstoječo stran.")
    page = document[segment.page_number - 1]
    if page.rotation != 0 or segment.source_rotation not in {0, None}:
        return _skip(replacement, "rotated_page", "Rotirana stran je varno preskočena.")
    rect = _rect(segment.source_bbox)
    if rect.is_empty or rect.is_infinite or not _inside(rect, page.rect):
        return _skip(replacement, "bbox_outside_page", "OCR območje ni v celoti znotraj strani.")
    if rect.width < 6 or rect.height < 4 or rect.get_area() < 40:
        return _skip(replacement, "ocr_region_too_small", "OCR območje je premajhno za varen izvoz.")
    assessment = overlap_analyzer.analyze_ocr_region(page, rect)
    _apply_overlap_assessment(replacement, assessment)
    if not assessment.safe_for_background_patch:
        return _skip(replacement, "ocr_background_unsafe", _overlap_skip_message(assessment))
    style, asset, warnings, line_count = typography.resolve(page, rect, segment)
    replacement.typography = style
    replacement.typography_warnings.extend(warnings)
    replacement.requested_font_size = style.font_size
    replacement.proposed_font_size = style.font_size
    replacement.text_color = _style_color(style.color)
    replacement.color = style.color
    replacement.alignment = style.alignment
    replacement.line_height = style.line_height
    replacement.original_line_count = line_count
    replacement.font_weight = "bold" if style.bold else "regular"
    replacement.font_style = "italic" if style.italic else "normal"
    if asset is None:
        replacement.fit_strategy = "missing_glyph"
        return _skip(replacement, "missing_glyph", "Lokalni pisavi nimata vseh znakov prevoda.")
    replacement.font_family = asset.family
    replacement.proposed_font = asset.family
    return replacement


def _new_replacement(segment: PdfExportSegment) -> PdfReplacement:
    proposed_size = _bounded_font_size(segment.source_style.font_size if segment.source_style else None)
    source_color = _style_color(segment.source_style.color if segment.source_style else None)
    return PdfReplacement(
        segment_id=segment.segment_id,
        page_number=segment.page_number,
        source_bbox=segment.source_bbox,
        source_text=segment.source_text,
        translated_text=segment.edited_translation,
        translation_origin=segment.translation_origin,
        reviewed=segment.status == "reviewed",
        block_type=segment.block_type,
        proposed_font="FiraGO",
        proposed_font_size=proposed_size,
        requested_font_size=proposed_size,
        text_color=source_color,
        color=_color_hex(source_color),
        font_family="FiraGO",
        original_bbox=segment.source_bbox,
        fitted_bbox=segment.source_bbox,
        fit_status="not_checked",
    )


def _plan_tables(
    document: pymupdf.Document,
    job: PdfExportJob,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
) -> tuple[list[PdfTableExportPlan], list[PdfReplacement]]:
    segments_for_export = [
        segment for segment in job.segments
        if not _is_empty_structural_table_cell(segment)
    ]
    detector = PdfTableAnalyzer()
    detected = {
        page_number: detector.detect(document[page_number - 1], page_number)
        for page_number in sorted({segment.page_number for segment in segments_for_export if segment.block_type in {"table", "table_cell"}})
        if 1 <= page_number <= document.page_count
    }
    table_plans = {
        table.table_id: PdfTableExportPlan(
            table_id=table.table_id,
            page_number=table.page_number,
            bbox=table.bbox,
            row_count=table.row_count,
            column_count=table.column_count,
            reliable=table.reliable,
            warnings=list(table.warnings),
        )
        for tables in detected.values()
        for table in tables
    }
    replacements: list[PdfReplacement] = []
    cell_groups: dict[tuple[int, str], list[PdfExportSegment]] = defaultdict(list)
    container_blocks = {
        (segment.page_number, segment.block_id)
        for segment in segments_for_export
        if segment.block_type == "table"
    }
    cell_group_keys = {
        (segment.page_number, segment.block_id)
        for segment in segments_for_export
        if segment.block_type == "table_cell"
    }
    for segment in segments_for_export:
        if segment.block_type == "table_cell":
            cell_groups[(segment.page_number, segment.block_id)].append(segment)
        elif segment.block_type == "table":
            replacements.append(_not_applicable(
                _new_replacement(segment),
                "Vsebnik tabele je strukturna enota; izvozijo se njegove posamezne celice.",
            ))

    assigned_tables: set[str] = set()
    for (page_number, block_id), segments in cell_groups.items():
        positions = [parse_cell_position(segment.segment_id) for segment in segments]
        duplicate_positions = len([position for position in positions if position is not None]) != len(set(position for position in positions if position is not None))
        if any(position is None for position in positions):
            cells = [_unresolved_table_cell(segment, block_id, "ambiguous_cell_membership") for segment in segments]
            replacements.extend(cells)
            table_plans[block_id] = PdfTableExportPlan(
                table_id=block_id, page_number=page_number, reliable=False,
                warnings=["ambiguous_cell_membership"], cells=cells,
            )
            continue
        if duplicate_positions:
            cells = [_unresolved_table_cell(segment, block_id, "duplicate_table_content") for segment in segments]
            replacements.extend(cells)
            table_plans[block_id] = PdfTableExportPlan(
                table_id=block_id, page_number=page_number, reliable=False,
                warnings=["duplicate_table_content"], cells=cells,
            )
            continue
        expected_rows = max(position[0] for position in positions) + 1
        expected_columns = max(position[1] for position in positions) + 1
        candidates = [
            table for table in detected.get(page_number, [])
            if table.row_count == expected_rows and table.column_count == expected_columns
        ]
        ranked = sorted(
            ((_table_match_score(table, segments), table) for table in candidates),
            key=lambda item: item[0], reverse=True,
        )
        ambiguous = not ranked or ranked[0][0] < 0.5 or (len(ranked) > 1 and ranked[0][0] == ranked[1][0])
        if ambiguous or ranked[0][1].table_id in assigned_tables:
            cells = [_unresolved_table_cell(segment, block_id, "ambiguous_cell_membership") for segment in segments]
            replacements.extend(cells)
            table_plans[block_id] = PdfTableExportPlan(
                table_id=block_id, page_number=page_number, row_count=expected_rows,
                column_count=expected_columns, reliable=False,
                warnings=["ambiguous_cell_membership"], cells=cells,
            )
            continue
        table = ranked[0][1]
        assigned_tables.add(table.table_id)
        cells = [
            _plan_table_cell(document[page_number - 1], table, segment, typography, overlap_analyzer)
            for segment in segments
        ]
        replacements.extend(cells)
        plan = table_plans[table.table_id]
        plan.cells = sorted(cells, key=lambda cell: (cell.row_index, cell.column_index))
        if (page_number, block_id) in container_blocks:
            for cell in plan.cells:
                _skip(cell, "duplicate_table_content", "Vsebina je podvojena s tabelnim vsebnikom.")
    return list(table_plans.values()), replacements


def _is_empty_structural_table_cell(segment: PdfExportSegment) -> bool:
    if segment.block_type != "table_cell":
        return False
    return not any(
        not character.isspace() and character not in INVISIBLE_STRUCTURAL_TEXT
        for character in segment.source_text
    )


def _table_match_score(table: DetectedTable, segments: list[PdfExportSegment]) -> float:
    matches = 0
    checked = 0
    for segment in segments:
        position = parse_cell_position(segment.segment_id)
        if position is None:
            continue
        row, column = position
        checked += 1
        if row < table.row_count and column < table.column_count:
            matches += canonical_text(table.cells[row][column].text) == canonical_text(segment.source_text)
    return matches / max(1, checked)


def _unresolved_table_cell(segment: PdfExportSegment, table_id: str, reason: SkipReason) -> PdfTableCellReplacement:
    row, column = parse_cell_position(segment.segment_id) or (0, 0)
    cell = PdfTableCellReplacement(
        **_new_replacement(segment).model_dump(), table_id=table_id,
        row_index=row, column_index=column, row_span=1, column_span=1,
    )
    return _skip(cell, reason, "Celice ni mogoče enolično povezati z zaznano tabelo.")


def _plan_table_cell(
    page: pymupdf.Page,
    table: DetectedTable,
    segment: PdfExportSegment,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
) -> PdfTableCellReplacement:
    row, column = parse_cell_position(segment.segment_id) or (0, 0)
    geometry = table.cells[row][column]
    base = _new_replacement(segment).model_dump()
    base.update(
        source_bbox=geometry.source_bbox,
        original_bbox=geometry.source_bbox,
        fitted_bbox=geometry.content_bbox,
        warnings=list(geometry.warnings),
    )
    cell = PdfTableCellReplacement(
        **base, table_id=table.table_id, row_index=row, column_index=column,
        row_span=geometry.row_span, column_span=geometry.column_span,
        cell_bbox=geometry.cell_bbox, content_bbox=geometry.content_bbox,
        background_fill=geometry.background_fill, border_clearance=geometry.border_clearance,
        technical_value=is_technical_value(geometry.text),
    )
    if segment.status == "skipped":
        return _not_applicable(cell, "Celica ni prevajalska enota za PDF izvoz.")
    if not geometry.text:
        return _not_applicable(cell, "Prazna celica ostane samo del geometrije tabele.")
    if not table.reliable:
        return _skip(cell, "unsupported_table_structure", "Struktura tabele ni dovolj zanesljiva za celični izvoz.")
    if "missing_cell_bbox" in geometry.warnings:
        return _skip(cell, "missing_cell_bbox", "Celica nima zanesljive geometrije.")
    if "duplicate_cell_geometry" in geometry.warnings:
        return _skip(cell, "duplicate_cell_geometry", "Več celic uporablja isto geometrijo.")
    if geometry.source_bbox is None or geometry.content_bbox is None or geometry.cell_bbox is None:
        return _skip(cell, "missing_cell_bbox", "Celica nima zanesljivega vsebinskega območja.")
    if canonical_text(geometry.text) != canonical_text(segment.source_text):
        cell.source_match_status = "mismatch"
        return _skip(cell, "source_text_mismatch", "Izvorno besedilo se ne ujema z vsebino celice.")
    cell.source_match_status = "matched"
    if segment.status != "reviewed":
        return _skip(cell, "not_reviewed", "Celica ni ročno potrjena.")
    if not segment.edited_translation.strip():
        return _skip(cell, "empty_translation", "Potrjena celica nima prevoda.")
    if _strict_display_text(segment.source_text) == _strict_display_text(segment.edited_translation):
        return _preserve_identical(cell)
    if page.rotation != 0 or segment.source_rotation not in {0, None}:
        return _skip(cell, "rotated_page", "Rotirana tabela ni podprta.")
    if segment.source_text_origin != "native":
        assessment = overlap_analyzer.analyze(page, _rect(geometry.source_bbox), native_text=False)
        _apply_overlap_assessment(cell, assessment)
        return _skip(cell, "ocr_only_source", "Image-only oziroma OCR celica zahteva ročni potek.")
    if segment.source_page_type != "digital":
        return _skip(cell, "non_digital_source", "Celica ni iz digitalne strani.")
    assessment = overlap_analyzer.analyze(
        page,
        _rect(geometry.source_bbox),
        patch_clip=_rect(geometry.cell_bbox),
        ignore_enclosing_drawings=True,
        allow_opaque_vector_cover=True,
    )
    _apply_overlap_assessment(cell, assessment)
    if assessment.kind != "none" and not assessment.safe_for_background_patch:
        return _skip(cell, "image_overlap", _overlap_skip_message(assessment))
    source_style_rect = _rect(geometry.source_bbox)
    source_style_rect = pymupdf.Rect(
        source_style_rect.x0 - GEOMETRY_TOLERANCE,
        source_style_rect.y0 - GEOMETRY_TOLERANCE,
        source_style_rect.x1 + GEOMETRY_TOLERANCE,
        source_style_rect.y1 + GEOMETRY_TOLERANCE,
    ) & page.rect
    style, asset, warnings, line_count = typography.resolve(
        page, source_style_rect, segment, layout_rect=_rect(geometry.content_bbox)
    )
    cell.typography = style
    cell.typography_warnings.extend(warnings)
    cell.requested_font_size = style.font_size
    cell.proposed_font_size = style.font_size
    cell.text_color = _style_color(style.color)
    cell.color = style.color
    cell.alignment = style.alignment
    cell.line_height = style.line_height
    cell.original_line_count = line_count
    cell.font_weight = "bold" if style.bold else "regular"
    cell.font_style = "italic" if style.italic else "normal"
    if asset is None:
        cell.fit_strategy = "missing_glyph"
        return _skip(cell, "missing_glyph", "Lokalni pisavi nimata vseh znakov prevoda.")
    cell.font_family = asset.family
    cell.proposed_font = asset.family
    return cell


def _apply_overlap_assessment(replacement: PdfReplacement, assessment: ImageOverlapAssessment) -> None:
    replacement.image_overlap_kind = assessment.kind
    replacement.overlapping_image_xrefs = assessment.overlapping_image_xrefs
    replacement.overlap_ratio = assessment.overlap_ratio
    replacement.background_color = assessment.sampled_background_color
    replacement.background_metrics = assessment.metrics
    replacement.overlap_confidence = assessment.confidence
    replacement.patch_rects = assessment.patch_rects
    replacement.redaction_rects = assessment.redaction_rects
    replacement.list_marker_preserved = (
        replacement.block_type == "list_item"
        and assessment.safe_for_background_patch
        and bool(assessment.redaction_rects)
    )
    replacement.image_overlap_warnings.extend(assessment.warnings)
    replacement.background_patch_used = assessment.safe_for_background_patch


def _overlap_skip_message(assessment: ImageOverlapAssessment) -> str:
    reasons = {
        "complex_background": "Slikovno ozadje je preveč kompleksno za varen lokalni patch.",
        "image_only_text": "Besedilo obstaja samo v slikovni plasti in zahteva ročni OCR potek.",
        "uncertain": "Enotnosti slikovnega ozadja ni bilo mogoče dovolj zanesljivo dokazati.",
    }
    return reasons.get(assessment.kind, "Slikovnega prekrivanja ni mogoče varno popraviti.")


def _validate_expanded_patch_area(
    page: pymupdf.Page,
    replacement: PdfReplacement,
    analyzer: PdfImageOverlapAnalyzer,
) -> None:
    original = _layout_rect(replacement)
    fitted = _rect(replacement.fitted_bbox)
    if fitted == original:
        return
    assessment = analyzer.analyze(
        page,
        fitted,
        preserve_list_marker=replacement.block_type == "list_item",
        patch_rects=[fitted],
    )
    if not assessment.safe_for_background_patch or not _background_colors_close(replacement.background_color, assessment.sampled_background_color):
        replacement.background_patch_used = False
        replacement.image_overlap_warnings.append("expanded_background_not_safe")
        _skip(replacement, "image_overlap", "Razširjeno območje ne ostane na dokazljivo enotnem ozadju.")
        return
    replacement.background_metrics = assessment.metrics
    replacement.overlap_confidence = min(replacement.overlap_confidence, assessment.confidence)
    replacement.image_overlap_warnings.append("expanded_background_checked")


def _mark_overlap_conflicts(replacements: list[PdfReplacement]) -> None:
    candidates = [
        item for item in replacements
        if item.skip_reason is None and item.source_bbox is not None and item.resolution == "blocked_other"
    ]
    conflicting: set[str] = set()
    for index, first in enumerate(candidates):
        first_rect = _rect(first.source_bbox)
        for second in candidates[index + 1 :]:
            if first.page_number != second.page_number:
                continue
            intersection = first_rect & _rect(second.source_bbox)
            if _is_material_overlap(intersection):
                conflicting.update((first.segment_id, second.segment_id))
    for replacement in candidates:
        if replacement.segment_id in conflicting:
            reason: SkipReason = "overlapping_table_replacement" if isinstance(replacement, PdfTableCellReplacement) else "overlap_conflict"
            _skip(replacement, reason, "Območje se prekriva z drugim replacementom.")


def _is_material_overlap(intersection: pymupdf.Rect) -> bool:
    """Ignore sub-point extraction noise, but retain real two-dimensional overlap."""
    return (
        not intersection.is_empty
        and intersection.width > OVERLAP_TOLERANCE
        and intersection.height > OVERLAP_TOLERANCE
    )


def _fit_replacement(
    page: pymupdf.Page,
    replacement: PdfReplacement,
    replacements: list[PdfReplacement],
    typography: PdfTypographyResolver,
) -> None:
    original = _layout_rect(replacement)
    asset = _asset_for(replacement, typography)
    exact = _htmlbox_preflight(page.rect, original, replacement, typography, asset, 1.0)
    if exact[0] >= 0:
        return _accept_fit(replacement, original, exact, "exact")

    expanded = _safe_expansion(page, original, replacement, replacements)
    if expanded != original:
        expanded_fit = _htmlbox_preflight(page.rect, expanded, replacement, typography, asset, 1.0)
        if expanded_fit[0] >= 0:
            return _accept_fit(replacement, expanded, expanded_fit, "expanded_bbox")

    fit_rect = expanded if expanded != original else original
    minimum_scale = _minimum_text_scale(replacement)
    scaled = _htmlbox_preflight(page.rect, fit_rect, replacement, typography, asset, minimum_scale)
    if scaled[0] >= 0 and scaled[1] + 0.0001 >= minimum_scale:
        return _accept_fit(replacement, fit_rect, scaled, "scaled")

    replacement.fit_strategy = "overflow"
    _skip(replacement, "overflow", "Prevod se ne prilega znotraj dovoljene tipografske meje.", "overflow")


def _fit_ocr_replacement(
    page: pymupdf.Page,
    replacement: PdfOcrReplacement,
    typography: PdfTypographyResolver,
) -> None:
    """Fit OCR translation only inside its accepted bbox; expansion is intentionally forbidden."""
    rect = _rect(replacement.original_bbox)
    asset = _asset_for(replacement, typography)
    exact = _htmlbox_preflight(page.rect, rect, replacement, typography, asset, 1.0)
    if exact[0] >= 0:
        return _accept_fit(replacement, rect, exact, "exact")
    minimum_scale = _minimum_text_scale(replacement)
    scaled = _htmlbox_preflight(page.rect, rect, replacement, typography, asset, minimum_scale)
    if scaled[0] >= 0 and scaled[1] + 0.0001 >= minimum_scale:
        return _accept_fit(replacement, rect, scaled, "scaled")
    replacement.fit_strategy = "overflow"
    _skip(replacement, "overflow", "OCR prevod se ne prilega sprejetemu območju; območje se ne razširi.", "overflow")


def _minimum_text_scale(replacement: PdfReplacement) -> float:
    relative_minimum = HEADING_MIN_SCALE if replacement.block_type == "heading" else BODY_MIN_SCALE
    return max(MIN_FONT_SIZE / replacement.requested_font_size, relative_minimum)


def _fit_table_cell(
    page: pymupdf.Page,
    replacement: PdfTableCellReplacement,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
) -> None:
    content = _rect(replacement.content_bbox)
    asset = _asset_for(replacement, typography)
    exact = _htmlbox_preflight(page.rect, content, replacement, typography, asset, 1.0)
    if exact[0] >= 0:
        _accept_fit(replacement, content, exact, "exact")
    else:
        minimum_ratio = TABLE_TITLE_MIN_SCALE if replacement.column_span > 1 else TABLE_BODY_MIN_SCALE
        minimum_scale = max(TABLE_MIN_FONT_SIZE / replacement.requested_font_size, minimum_ratio)
        scaled = _htmlbox_preflight(page.rect, content, replacement, typography, asset, minimum_scale)
        if scaled[0] < 0 or scaled[1] + 0.0001 < minimum_scale:
            replacement.fit_strategy = "overflow"
            _skip(replacement, "cell_overflow", "Prevod se ne prilega varnemu območju celice.", "overflow")
            return
        _accept_fit(replacement, content, scaled, "scaled")

    if replacement.background_patch_used:
        assessment = overlap_analyzer.analyze(
            page,
            content,
            patch_rects=[content],
            patch_clip=_rect(replacement.cell_bbox),
            ignore_enclosing_drawings=True,
            allow_opaque_vector_cover=True,
        )
        if not assessment.safe_for_background_patch or not _background_colors_close(
            replacement.background_color, assessment.sampled_background_color
        ):
            replacement.background_patch_used = False
            replacement.image_overlap_warnings.append("table_content_background_not_safe")
            _skip(replacement, "image_overlap", "Celotno območje preloma nima dokazljivo varnega ozadja.")
            return
        replacement.background_metrics = assessment.metrics
        replacement.overlap_confidence = min(replacement.overlap_confidence, assessment.confidence)
        replacement.image_overlap_warnings.append("table_content_background_checked")


def _accept_fit(replacement: PdfReplacement, rect: pymupdf.Rect, result: tuple[float, float, int], strategy: FitStrategy) -> None:
    _, scale, line_count = result
    replacement.fitted_bbox = _bbox(rect)
    replacement.scale = scale
    replacement.fitted_font_size = round(replacement.requested_font_size * scale, 2)
    replacement.fitted_line_count = line_count
    replacement.fit_strategy = strategy
    replacement.fit_status = "fits"
    replacement.eligible = True
    replacement.resolution = "replacement_planned"


def _htmlbox_preflight(
    page_rect: pymupdf.Rect,
    rect: pymupdf.Rect,
    replacement: PdfReplacement,
    typography: PdfTypographyResolver,
    asset: FontAsset,
    scale_low: float,
) -> tuple[float, float, int]:
    html_text, css = typography.html_and_css(_rendered_translation(replacement), replacement.typography, asset)
    with pymupdf.open() as scratch:
        page = scratch.new_page(width=page_rect.width, height=page_rect.height)
        spare, scale = page.insert_htmlbox(rect, html_text, css=css, scale_low=scale_low, archive=typography.archive)
        line_count = sum(len(block.get("lines", [])) for block in page.get_text("dict", clip=rect).get("blocks", []))
    return spare, scale, line_count


def _safe_expansion(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    replacement: PdfReplacement,
    replacements: list[PdfReplacement],
) -> pymupdf.Rect:
    midpoint = page.rect.x0 + page.rect.width / 2
    if rect.x1 <= midpoint:
        right_limit = midpoint - 6
    elif rect.x0 >= midpoint:
        right_limit = page.rect.x1 - 12
    else:
        right_limit = min(page.rect.x1 - 12, rect.x1 + rect.width)
    candidate = pymupdf.Rect(
        rect.x0,
        max(page.rect.y0 + 12, rect.y0 - max(6.0, rect.height * 0.5)),
        min(right_limit, rect.x1 + max(36.0, rect.width * 1.5)),
        min(page.rect.y1 - 12, rect.y1 + max(12.0, rect.height * 1.2)),
    )
    obstacles = _text_span_rects(page)
    obstacles.extend(pymupdf.Rect(image["bbox"]) for image in page.get_image_info())
    obstacles.extend(pymupdf.Rect(drawing["rect"]) for drawing in page.get_drawings())
    obstacles.extend(
        _rect(item.original_bbox)
        for item in replacements
        if item is not replacement and item.page_number == replacement.page_number and item.original_bbox is not None
    )
    for obstacle in obstacles:
        if not (obstacle & rect).is_empty:
            continue
        if obstacle.x0 >= rect.x1 and not (pymupdf.Rect(rect.x1, rect.y0, candidate.x1, rect.y1) & obstacle).is_empty:
            candidate.x1 = min(candidate.x1, obstacle.x0 - 1)
        if obstacle.y0 >= rect.y1 and not (pymupdf.Rect(rect.x0, rect.y1, candidate.x1, candidate.y1) & obstacle).is_empty:
            candidate.y1 = min(candidate.y1, obstacle.y0 - 1)
        if obstacle.y1 <= rect.y0 and not (pymupdf.Rect(rect.x0, candidate.y0, candidate.x1, rect.y0) & obstacle).is_empty:
            candidate.y0 = max(candidate.y0, obstacle.y1 + 1)
    if candidate.x1 <= rect.x1 + GEOMETRY_TOLERANCE and candidate.y1 <= rect.y1 + GEOMETRY_TOLERANCE and candidate.y0 >= rect.y0 - GEOMETRY_TOLERANCE:
        return rect
    if any(not (candidate & obstacle).is_empty and (obstacle & rect).is_empty for obstacle in obstacles):
        return rect
    return candidate


def _write_diagnostics(document: pymupdf.Document, plan: PdfExportPlan) -> None:
    for replacement in [*plan.replacements, *plan.ocr_replacements]:
        diagnostic_bbox = replacement.cell_bbox if isinstance(replacement, PdfTableCellReplacement) else replacement.fitted_bbox or replacement.source_bbox
        if diagnostic_bbox is None:
            continue
        page = document[replacement.page_number - 1]
        rect = _rect(diagnostic_bbox)
        fit_colors = {"exact": (0.12, 0.55, 0.28), "expanded_bbox": (0.08, 0.45, 0.76), "scaled": (0.92, 0.48, 0.08)}
        overlap_colors = {
            "simple_background": (0.12, 0.55, 0.28),
            "complex_background": (0.78, 0.12, 0.16),
            "image_only_text": (0.55, 0.20, 0.70),
            "uncertain": (0.92, 0.48, 0.08),
        }
        if isinstance(replacement, PdfTableCellReplacement):
            if replacement.skip_reason == "technical_value":
                color = (0.08, 0.45, 0.76)
            elif replacement.skip_reason == "cell_overflow":
                color = (0.92, 0.48, 0.08)
            elif replacement.skip_reason in {"image_overlap", "ocr_only_source", "unsupported_table_structure"}:
                color = (0.55, 0.20, 0.70)
            elif replacement.eligible:
                color = (0.12, 0.55, 0.28)
            else:
                color = (0.78, 0.12, 0.16)
        else:
            color = overlap_colors.get(replacement.image_overlap_kind, fit_colors.get(replacement.fit_strategy, (0.78, 0.12, 0.16)))
        page.draw_rect(rect, color=color, width=0.8, overlay=True)
        if not isinstance(replacement, PdfTableCellReplacement):
            label_y = rect.y0 - 2 if rect.y0 >= 8 else rect.y0 + 7
            label = replacement.image_overlap_kind if replacement.image_overlap_kind != "none" else replacement.fit_strategy
            page.insert_text((rect.x0, label_y), f"{replacement.segment_id} {label}", fontname="helv", fontsize=5, color=color, overlay=True)
    for table in plan.tables:
        if table.bbox is None:
            continue
        page = document[table.page_number - 1]
        table_rect = _rect(table.bbox)
        rows: dict[int, list[PdfTableCellReplacement]] = defaultdict(list)
        for cell in table.cells:
            if cell.cell_bbox is not None:
                rows[cell.row_index].append(cell)
        for row_index, cells in rows.items():
            label = f"r{row_index + 1} " + "/".join(
                _table_diagnostic_code(cell) for cell in sorted(cells, key=lambda item: item.column_index)
            )
            font_size = 3.5
            label_width = pymupdf.get_text_length(label, fontname="helv", fontsize=font_size)
            if page.rect.x1 - table_rect.x1 >= label_width + 2:
                label_x = table_rect.x1 + 1
            elif table_rect.x0 - page.rect.x0 >= label_width + 2:
                label_x = table_rect.x0 - label_width - 1
            else:
                continue
            row_top = min(_rect(cell.cell_bbox).y0 for cell in cells)
            page.insert_text((label_x, row_top + 4), label, fontname="helv", fontsize=font_size, color=(0.25, 0.25, 0.25), overlay=True)


def _table_diagnostic_code(cell: PdfTableCellReplacement) -> str:
    if cell.eligible:
        return "G"
    if cell.skip_reason == "technical_value":
        return "B"
    if cell.skip_reason == "cell_overflow":
        return "O"
    if cell.skip_reason in {"image_overlap", "ocr_only_source", "unsupported_table_structure"}:
        return "V"
    return "R"


def _write_replacements(document: pymupdf.Document, plan: PdfExportPlan, typography: PdfTypographyResolver) -> int:
    eligible = [item for item in [*plan.replacements, *plan.ocr_replacements] if item.eligible and item.source_bbox is not None]
    pages = sorted({item.page_number for item in eligible})
    applied = 0
    for page_number in pages:
        page = document[page_number - 1]
        page_items = [item for item in eligible if item.page_number == page_number]
        for item in page_items:
            redactions = item.redaction_rects if item.background_patch_used and item.redaction_rects else [item.original_bbox]
            for redaction in redactions:
                page.add_redact_annot(_rect(redaction), fill=False, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        for item in page_items:
            if item.background_patch_used and item.background_color:
                patch_color = _style_color(item.background_color)
                for patch in item.patch_rects:
                    page.draw_rect(_rect(patch), color=None, fill=patch_color, fill_opacity=1, overlay=True)
            asset = _asset_for(item, typography)
            html_text, css = typography.html_and_css(_rendered_translation(item), item.typography, asset)
            spare, scale = page.insert_htmlbox(
                # PyMuPDF compares scale_low without tolerance and can return one ULP below
                # the same input. Validate its computed scale ourselves before saving.
                _rect(item.fitted_bbox), html_text, css=css, scale_low=0.0, archive=typography.archive, overlay=True
            )
            writer_floor = _accepted_writer_scale_floor(item)
            if spare < 0 or not _scale_meets_floor(scale, writer_floor):
                raise PdfExportValidationError(
                    "insert_failed",
                    f"Prevod segmenta {item.segment_id} se pri končnem zapisu ni prilegal "
                    f"(writer={scale:.12g}, floor={writer_floor:.12g}, preflight={item.scale or 1.0:.12g}).",
                )
            applied += 1
    return applied


def _scale_meets_floor(actual_scale: float, accepted_scale: float) -> bool:
    return actual_scale + SCALE_NUMERIC_TOLERANCE >= accepted_scale


def _accepted_writer_scale_floor(replacement: PdfReplacement) -> float:
    if replacement.fit_strategy in {"exact", "expanded_bbox"}:
        return 1.0
    return _minimum_text_scale(replacement)


def _verify_inserted_text(path: Path, plan: PdfExportPlan) -> None:
    with pymupdf.open(path) as document:
        for item in [*plan.replacements, *plan.ocr_replacements]:
            if not item.eligible or item.source_bbox is None:
                continue
            page = document[item.page_number - 1]
            verification_boxes = [item.source_bbox]
            if item.fitted_bbox is not None and not _same_bbox(item.fitted_bbox, item.source_bbox):
                verification_boxes.append(item.fitted_bbox)
            extracted_regions = [page.get_text("text", clip=_rect(bbox), sort=True) for bbox in verification_boxes]
            if item.fitted_bbox is not None:
                extracted_regions.extend(_text_blocks_in_rect(page, _rect(item.fitted_bbox)))
            if not any(_written_text_matches(_rendered_translation(item), text) for text in extracted_regions):
                raise PdfExportValidationError("output_verification_failed", f"Vstavljenega prevoda segmenta {item.segment_id} ni bilo mogoče preveriti.")


def _text_blocks_in_rect(page: pymupdf.Page, rect: pymupdf.Rect) -> list[str]:
    """Return complete text blocks that overlap an already-approved layout rectangle."""
    return [
        block[4]
        for block in page.get_text("blocks", clip=rect, sort=True)
        if len(block) > 6 and block[6] == 0 and block[4].strip()
    ]


def _rect(bbox) -> pymupdf.Rect:
    return pymupdf.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)


def _bbox(rect: pymupdf.Rect) -> BBox:
    return BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)


def _same_bbox(left: BBox, right: BBox) -> bool:
    return all(_close(getattr(left, key), getattr(right, key)) for key in ("x0", "y0", "x1", "y1"))


def _asset_for(replacement: PdfReplacement, typography: PdfTypographyResolver) -> FontAsset:
    if replacement.font_family == typography.fallback.family:
        return typography.fallback
    return typography.assets[(replacement.font_weight == "bold", replacement.font_style == "italic")]


def _style_color(value: int | str | None) -> tuple[float, float, float]:
    if isinstance(value, str):
        try:
            value = int(value[1:], 16) if value.startswith("#") else int(value, 10)
        except ValueError:
            return (0.0, 0.0, 0.0)
    if not isinstance(value, int) or not 0 <= value <= 0xFFFFFF:
        return (0.0, 0.0, 0.0)
    return (((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)


def _color_hex(value: tuple[float, float, float]) -> str:
    red, green, blue = (round(channel * 255) for channel in value)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _bounded_font_size(value: float | None) -> float:
    if value is None:
        return 10.0
    return min(72.0, max(MIN_FONT_SIZE, float(value)))


def _inside(rect: pymupdf.Rect, page_rect: pymupdf.Rect) -> bool:
    return (
        rect.x0 >= page_rect.x0 - GEOMETRY_TOLERANCE
        and rect.y0 >= page_rect.y0 - GEOMETRY_TOLERANCE
        and rect.x1 <= page_rect.x1 + GEOMETRY_TOLERANCE
        and rect.y1 <= page_rect.y1 + GEOMETRY_TOLERANCE
    )


def _background_colors_close(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    first = _style_color(left)
    second = _style_color(right)
    return sum((a - b) ** 2 for a, b in zip(first, second)) ** 0.5 <= 8 / 255


def _text_span_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    rectangles: list[pymupdf.Rect] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            rectangles.extend(pymupdf.Rect(span["bbox"]) for span in line.get("spans", []) if span.get("text", "").strip())
    return rectangles


def _text_matches(source: str, extracted: str) -> bool:
    expected = _canonical_text(source)
    actual = _canonical_text(extracted)
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    if expected in actual and len(expected) / len(actual) >= 0.85:
        return True
    return SequenceMatcher(None, expected, actual).ratio() >= 0.78


def _canonical_text(value: str) -> str:
    without_list_markers = re.sub(r"(?m)^\s*[-–—•▪◦*]+\s+(?=[^\W\d_])", "", value)
    return " ".join(without_list_markers.casefold().split())


def _written_text_matches(expected: str, extracted: str) -> bool:
    """Verify written content exactly, allowing only layout whitespace and a preserved list marker."""
    expected_text = _verification_text(expected)
    return expected_text in _verification_text_candidates(extracted)


def _verification_text_candidates(value: str) -> set[str]:
    """Keep physical hyphen line breaks distinct until strict comparison."""
    translated = value.translate(EXTRACTION_LIGATURES)
    without_list_marker = re.sub(r"^\s*[-–—•▪◦*]+\s*", "", translated, count=1)
    marker = "\u0000"
    marked, boundary_count = HYPHEN_LINE_BREAK.subn(marker, without_list_marker)
    standard = re.sub(r"[\s\u00a0]+", " ", marked).strip()
    if boundary_count == 0:
        return {standard}
    if boundary_count > MAX_HYPHEN_LINE_BREAK_BOUNDARIES:
        LOGGER.warning(
            "PDF text verification rejected %s hyphen line-break boundaries; the safe limit is %s variants.",
            boundary_count,
            MAX_HYPHEN_LINE_BREAK_VARIANTS,
        )
        return set()
    parts = standard.split(marker)
    candidates = {parts[0]}
    for part in parts[1:]:
        candidates = {
            candidate + separator + part
            for candidate in candidates
            for separator in ("", " ")
        }
    return candidates


def _verification_text(value: str) -> str:
    without_list_marker = re.sub(r"^\s*[-–—•▪◦*]+\s*", "", value.translate(EXTRACTION_LIGATURES), count=1)
    return re.sub(r"[\s\u00a0]+", " ", without_list_marker).strip()


def _rendered_translation(replacement: PdfReplacement) -> str:
    if replacement.list_marker_preserved:
        return re.sub(r"^\s*[-–—•▪◦*]+\s*", "", replacement.translated_text, count=1)
    return replacement.translated_text


def _layout_rect(replacement: PdfReplacement) -> pymupdf.Rect:
    if replacement.list_marker_preserved and replacement.redaction_rects:
        rects = [_rect(item) for item in replacement.redaction_rects]
        result = pymupdf.Rect(rects[0])
        for rect in rects[1:]:
            result |= rect
        return result
    return _rect(replacement.original_bbox)


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= GEOMETRY_TOLERANCE


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skip(replacement: PdfReplacement, reason: SkipReason, warning: str, fit_status: str = "skipped") -> PdfReplacement:
    replacement.skip_reason = reason
    replacement.fit_status = fit_status
    replacement.eligible = False
    if replacement.fit_strategy == "not_checked":
        replacement.fit_strategy = "overflow" if reason == "overflow" else "skipped"
    replacement.warnings.append(warning)
    replacement.resolution = _blocked_resolution(reason)
    return replacement


def _preserve_identical(replacement: PdfReplacement) -> PdfReplacement:
    replacement.fit_status = "skipped"
    replacement.fit_strategy = "skipped"
    replacement.eligible = False
    replacement.resolution = "preserved_identical"
    replacement.warnings.append("Prevod je po strogi prikazni primerjavi enak izvoru in ne zahteva zamenjave.")
    return replacement


def _not_applicable(replacement: PdfReplacement, warning: str) -> PdfReplacement:
    replacement.fit_status = "skipped"
    replacement.fit_strategy = "skipped"
    replacement.eligible = False
    replacement.resolution = "not_applicable"
    replacement.warnings.append(warning)
    return replacement


def _blocked_resolution(reason: SkipReason) -> str:
    if reason in {"overflow", "cell_overflow"}:
        return "blocked_overflow"
    if reason == "image_overlap":
        return "blocked_image_safety"
    if reason in {"overlap_conflict", "overlapping_table_replacement"}:
        return "blocked_overlap_conflict"
    if reason in {
        "complex_table", "duplicate_table_content", "ambiguous_cell_membership",
        "merged_cell_ambiguous", "missing_cell_bbox", "duplicate_cell_geometry",
        "unsupported_table_structure",
    }:
        return "blocked_table_structure"
    if reason.startswith("ocr_"):
        return "blocked_ocr_safety"
    return "blocked_other"


def _strict_display_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[\t \u00a0\u200b\ufeff]*\n[\t \u00a0\u200b\ufeff]*", " ", normalized)
    return normalized.strip(" \t\n\u00a0\u200b\ufeff")


def _summarize(replacements: list[PdfReplacement]) -> PdfExportSummary:
    return PdfExportSummary(
        total_segments=len(replacements),
        reviewed=sum(item.reviewed for item in replacements),
        eligible=sum(item.eligible for item in replacements),
        skipped=sum(not item.eligible for item in replacements),
        overflow=sum(item.skip_reason in {"overflow", "cell_overflow"} for item in replacements),
        conflicts=sum(item.skip_reason in {"overlap_conflict", "overlapping_table_replacement"} for item in replacements),
        exact=sum(item.fit_strategy == "exact" for item in replacements),
        expanded=sum(item.fit_strategy == "expanded_bbox" for item in replacements),
        scaled=sum(item.fit_strategy == "scaled" for item in replacements),
        background_patches=sum(item.background_patch_used for item in replacements),
        image_overlap_skipped=sum(item.skip_reason == "image_overlap" for item in replacements),
    )


def _summarize_resolutions(replacements: list[PdfReplacement]) -> PdfExportResolutionSummary:
    counts = Counter(item.resolution for item in replacements)
    blocked_total = sum(count for resolution, count in counts.items() if resolution.startswith("blocked_"))
    return PdfExportResolutionSummary(
        total_segments=len(replacements),
        replacement_planned=counts["replacement_planned"],
        preserved_identical=counts["preserved_identical"],
        not_applicable=counts["not_applicable"],
        blocked_overflow=counts["blocked_overflow"],
        blocked_image_safety=counts["blocked_image_safety"],
        blocked_vector_safety=counts["blocked_vector_safety"],
        blocked_overlap_conflict=counts["blocked_overlap_conflict"],
        blocked_table_structure=counts["blocked_table_structure"],
        blocked_ocr_safety=counts["blocked_ocr_safety"],
        blocked_other=counts["blocked_other"],
        blocked_total=blocked_total,
        resolved_if_exported=counts["replacement_planned"] + counts["preserved_identical"],
    )


def _summarize_ocr(replacements: list[PdfOcrReplacement]) -> PdfOcrExportSummary:
    return PdfOcrExportSummary(
        reviewed_regions=sum(item.reviewed for item in replacements),
        eligible_regions=sum(item.eligible for item in replacements),
        exported_regions=sum(item.eligible for item in replacements),
        unsafe_regions=sum(item.skip_reason in {"ocr_background_unsafe", "ocr_region_too_small", "overflow", "overlap_conflict"} for item in replacements),
        stale_regions=sum(item.skip_reason in {
            "ocr_provenance_mismatch", "ocr_registry_missing", "ocr_registry_unaccepted",
            "ocr_registry_stale", "ocr_registry_mismatch",
        } for item in replacements),
    )


def _refresh_table_status(tables: list[PdfTableExportPlan]) -> None:
    for table in tables:
        reviewed = [cell for cell in table.cells if cell.reviewed]
        table.partial_export = (
            bool(reviewed)
            and any(cell.resolution == "replacement_planned" for cell in reviewed)
            and any(cell.resolution.startswith("blocked_") for cell in reviewed)
        )


def _summarize_tables(tables: list[PdfTableExportPlan]) -> PdfTableExportSummary:
    cells = [cell for table in tables for cell in table.cells]
    ambiguous_reasons = {
        "ambiguous_cell_membership", "merged_cell_ambiguous", "missing_cell_bbox",
        "duplicate_cell_geometry", "unsupported_table_structure", "duplicate_table_content",
    }
    return PdfTableExportSummary(
        detected_tables=len([table for table in tables if table.bbox is not None]),
        reviewed_cells=sum(cell.reviewed for cell in cells),
        eligible_cells=sum(cell.eligible for cell in cells),
        exported_cells=sum(cell.eligible for cell in cells),
        technical_cells=sum(cell.technical_value for cell in cells),
        overflow_cells=sum(cell.skip_reason == "cell_overflow" for cell in cells),
        ambiguous_cells=sum(cell.skip_reason in ambiguous_reasons for cell in cells),
        image_only_cells=sum(cell.skip_reason == "ocr_only_source" for cell in cells),
        conflicting_cells=sum(cell.skip_reason == "overlapping_table_replacement" for cell in cells),
        partial_tables=sum(table.partial_export for table in tables),
    )


def _output_filename(filename: str, mode: ExportMode) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).stem).strip(".-") or "document"
    suffix = "sl-diagnostic" if mode == "diagnostic" else "sl"
    return f"{safe_stem}-{suffix}.pdf"
