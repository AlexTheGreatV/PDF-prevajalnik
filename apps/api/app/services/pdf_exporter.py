from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import pymupdf

from app.models import BBox, DocumentModel, TableCellProvenance, TableProvenance
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
    PdfTableCellProvenance,
    PdfTableExportPlan,
    PdfTableExportSummary,
    SkipReason,
)
from app.services.pdf_image_overlap import ImageOverlapAssessment, PdfImageOverlapAnalyzer, _drawing_items_intersect
from app.services.document_segmenter import DOCUMENT_MODEL_VERSION, segment_pdf
from app.services.pdf_table_export import DetectedTable, PdfTableAnalyzer, canonical_text, is_technical_value, parse_cell_position
from app.services.pdf_typography import FontAsset, MAX_SOURCE_FONT_SIZE, MIN_SOURCE_FONT_SIZE, PdfTypographyResolver
from app.services.ocr_review_registry import ocr_review_registry
from app.services.technical_integrity import validate_technical_integrity

LOGGER = logging.getLogger(__name__)

MIN_FONT_SIZE = 6.0
GEOMETRY_TOLERANCE = 0.5
OVERLAP_TOLERANCE = 0.1
HEADING_MIN_SCALE = 0.85
BODY_MIN_SCALE = 0.75
TABLE_MIN_FONT_SIZE = 5.5
TABLE_BODY_MIN_SCALE = 0.75
TABLE_TITLE_MIN_SCALE = 0.85
TABLE_MIN_LINE_HEIGHT = 1.0
TABLE_MAX_LAYOUT_LINES = 4
TABLE_SOURCE_FONT_SIZE_TOLERANCE = 0.05
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
TABLE_RANGE_PATTERN = re.compile(
    r"(?<!\w)[+\-−]?\d+(?:[.,]\d+)?(?:[ \t]*(?:°[CFK]|%|mm|cm|m|MPa|kPa|Pa|ml|L|kg|g|W|kW))?"
    r"[ \t]+(?:do|to|med|in|and|[-–—])[ \t]+"
    r"[+\-−]?\d+(?:[.,]\d+)?(?:[ \t]*(?:°[CFK]|%|mm|cm|m|MPa|kPa|Pa|ml|L|kg|g|W|kW))?(?!\w)",
    re.IGNORECASE,
)
TABLE_VALUE_UNIT_PATTERN = re.compile(
    r"(?<!\w)[+\-−]?\d+(?:[.,]\d+)?[ \t]+(?:°[CFK]|%|mm|cm|m|MPa|kPa|Pa|ml|L|kg|g|W|kW)(?!\w)",
    re.IGNORECASE,
)
TABLE_STANDARD_PATTERN = re.compile(r"\b(?:RAL|EN|ISO|DIN|SIST)[ \t]+[A-Z0-9][A-Z0-9./:+-]*\b", re.IGNORECASE)
TABLE_PRODUCT_PATTERN = re.compile(
    r"\b[A-Z][A-Za-z]+[ \t]+(?:[A-Z]{1,5}[ \t]*)?\d+[A-Za-z0-9-]*(?:[ \t]+[A-Z][A-Za-z]+){0,3}\b"
)


class PdfExportValidationError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _require_technical_integrity(segment: PdfExportSegment, authoritative_source: str) -> None:
    # Fuzzy source matching is only a geometry aid, never a protected-data proof.
    for stage, text in (("source", segment.source_text), ("target", segment.edited_translation)):
        result = validate_technical_integrity(authoritative_source, text)
        if not result.valid:
            raise PdfExportValidationError(
                "protected_data_mismatch",
                f"Zaščiteni podatki segmenta {segment.segment_id} se ne ujemajo ({stage}). "
                f"Pričakovano: {result.expected}; zaznano: {result.detected}.",
                details={"segment_id": segment.segment_id, "stage": stage,
                         "expected": result.expected, "detected": result.detected},
            )


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    plan: PdfExportPlan
    applied_replacements: int


def plan_pdf_export(
    path: Path, job: PdfExportJob, mode: ExportMode, temp_dir: Path,
    *, typography: PdfTypographyResolver | None = None,
) -> PdfExportPlan:
    temp_dir.mkdir(parents=True, exist_ok=True)
    typography = typography or PdfTypographyResolver(temp_dir)
    overlap_analyzer = PdfImageOverlapAnalyzer()
    authoritative_model = segment_pdf(path)
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
        tables, table_replacements = _plan_tables(
            document, job, typography, overlap_analyzer, authoritative_model,
        )
        replacements.extend(table_replacements)
        deferred_pairs = _mark_overlap_conflicts([*replacements, *ocr_replacements])
        for replacement in replacements:
            if replacement.skip_reason is not None or replacement.resolution != "blocked_other":
                continue
            if isinstance(replacement, PdfTableCellReplacement):
                _fit_table_cell(document[replacement.page_number - 1], replacement, typography, overlap_analyzer)
                continue
            page = document[replacement.page_number - 1]
            _fit_replacement(page, replacement, replacements, typography)
            if replacement.eligible and replacement.background_patch_used:
                _validate_expanded_patch_area(page, replacement, overlap_analyzer, typography)
        for replacement in ocr_replacements:
            if replacement.skip_reason is None and replacement.resolution == "blocked_other":
                _fit_ocr_replacement(document[replacement.page_number - 1], replacement, typography,
                                     replacements=[*replacements, *ocr_replacements], overlap_analyzer=overlap_analyzer)
        _validate_deferred_pairs(document, deferred_pairs, typography)
        _refresh_table_status(tables)

    return PdfExportPlan(
        filename=_output_filename(job.source_filename, mode),
        mode=mode,
        document_sha256=job.document_identity.sha256,
        typography_revision=getattr(typography, "typography_revision", None),
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
        raise PdfExportValidationError(
            "document_model_version_mismatch",
            "Različica dokumentnega modela se ne ujema. PDF ponovno analizirajte; staro opravilo ostane nespremenjeno.",
        )
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
    *, source_formatting: bool = False,
) -> PdfReplacement:
    if source_formatting and (segment.source_text_origin != "native" or segment.edited_translation != segment.source_text):
        raise ValueError("Source formatting may only write the exact native source text.")
    replacement = _new_replacement(segment)
    if segment.status == "skipped" and not source_formatting:
        return _not_applicable(replacement, "Segment ni prevajalska enota za PDF izvoz.")
    if segment.status != "reviewed" and not source_formatting:
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
    _require_technical_integrity(segment, extracted)
    if not source_formatting and _strict_display_text(segment.source_text) == _strict_display_text(segment.edited_translation):
        return _preserve_identical(replacement)
    if source_formatting and not _written_text_matches(segment.source_text, extracted):
        return _skip(replacement, "source_text_mismatch", "Oblikovanje zahteva natančno ujemanje izvornega besedila.")
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
    replacement.original_line_count = len(_source_body_lines(page, replacement)) if replacement.list_marker_preserved else line_count
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
        source=provenance.source,
        reason=provenance.reason,
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
    if segment.source_text != provenance.accepted_text or not _same_bbox(segment.source_bbox, provenance.bbox):
        return _skip(replacement, "ocr_provenance_mismatch", "Sprejeto OCR besedilo ali območje se je po potrditvi spremenilo.")
    _require_technical_integrity(segment, provenance.accepted_text)
    if _strict_display_text(segment.source_text) == _strict_display_text(segment.edited_translation):
        return _preserve_identical(replacement)
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
    authoritative_model: DocumentModel,
) -> tuple[list[PdfTableExportPlan], list[PdfReplacement]]:
    # Cell export always requires server-verified provenance. Older jobs may still
    # be opened, but their table cells remain blocked until rebuilt from analysis.
    if job.schema_version >= 9 or any(segment.block_type == "table_cell" for segment in job.segments):
        return _plan_provenance_tables(document, job, typography, overlap_analyzer, authoritative_model)
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


def _plan_provenance_tables(
    document: pymupdf.Document,
    job: PdfExportJob,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
    authoritative_model: DocumentModel,
) -> tuple[list[PdfTableExportPlan], list[PdfReplacement]]:
    authoritative: dict[str, tuple[TableProvenance, TableCellProvenance, str, int]] = {}
    plans: dict[str, PdfTableExportPlan] = {}
    for page in authoritative_model.pages:
        for region in page.regions:
            for block in region.blocks:
                table = block.table_provenance
                if table is None:
                    continue
                plans[table.table_id] = PdfTableExportPlan(
                    table_id=table.table_id,
                    page_number=page.page_number,
                    bbox=table.bbox,
                    row_count=table.row_count,
                    column_count=table.column_count,
                    reliable=not table.warnings,
                    warnings=list(table.warnings),
                )
                for cell in table.cells:
                    text = ""
                    if block.table_cells and cell.row_index < len(block.table_cells):
                        row = block.table_cells[cell.row_index]
                        if cell.column_index < len(row):
                            text = row[cell.column_index]
                    if cell.cell_id in authoritative:
                        plans[table.table_id].reliable = False
                        plans[table.table_id].warnings.append("duplicate_cell_id")
                        continue
                    authoritative[cell.cell_id] = (table, cell, text, page.page_number)

    container_blocks = {
        (segment.page_number, segment.block_id)
        for segment in job.segments
        if segment.block_type == "table"
    }
    client_cell_counts = Counter(
        segment.table_cell_provenance.cell_id
        for segment in job.segments
        if segment.block_type == "table_cell"
        and segment.table_cell_provenance is not None
        and not _is_empty_structural_table_cell(segment)
    )

    replacements: list[PdfReplacement] = []
    for segment in job.segments:
        if segment.block_type == "table":
            replacements.append(_not_applicable(
                _new_replacement(segment),
                "Vsebnik tabele je strukturna enota; izvozijo se posamezne dokazane celice.",
            ))
            continue
        if segment.block_type != "table_cell" or _is_empty_structural_table_cell(segment):
            continue
        client = segment.table_cell_provenance
        table_id = client.table_id if client else segment.block_id
        if (segment.page_number, segment.block_id) in container_blocks:
            cell = _unresolved_table_cell(segment, table_id, "duplicate_table_content")
            cell.provenance_block_reason = "table_container_and_cells_both_present"
        elif client is not None and client_cell_counts[client.cell_id] > 1:
            cell = _unresolved_table_cell(segment, table_id, "duplicate_table_content")
            cell.provenance_block_reason = "duplicate_cell_id_in_request"
        elif client is None:
            cell = _unresolved_table_cell(segment, table_id, "missing_table_provenance")
            cell.provenance_block_reason = "legacy_or_missing_provenance"
        else:
            registered = authoritative.get(client.cell_id)
            if registered is None:
                cell = _unresolved_table_cell(segment, table_id, "stale_table_provenance")
                cell.provenance_block_reason = "cell_not_present_in_source_pdf"
            else:
                table, source_cell, source_text, page_number = registered
                if page_number != segment.page_number or not _provenance_matches(client, table, source_cell):
                    cell = _unresolved_table_cell(segment, table_id, "stale_table_provenance")
                    cell.provenance_block_reason = "client_provenance_mismatch"
                else:
                    cell = _plan_provenance_cell(
                        document[page_number - 1], table, source_cell, source_text,
                        segment, typography, overlap_analyzer,
                    )
        replacements.append(cell)
        plan = plans.setdefault(table_id, PdfTableExportPlan(
            table_id=table_id,
            page_number=segment.page_number,
            reliable=False,
            warnings=["missing_table_provenance"],
        ))
        plan.cells.append(cell)

    for plan in plans.values():
        plan.cells.sort(key=lambda item: (item.row_index, item.column_index))
    return list(plans.values()), replacements


def _provenance_matches(
    client: PdfTableCellProvenance,
    table: TableProvenance,
    cell: TableCellProvenance,
) -> bool:
    return (
        client.table_id == table.table_id
        and client.cell_id == cell.cell_id
        and client.row_index == cell.row_index
        and client.column_index == cell.column_index
        and client.row_span == cell.row_span
        and client.column_span == cell.column_span
        and client.table_geometry_evidence == table.geometry_evidence
        and client.geometry_evidence == cell.geometry_evidence
        and client.source_span_ids == cell.source_span_ids
        and client.proven_edges == cell.proven_edges
        and client.ownership == cell.ownership
        and client.block_reason == cell.block_reason
        and client.has_graphic_overlap == cell.has_graphic_overlap
        and client.rotated_text == cell.rotated_text
        and client.background_fill == cell.background_fill
        and _optional_bbox_equal(client.table_bbox, table.bbox)
        and _optional_bbox_equal(client.cell_bbox, cell.cell_bbox)
        and _optional_bbox_equal(client.source_bbox, cell.source_bbox)
        and _optional_bbox_equal(client.content_bbox, cell.content_bbox)
    )


def _optional_bbox_equal(left: BBox | None, right: BBox | None) -> bool:
    if left is None or right is None:
        return left is right
    return _same_bbox(left, right)


def _plan_provenance_cell(
    page: pymupdf.Page,
    table: TableProvenance,
    provenance: TableCellProvenance,
    source_text: str,
    segment: PdfExportSegment,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
    *, source_formatting: bool = False,
) -> PdfTableCellReplacement:
    if source_formatting and (
        segment.source_text_origin != "native" or segment.edited_translation != source_text
        or segment.source_text != source_text
    ):
        raise ValueError("Source formatting may only write the authoritative native cell text.")
    if segment.status == "reviewed" and segment.source_text_origin == "native":
        _require_technical_integrity(segment, source_text)
    base = _new_replacement(segment).model_dump()
    base.update(
        source_bbox=provenance.source_bbox,
        original_bbox=provenance.source_bbox,
        fitted_bbox=provenance.content_bbox,
    )
    cell = PdfTableCellReplacement(
        **base,
        table_id=table.table_id,
        row_index=provenance.row_index,
        column_index=provenance.column_index,
        row_span=provenance.row_span,
        column_span=provenance.column_span,
        cell_bbox=provenance.cell_bbox,
        content_bbox=provenance.content_bbox,
        background_fill=provenance.background_fill,
        border_clearance=0.5,
        technical_value=is_technical_value(source_text),
        cell_id=provenance.cell_id,
        source_span_ids=list(provenance.source_span_ids),
        geometry_evidence=provenance.geometry_evidence,
        proven_edges=list(provenance.proven_edges),
        ownership=provenance.ownership,
        provenance_block_reason=provenance.block_reason,
    )
    island = _verified_cell_text_island(page, table, provenance, source_text)
    if island is not None:
        # Supplemental server-only write proof. Keep the analysis and client
        # provenance unchanged so existing content identities cannot drift.
        provenance = provenance.model_copy(update={
            "ownership": "owned", "has_graphic_overlap": False, "block_reason": None,
            "content_bbox": _bbox(island),
        })
        cell.content_bbox = _bbox(island)
        cell.ownership = "owned"
        cell.provenance_block_reason = None
        cell.typography_warnings.append("verified_native_cell_text_island")
    if segment.status == "skipped" and not source_formatting:
        return _not_applicable(cell, "Celica ni prevajalska enota za PDF izvoz.")
    if provenance.ownership == "empty":
        return _not_applicable(cell, "Prazna celica ostane del strukturnega modela.")
    if (provenance.row_span != 1 or provenance.column_span != 1) and island is None:
        return _skip(cell, "merged_cell_ambiguous", "Združena celica nima podprtega nedvoumnega razpona.")
    if provenance.has_graphic_overlap:
        return _skip(cell, "graphic_cell", "Celica vsebuje grafični element, polnilo ali swatch.")
    if provenance.rotated_text:
        return _skip(cell, "rotated_cell", "Rotirano besedilo v tabelni celici ni podprto.")
    if provenance.ownership != "owned" or len(provenance.proven_edges) != 4:
        reason: SkipReason = "span_crosses_cell_boundary" if provenance.block_reason == "span_crosses_cell_boundary" else "ambiguous_cell_membership"
        return _skip(cell, reason, "Lastništvo besedila v celici ni dokazljivo enolično.")
    if provenance.source_bbox is None or provenance.content_bbox is None or provenance.cell_bbox is None:
        return _skip(cell, "missing_cell_bbox", "Celica nima vseh zahtevanih geometrijskih dokazov.")
    if not provenance.source_span_ids:
        return _skip(cell, "missing_table_provenance", "Celica nima dokazljive povezave z izvornimi spani.")
    if canonical_text(source_text) != canonical_text(segment.source_text):
        cell.source_match_status = "mismatch"
        return _skip(cell, "source_text_mismatch", "Izvorno besedilo se ne ujema z dokazano vsebino celice.")
    cell.source_match_status = "matched"
    if segment.status != "reviewed" and not source_formatting:
        return _skip(cell, "not_reviewed", "Celica ni ročno potrjena.")
    if not segment.edited_translation.strip():
        return _skip(cell, "empty_translation", "Potrjena celica nima prevoda.")
    if not source_formatting and _strict_display_text(segment.source_text) == _strict_display_text(segment.edited_translation):
        return _preserve_identical(cell)
    if page.rotation != 0 or segment.source_rotation not in {0, None}:
        return _skip(cell, "rotated_page", "Rotirana tabela ni podprta.")
    if segment.source_text_origin != "native" or segment.source_page_type != "digital":
        return _skip(cell, "ocr_only_source", "OCR oziroma nedigitalna celica zahteva ločen preverjeni potek.")
    assessment = overlap_analyzer.analyze(
        page,
        _rect(provenance.source_bbox),
        patch_clip=_rect(provenance.cell_bbox),
        ignore_enclosing_drawings=True,
        allow_opaque_vector_cover=True,
        allow_matching_rect_fills=True,
    )
    _apply_overlap_assessment(cell, assessment)
    if assessment.kind != "none" and not assessment.safe_for_background_patch:
        return _skip(cell, "image_overlap", _overlap_skip_message(assessment))
    style, asset, warnings, line_count = typography.resolve(
        page, _rect(provenance.source_bbox), segment, layout_rect=_rect(provenance.content_bbox),
    )
    source_font_sizes = typography.source_font_sizes(page, _rect(provenance.source_bbox))
    cell.source_font_sizes = [round(value, 4) for value in source_font_sizes]
    source_font_size = _verified_table_source_font_size(source_font_sizes)
    if source_font_size is None:
        return _skip(cell, "missing_table_provenance", "Izvorne velikosti pisave v dokazani celici ni mogoče preveriti.")
    cell.source_font_size = round(source_font_size, 4)
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


def _verified_cell_text_island(
    page: pymupdf.Page, table: TableProvenance, cell: TableCellProvenance, source_text: str,
) -> pymupdf.Rect | None:
    """Prove an exclusive native caption inside an explicit opaque rectangle."""
    if (cell.block_reason not in {"merged_cell", "graphic_overlap"}
            or cell.rotated_text or cell.cell_bbox is None or cell.source_bbox is None
            or not cell.source_span_ids or len(cell.proven_edges) != 4):
        return None
    if any(set(other.source_span_ids) & set(cell.source_span_ids)
           for other in table.cells if other.cell_id != cell.cell_id):
        return None
    source = _rect(cell.source_bbox)
    container = _rect(cell.cell_bbox)
    if not container.contains(source) or not _text_matches(source_text, page.get_text(clip=source)):
        return None
    candidates = []
    for drawing in page.get_drawings():
        items = drawing.get("items", [])
        if (drawing.get("fill") is None or drawing.get("color") is not None
                or float(drawing.get("fill_opacity", 0.0)) < 0.999
                or len(items) != 1 or items[0][0] != "re"):
            continue
        rect = pymupdf.Rect(items[0][1])
        if not container.contains(rect) or not rect.contains(source):
            continue
        if any(other.source_bbox is not None and other.cell_id != cell.cell_id
               and _is_material_overlap(rect & _rect(other.source_bbox)) for other in table.cells):
            continue
        if any(not _contains_with_tolerance(source, span) and _is_material_overlap(rect & span)
               for span in _text_span_rects(page)):
            continue
        inset = pymupdf.Rect(rect.x0+0.5, rect.y0+0.5, rect.x1-0.5, rect.y1-0.5)
        if not inset.is_empty and not any(
            int(other.get("seqno", -1)) > int(drawing.get("seqno", -1))
            and _drawing_items_intersect(other, inset)
            and not (
                other.get("color") is None and other.get("fill") == drawing.get("fill")
                and float(other.get("fill_opacity", 0.0)) >= 0.999
                and len(other.get("items", [])) == 1 and other["items"][0][0] == "re"
                and pymupdf.Rect(other["items"][0][1]).contains(inset)
            )
            for other in page.get_drawings()
        ):
            candidates.append(inset)
    return min(candidates, key=lambda rect: rect.get_area(), default=None)


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
    if segment.status == "reviewed" and segment.source_text_origin == "native":
        _require_technical_integrity(segment, geometry.text)
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
    source_font_sizes = typography.source_font_sizes(page, source_style_rect)
    cell.source_font_sizes = [round(value, 4) for value in source_font_sizes]
    source_font_size = _verified_table_source_font_size(source_font_sizes)
    if source_font_size is None:
        return _skip(cell, "missing_table_provenance", "Izvorne velikosti pisave v dokazani celici ni mogoče preveriti.")
    cell.source_font_size = round(source_font_size, 4)
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
    typography: PdfTypographyResolver | None = None,
) -> None:
    original = _layout_rect(replacement)
    fitted = _rect(replacement.fitted_bbox)
    if fitted == original:
        return
    glyphs = None
    # Empty HTML leading is not painted by the writer, regardless of profile.
    # Assess the measured glyphs, while retaining the original erasure patches.
    if typography is not None:
        glyphs = _replacement_glyph_rects(page, replacement, typography)
    assessment = analyzer.analyze(
        page,
        _rect(replacement.source_bbox) if glyphs else fitted,
        preserve_list_marker=replacement.block_type == "list_item",
        patch_rects=glyphs or [fitted],
    )
    if not assessment.safe_for_background_patch or not _background_colors_close(replacement.background_color, assessment.sampled_background_color):
        replacement.background_patch_used = False
        replacement.image_overlap_warnings.append("expanded_background_not_safe")
        _skip(replacement, "image_overlap", "Razširjeno območje ne ostane na dokazljivo enotnem ozadju.")
        return
    replacement.background_metrics = assessment.metrics
    replacement.overlap_confidence = min(replacement.overlap_confidence, assessment.confidence)
    if glyphs:
        replacement.patch_rects.extend(assessment.patch_rects)
    replacement.image_overlap_warnings.append("expanded_background_checked")


def _source_write_regions(item: PdfReplacement) -> list[pymupdf.Rect]:
    if item.list_marker_preserved and item.background_patch_used and item.redaction_rects:
        return [_rect(r) for r in [*item.redaction_rects, *item.patch_rects]]
    return [_rect(item.source_bbox)]


def _mark_overlap_conflicts(replacements: list[PdfReplacement]) -> list[tuple[PdfReplacement, PdfReplacement]]:
    candidates = [
        item for item in replacements
        if item.skip_reason is None and item.source_bbox is not None and item.resolution == "blocked_other"
    ]
    conflicting: set[str] = set()
    deferred = []
    for index, first in enumerate(candidates):
        first_rect = _rect(first.source_bbox)
        for second in candidates[index + 1 :]:
            if first.page_number != second.page_number:
                continue
            intersection = first_rect & _rect(second.source_bbox)
            if _is_material_overlap(intersection):
                if (first.list_marker_preserved and second.list_marker_preserved
                        and first.background_patch_used and second.background_patch_used
                        and first.redaction_rects and second.redaction_rects
                        and not any(_is_material_overlap(a & b)
                                    for a in _source_write_regions(first) for b in _source_write_regions(second))):
                    deferred.append((first, second))
                    continue
                conflicting.update((first.segment_id, second.segment_id))
    for replacement in candidates:
        if replacement.segment_id in conflicting:
            reason: SkipReason = "overlapping_table_replacement" if isinstance(replacement, PdfTableCellReplacement) else "overlap_conflict"
            _skip(replacement, reason, "Območje se prekriva z drugim replacementom.")
    return deferred


def _replacement_glyph_rects(page, item, typography) -> list[pymupdf.Rect] | None:
    """Reproduce the accepted writer, not the whitespace of its layout rectangle."""
    if item.original_bbox is None or item.fitted_bbox is None:
        return None
    asset = _asset_for(item, typography)
    if "source_baseline_direct_write" in item.typography_warnings:
        source = _replacement_source_line_geometry(page, item)
        if source is None or item.scale is None:
            return None
        geometry = _direct_single_line_geometry(page.rect, source.bbox.x0, source.baseline_y, item, asset, item.scale)
        glyphs = [geometry.bbox] if geometry else None
    else:
        html, css = typography.html_and_css(_rendered_translation(item), item.typography,
            asset, use_full_table_interior=isinstance(item, PdfTableCellReplacement))
        with pymupdf.open() as scratch:
            target = scratch.new_page(width=page.rect.width, height=page.rect.height)
            spare, scale = target.insert_htmlbox(_rect(item.fitted_bbox), html, css=css,
                scale_low=0.0, archive=typography.archive)
            if (spare < 0 or not _scale_meets_floor(scale, _accepted_writer_scale_floor(item))
                    or not _written_text_matches(_rendered_translation(item), target.get_text())):
                return None
            glyphs = _text_span_rects(target)
    return glyphs if glyphs and all(_inside(r, page.rect) for r in glyphs) else None


def _validate_deferred_pairs(document, pairs, typography) -> None:
    for first, second in pairs:
        glyphs = [_replacement_glyph_rects(document[i.page_number-1], i, typography) if i.eligible else []
                  for i in (first, second)]
        regions = [[*_source_write_regions(i), *(g or [])] if i.eligible else []
                   for i, g in zip((first, second), glyphs)]
        # Retained markers are obstacles, not write regions: do not compare their
        # oversized font boxes to one another when neither is being rewritten.
        source_chars = []
        for item in (first, second):
            page = document[item.page_number-1]
            source_chars.append([pymupdf.Rect(c['bbox']) for b in page.get_text('rawdict', clip=_rect(item.source_bbox)).get('blocks', [])
                                 for line in b.get('lines', []) for span in line.get('spans', [])
                                 for c in span.get('chars', []) if c.get('c', '').strip()])
        conflict = (any(i.eligible and not g for i, g in zip((first, second), glyphs))
                    or any(_is_material_overlap(a & b) for a in regions[0] for b in regions[1])
                    or any(_is_material_overlap(a & b) for a in regions[0] for b in source_chars[1])
                    or any(_is_material_overlap(a & b) for a in regions[1] for b in source_chars[0]))
        if conflict:
            for item in (first, second):
                if item.eligible:
                    _skip(item, "overlap_conflict", "Dejanski zapis ali brisanje posega v sosednjo alinejo.")


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
    if getattr(typography, "typography_revision", None):
        replacement.typography_warnings.append("profile_source_body_anchor")
    if getattr(typography, "typography_revision", None) and _has_discrete_source_lines(page, replacement):
        _skip(replacement, "overlap_conflict", "Ločenih izvornih vrstic brez dokazljivega lastništva celic ni dovoljeno ponovno razporediti.")
        replacement.typography_warnings.append("discrete_source_lines_require_ownership")
        return
    _fit_replacement_layout(page, replacement, replacements, typography)
    if (replacement.eligible and (getattr(typography, "typography_revision", None) or replacement.list_marker_preserved
            or (replacement.block_type == "heading" and replacement.original_line_count == 1))
            and "source_baseline_direct_write" not in replacement.typography_warnings):
        _preserve_profile_source_inset(page, replacement, replacements, typography)


def _source_body_lines(page, replacement) -> list[dict]:
    """Use character evidence for an inline marker, not its shared span origin."""
    raw = page.get_text("rawdict", clip=_rect(replacement.source_bbox), sort=True)
    result = []
    first = True
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = [char for span in line.get("spans", []) for char in span.get("chars", [])]
            if first and replacement.list_marker_preserved:
                while chars and (chars[0]["c"].isspace() or chars[0]["c"] in "-–—•▪◦*"):
                    chars.pop(0)
            if not any(c["c"].strip() for c in chars):
                continue
            first = False
            spans = [{"text": c["c"], "origin": c["origin"], "bbox": c["bbox"]}
                     for c in chars if c["c"].strip()]
            result.append({"spans": spans, "dir": line.get("dir", (1, 0))})
    return result


def _replacement_source_line_geometry(page, replacement) -> _SingleLineGeometry | None:
    if ("profile_source_body_anchor" not in replacement.typography_warnings and not replacement.list_marker_preserved
            and replacement.original_line_count != 1):
        return _single_source_line_geometry(page, _layout_rect(replacement))
    lines = _source_body_lines(page, replacement)
    return _line_geometry(lines[0]) if len(lines) == 1 else None


def _has_discrete_source_lines(page, replacement) -> bool:
    if isinstance(replacement, (PdfTableCellReplacement, PdfOcrReplacement)):
        return False
    lines = _source_body_lines(page, replacement)
    geometry = [_line_geometry(line) for line in lines]
    if len(geometry) < 2 or any(g is None for g in geometry):
        return False
    # Separated labels are not wrapped prose. A shared block bbox does not prove
    # which row owns a translated label, even when the target has explicit breaks.
    return any(b.baseline_y - a.baseline_y > 2 * replacement.requested_font_size
               for a, b in zip(geometry, geometry[1:]))


def _preserve_profile_source_inset(page, replacement, replacements, typography) -> None:
    original = _layout_rect(replacement)
    lines = _source_body_lines(page, replacement)
    if not lines:
        _skip(replacement, "overlap_conflict", "Izvornega zamika besedila ni mogoče potrditi.")
        return
    source_first = _line_geometry(lines[0])
    if source_first is None:
        _skip(replacement, "overlap_conflict", "Izvorne osnovnice ni mogoče potrditi.")
        return
    source_x = source_first.bbox.x0
    # Measure the first body glyph and baseline, including markers sharing a span.
    # The HTML box may include blank leading; only measured ink is allowed to move.
    asset = _asset_for(replacement, typography)
    rect = _rect(replacement.fitted_bbox)
    html, css = typography.html_and_css(_rendered_translation(replacement), replacement.typography, asset)
    scale_floor = _accepted_writer_scale_floor(replacement)
    with pymupdf.open() as scratch:
        target = scratch.new_page(width=page.rect.width, height=page.rect.height)
        spare, scale = target.insert_htmlbox(rect, html, css=css, scale_low=scale_floor, archive=typography.archive)
        rendered = [line for block in target.get_text("dict").get("blocks", []) for line in block.get("lines", [])]
    if spare < 0 or not _scale_meets_floor(scale, scale_floor) or not rendered:
        _skip(replacement, "overlap_conflict", "Zamika oblikovanega besedila ni mogoče izmeriti.")
        return
    first = _line_geometry(rendered[0])
    if first is None:
        _skip(replacement, "overlap_conflict", "Osnovnice oblikovanega besedila ni mogoče izmeriti.")
        return
    delta_x = source_x - first.bbox.x0
    delta_y = source_first.baseline_y - first.baseline_y
    if abs(delta_x) <= GEOMETRY_TOLERANCE and abs(delta_y) <= GEOMETRY_TOLERANCE:
        return
    right = rect.x1 + delta_x if replacement.alignment != "left" else rect.x1
    candidate = pymupdf.Rect(rect.x0 + delta_x, rect.y0 + delta_y, right, rect.y1 + delta_y)
    if not _inside(candidate, page.rect) or candidate.width <= 0:
        _skip(replacement, "overlap_conflict", "Zamik bi presegel preverjeno območje besedila.")
        return
    fit = _htmlbox_preflight(page.rect, candidate, replacement, typography, asset, scale_floor)
    if fit[0] < 0:
        _skip(replacement, "overflow", "Besedilo se ob ohranjenem izvornem zamiku ne prilega.", "overflow")
        return
    with pymupdf.open() as scratch:
        target = scratch.new_page(width=page.rect.width, height=page.rect.height)
        target.insert_htmlbox(candidate, html, css=css, scale_low=scale_floor, archive=typography.archive)
        glyphs = _text_span_rects(target)
    available = original | _safe_expansion(page, original, replacement, replacements)
    obstacles = [r for r in _text_span_rects(page) if (r & _rect(replacement.source_bbox)).is_empty]
    obstacles.extend(_vector_stroke_obstacles(page))
    if (not glyphs or any(not _contains_with_tolerance(available, g) for g in glyphs)
            or any(_is_material_overlap(g & o) for g in glyphs for o in obstacles)):
        _skip(replacement, "overlap_conflict", "Zapis na izvorni osnovnici presega preverjeno območje ali seka sosednjo vsebino.")
        return
    _accept_fit(replacement, candidate, fit, replacement.fit_strategy)
    if not _align_single_line_around_vector_strokes(page, replacement, replacements, typography, asset):
        _skip(replacement, "overlap_conflict", "Besedila ob ohranjenem zamiku ni mogoče varno poravnati.")
        return
    replacement.typography_warnings.append("source_text_inset_preserved")
    replacement.typography_warnings.append("source_first_baseline_preserved")


def _fit_replacement_layout(
    page: pymupdf.Page,
    replacement: PdfReplacement,
    replacements: list[PdfReplacement],
    typography: PdfTypographyResolver,
) -> None:
    original = _layout_rect(replacement)
    asset = _asset_for(replacement, typography)
    if "document_profile_fixed_size" in replacement.typography_warnings:
        available = _safe_expansion(page, original, replacement, replacements)
        obstacles = [r for r in _text_span_rects(page) if not _contains_with_tolerance(original, r)]
        obstacles.extend(pymupdf.Rect(i["bbox"]) for i in page.get_image_info())
        for drawing in page.get_drawings():
            bounds = pymupdf.Rect(drawing["rect"])
            obstacles.append(bounds + (-0.5, -0.5, 0.5, 0.5))
        if _fit_single_line_on_source_baseline(
            page, available, replacement, asset, _minimum_text_scale(replacement),
            geometry_guard=lambda rect: not any(not (rect & obstacle).is_empty for obstacle in obstacles),
        ):
            return
    exact = _htmlbox_preflight(page.rect, original, replacement, typography, asset, 1.0)
    if exact[0] >= 0:
        return _accept_fit(replacement, original, exact, "exact")

    expanded = _safe_expansion(page, original, replacement, replacements)
    if expanded != original:
        expanded_fit = _htmlbox_preflight(page.rect, expanded, replacement, typography, asset, 1.0)
        if expanded_fit[0] >= 0:
            _accept_fit(replacement, expanded, expanded_fit, "expanded_bbox")
            if not _align_single_line_around_vector_strokes(page, replacement, replacements, typography, asset):
                _skip(replacement, "overlap_conflict", "Prevoda ni mogoče varno odmakniti od vektorske črte.")
            return

    fit_rect = expanded if expanded != original else original
    minimum_scale = _minimum_text_scale(replacement)
    scaled = _htmlbox_preflight(page.rect, fit_rect, replacement, typography, asset, minimum_scale)
    if scaled[0] >= 0 and scaled[1] + 0.0001 >= minimum_scale:
        _accept_fit(replacement, fit_rect, scaled, "scaled")
        if not _align_single_line_around_vector_strokes(page, replacement, replacements, typography, asset):
            _skip(replacement, "overlap_conflict", "Prevoda ni mogoče varno odmakniti od vektorske črte.")
        return

    if _fit_single_line_on_source_baseline(page, fit_rect, replacement, asset, minimum_scale):
        return

    replacement.fit_strategy = "overflow"
    _skip(replacement, "overflow", "Prevod se ne prilega znotraj dovoljene tipografske meje.", "overflow")


def _fit_ocr_replacement(
    page: pymupdf.Page,
    replacement: PdfOcrReplacement,
    typography: PdfTypographyResolver,
    *, replacements: list[PdfReplacement] | None = None,
    overlap_analyzer: PdfImageOverlapAnalyzer | None = None,
) -> None:
    """Keep OCR source/removal fixed; prove any extra native-text write space independently."""
    rect = _rect(replacement.original_bbox)
    asset = _asset_for(replacement, typography)
    exact = _htmlbox_preflight(page.rect, rect, replacement, typography, asset, 1.0)
    if exact[0] >= 0:
        return _accept_fit(replacement, rect, exact, "exact")
    minimum_scale = _minimum_text_scale(replacement)
    scaled = _htmlbox_preflight(page.rect, rect, replacement, typography, asset, minimum_scale)
    if scaled[0] >= 0 and scaled[1] + 0.0001 >= minimum_scale:
        return _accept_fit(replacement, rect, scaled, "scaled")
    if ("accepted_native_ocr_uniform_background_checked" in replacement.image_overlap_warnings
            and _fit_single_line_on_source_baseline(
                page, rect, replacement, asset, minimum_scale,
                geometry_guard=lambda glyph: rect.contains(glyph),
            )):
        replacement.typography_warnings.append("verified_native_ocr_source_baseline")
        return
    if ("accepted_native_ocr_uniform_background_checked" in replacement.image_overlap_warnings
            and overlap_analyzer is not None):
        available = _safe_expansion(page, rect, replacement, replacements or [replacement])
        if _fit_single_line_on_source_baseline(
            page, available, replacement, asset, minimum_scale,
            geometry_guard=lambda glyph: available.contains(glyph),
        ):
            glyphs = _replacement_glyph_rects(page, replacement, typography)
            assessment = overlap_analyzer.analyze_ocr_region(page, rect, write_rects=glyphs) if glyphs else None
            if assessment is not None and assessment.safe_for_background_patch:
                _apply_overlap_assessment(replacement, assessment)
                replacement.typography_warnings.append("verified_native_ocr_write_region")
                return
    replacement.fit_strategy = "overflow"
    _skip(replacement, "overflow", "OCR prevod se ne prilega dokazano varnemu območju za zapis.", "overflow")


def _minimum_text_scale(replacement: PdfReplacement) -> float:
    if "document_profile_fixed_size" in replacement.typography_warnings:
        return max(1.0, MIN_FONT_SIZE / replacement.requested_font_size)
    relative_minimum = HEADING_MIN_SCALE if replacement.block_type == "heading" else BODY_MIN_SCALE
    return max(MIN_FONT_SIZE / replacement.requested_font_size, relative_minimum)


def _table_minimum_scale(replacement: PdfTableCellReplacement) -> float:
    source_font_size = replacement.source_font_size
    requested_font_size = replacement.requested_font_size
    if source_font_size is None or not math.isfinite(source_font_size) or source_font_size <= 0:
        return math.inf
    if not math.isfinite(requested_font_size) or requested_font_size <= 0:
        return math.inf
    if source_font_size < TABLE_MIN_FONT_SIZE:
        # A source-proven small table face may be retained, never reduced further.
        minimum_font_size = source_font_size
        return minimum_font_size / requested_font_size
    relative_minimum = TABLE_TITLE_MIN_SCALE if replacement.column_span > 1 else TABLE_BODY_MIN_SCALE
    minimum_font_size = max(TABLE_MIN_FONT_SIZE, relative_minimum * source_font_size)
    floor = minimum_font_size / requested_font_size
    return max(1.0, floor) if "document_profile_fixed_size" in replacement.typography_warnings else floor


def _verified_table_source_font_size(source_font_sizes: list[float]) -> float | None:
    if not source_font_sizes:
        return None
    if any(
        not math.isfinite(size) or not MIN_SOURCE_FONT_SIZE <= size <= MAX_SOURCE_FONT_SIZE
        for size in source_font_sizes
    ):
        return None
    reference = source_font_sizes[0]
    if any(not math.isclose(size, reference, rel_tol=0.0, abs_tol=TABLE_SOURCE_FONT_SIZE_TOLERANCE) for size in source_font_sizes[1:]):
        # A single replacement cannot safely inherit two materially different
        # source sizes without knowing which translated words map to each span.
        return None
    return max(source_font_sizes)


def _table_line_height(replacement: PdfTableCellReplacement) -> float:
    current = replacement.typography.line_height
    if replacement.original_line_count != 1 or replacement.source_bbox is None or not replacement.source_font_size:
        return max(TABLE_MIN_LINE_HEIGHT, current)
    source_height = _rect(replacement.source_bbox).height
    source_ratio = source_height / replacement.source_font_size
    return max(TABLE_MIN_LINE_HEIGHT, min(current, source_ratio))


def _protect_table_layout_text(value: str) -> str:
    protected = value
    for pattern in (TABLE_RANGE_PATTERN, TABLE_VALUE_UNIT_PATTERN, TABLE_STANDARD_PATTERN, TABLE_PRODUCT_PATTERN):
        protected = pattern.sub(lambda match: re.sub(r"[ \t]+", "\u00a0", match.group(0)), protected)
    return protected


def _balanced_table_layouts(value: str) -> list[str]:
    protected = _protect_table_layout_text(value)
    words = [word for word in protected.split(" ") if word]
    layouts = [protected]
    for line_count in range(2, min(TABLE_MAX_LAYOUT_LINES, len(words)) + 1):
        total = sum(len(word) for word in words) + len(words) - 1
        targets = [total * index / line_count for index in range(1, line_count)]
        breaks: list[int] = []
        current = 0
        for index, word in enumerate(words[:-1], start=1):
            current += len(word) + 1
            if len(breaks) < len(targets) and current >= targets[len(breaks)]:
                breaks.append(index)
        if len(breaks) != line_count - 1:
            continue
        lines: list[str] = []
        start = 0
        for end in [*breaks, len(words)]:
            lines.append(" ".join(words[start:end]))
            start = end
        layout = "\n".join(lines)
        if layout not in layouts:
            layouts.append(layout)
    return layouts


def _try_table_layouts(
    page: pymupdf.Page,
    content: pymupdf.Rect,
    replacement: PdfTableCellReplacement,
    typography: PdfTypographyResolver,
    asset: FontAsset,
    layouts: list[str],
    minimum_scale: float,
    strategy: FitStrategy,
) -> bool:
    for layout in layouts:
        replacement.layout_text = layout
        measurement_floor = 1.0 if minimum_scale >= 1.0 else 0.0
        result = _htmlbox_preflight(page.rect, content, replacement, typography, asset, measurement_floor)
        if result[0] >= 0 and _scale_meets_floor(result[1], minimum_scale):
            if "\n" in layout:
                replacement.typography_warnings.append("safe_table_line_wrap")
            _accept_fit(replacement, content, result, strategy)
            return True
    replacement.layout_text = None
    return False


def _fit_table_cell(
    page: pymupdf.Page,
    replacement: PdfTableCellReplacement,
    typography: PdfTypographyResolver,
    overlap_analyzer: PdfImageOverlapAnalyzer,
) -> None:
    content = _rect(replacement.content_bbox)
    asset = _asset_for(replacement, typography)
    layouts = _balanced_table_layouts(replacement.translated_text)
    replacement.layout_text = layouts[0]
    exact = _htmlbox_preflight(page.rect, content, replacement, typography, asset, 1.0)
    if _fit_table_single_line_on_source_baseline(page, replacement, asset, 1.0):
        pass
    elif exact[0] >= 0:
        _accept_fit(replacement, content, exact, "exact")
    else:
        if not _try_table_layouts(page, content, replacement, typography, asset, layouts[1:], 1.0, "exact"):
            readable_line_height = _table_line_height(replacement)
            if readable_line_height < replacement.typography.line_height:
                replacement.typography.line_height = readable_line_height
                replacement.line_height = readable_line_height
                replacement.typography_warnings.append("source_proven_table_line_height")
            minimum_scale = _table_minimum_scale(replacement)
            if not _try_table_layouts(
                page, content, replacement, typography, asset, layouts, minimum_scale, "scaled",
            ) and not _fit_table_single_line_on_source_baseline(page, replacement, asset, minimum_scale):
                replacement.fit_strategy = "overflow"
                _skip(replacement, "cell_overflow", "Prevod se ne prilega varnemu območju celice.", "overflow")
                return

    glyphs = _replacement_glyph_rects(page, replacement, typography)
    if glyphs and "source_baseline_direct_write" not in replacement.typography_warnings:
        # HTML leading can put the font's measured ascent above the box. Move
        # the complete line stack, never shrink it or extend the cell interior.
        bounds = pymupdf.Rect(glyphs[0])
        for glyph in glyphs[1:]:
            bounds |= glyph
        dy = max(0.0, content.y0 - bounds.y0)
        if dy and bounds.y1 + dy <= content.y1:
            rect = _rect(replacement.fitted_bbox)
            replacement.fitted_bbox = _bbox(pymupdf.Rect(rect.x0, rect.y0 + dy, rect.x1, rect.y1 + dy))
            glyphs = _replacement_glyph_rects(page, replacement, typography)
    if not glyphs or any(not _contains_with_tolerance(content, g) for g in glyphs):
        _skip(replacement, "cell_overflow", "Dejanski glifi ne ohranijo varnega odmika znotraj celice.", "overflow")
        return
    if replacement.background_patch_used or any(
        not (pymupdf.Rect(info["bbox"]) & glyph).is_empty
        for info in page.get_image_info() for glyph in glyphs
    ):
        assessment = overlap_analyzer.analyze(
            page,
            _rect(replacement.source_bbox),
            patch_rects=glyphs or [content],
            patch_clip=content,
            ignore_enclosing_drawings=True,
            allow_opaque_vector_cover=True,
            allow_matching_rect_fills=True,
        )
        if assessment.kind == "none":
            return
        if not assessment.safe_for_background_patch or (replacement.background_patch_used and not _background_colors_close(
            replacement.background_color, assessment.sampled_background_color
        )):
            replacement.background_patch_used = False
            replacement.image_overlap_warnings.append("table_content_background_not_safe")
            _skip(replacement, "image_overlap", "Celotno območje preloma nima dokazljivo varnega ozadja.")
            return
        if not replacement.background_patch_used:
            _apply_overlap_assessment(replacement, assessment)
        replacement.background_metrics = assessment.metrics
        replacement.patch_rects.extend(assessment.patch_rects)
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
    html_text, css = typography.html_and_css(
        _rendered_translation(replacement), replacement.typography, asset,
        use_full_table_interior=isinstance(replacement, PdfTableCellReplacement),
    )
    with pymupdf.open() as scratch:
        page = scratch.new_page(width=page_rect.width, height=page_rect.height)
        spare, scale = page.insert_htmlbox(rect, html_text, css=css, scale_low=scale_low, archive=typography.archive)
        line_count = sum(len(block.get("lines", [])) for block in page.get_text("dict", clip=rect).get("blocks", []))
    return spare, scale, line_count


@dataclass(frozen=True)
class _SingleLineGeometry:
    bbox: pymupdf.Rect
    baseline_y: float


def _fit_table_single_line_on_source_baseline(
    page: pymupdf.Page,
    replacement: PdfTableCellReplacement,
    asset: FontAsset,
    minimum_scale: float,
) -> bool:
    if (
        replacement.ownership != "owned"
        or len(replacement.proven_edges) != 4
        or replacement.source_match_status != "matched"
        or not replacement.source_span_ids
        or replacement.geometry_evidence is None
        or replacement.row_span != 1
        or replacement.column_span != 1
        or replacement.source_font_size is None
        or replacement.typography is None
        or replacement.typography.dominant_style_confidence < 0.99
        or replacement.content_bbox is None
        or replacement.cell_bbox is None
    ):
        return False
    content = _rect(replacement.content_bbox)
    if content.is_empty or not _rect(replacement.cell_bbox).contains(content):
        return False
    obstacles = _table_baseline_obstacles(page, replacement)

    def safe_geometry(rect: pymupdf.Rect) -> bool:
        return content.contains(rect) and not any(not (rect & obstacle).is_empty for obstacle in obstacles)

    accepted = _fit_single_line_on_source_baseline(
        page, content, replacement, asset, minimum_scale, geometry_guard=safe_geometry,
    )
    if accepted:
        replacement.fit_strategy = "scaled" if replacement.scale < 1.0 else "exact"
        replacement.typography_warnings.append("verified_table_source_baseline")
    return accepted


def _table_baseline_obstacles(page: pymupdf.Page, replacement: PdfTableCellReplacement) -> list[pymupdf.Rect]:
    """Exclude only owned text and proven rectangular background fills, never cell strokes."""
    source = _rect(replacement.source_bbox)
    cell = _rect(replacement.cell_bbox)
    obstacles = [rect for rect in _text_span_rects(page) if not _contains_with_tolerance(source, rect)]
    obstacles.extend(pymupdf.Rect(info["bbox"]) for info in page.get_image_info())
    for drawing in page.get_drawings():
        items = drawing.get("items", [])
        background_fill = (
            len(items) == 1 and items[0][0] == "re"
            and _contains_with_tolerance(pymupdf.Rect(items[0][1]), cell)
        )
        if drawing.get("fill") is not None and not background_fill:
            obstacles.append(pymupdf.Rect(drawing["rect"]))
        if "s" not in str(drawing.get("type", "")):
            continue
        padding = float(drawing.get("width") or 0.0) / 2 + max(GEOMETRY_TOLERANCE, replacement.border_clearance)
        for item in items:
            if item[0] == "l":
                start, end = item[1], item[2]
                obstacles.append(pymupdf.Rect(
                    min(start.x, end.x) - padding, min(start.y, end.y) - padding,
                    max(start.x, end.x) + padding, max(start.y, end.y) + padding,
                ))
            elif item[0] == "re":
                rect = pymupdf.Rect(item[1])
                obstacles.extend((
                    pymupdf.Rect(rect.x0 - padding, rect.y0 - padding, rect.x1 + padding, rect.y0 + padding),
                    pymupdf.Rect(rect.x0 - padding, rect.y1 - padding, rect.x1 + padding, rect.y1 + padding),
                    pymupdf.Rect(rect.x0 - padding, rect.y0 - padding, rect.x0 + padding, rect.y1 + padding),
                    pymupdf.Rect(rect.x1 - padding, rect.y0 - padding, rect.x1 + padding, rect.y1 + padding),
                ))
            else:
                obstacles.append(pymupdf.Rect(drawing["rect"]))
    return obstacles


def _align_single_line_around_vector_strokes(
    page: pymupdf.Page,
    replacement: PdfReplacement,
    replacements: list[PdfReplacement],
    typography: PdfTypographyResolver,
    asset: FontAsset,
) -> bool:
    """Keep a proven source baseline when a single-line HTML replacement can be shifted safely."""
    if replacement.original_line_count != 1 or replacement.fitted_bbox is None or replacement.source_bbox is None:
        return True
    source_rect = _rect(replacement.source_bbox)
    source = _replacement_source_line_geometry(page, replacement)
    rendered = _single_rendered_line_geometry(page.rect, _rect(replacement.fitted_bbox), replacement, typography, asset)
    if source is None or rendered is None:
        return True

    strokes = _vector_stroke_obstacles(page)
    rendered_collisions = [stroke for stroke in strokes if _is_material_overlap(rendered.bbox & stroke)]
    delta_y = source.baseline_y - rendered.baseline_y
    if abs(delta_y) <= GEOMETRY_TOLERANCE:
        return True
    shifted = pymupdf.Rect(
        replacement.fitted_bbox.x0,
        replacement.fitted_bbox.y0 + delta_y,
        replacement.fitted_bbox.x1,
        replacement.fitted_bbox.y1 + delta_y,
    )
    if not _inside(shifted, page.rect):
        return False
    aligned = _single_rendered_line_geometry(page.rect, shifted, replacement, typography, asset)
    if aligned is None or abs(aligned.baseline_y - source.baseline_y) > GEOMETRY_TOLERANCE:
        return False
    if any(_is_material_overlap(aligned.bbox & stroke) for stroke in strokes):
        return False

    obstacles = [
        rect for rect in _text_span_rects(page)
        if (rect & source_rect).is_empty
    ]
    obstacles.extend(
        pymupdf.Rect(image["bbox"])
        for image in page.get_image_info()
        if (pymupdf.Rect(image["bbox"]) & source_rect).is_empty
    )
    obstacles.extend(
        _rect(item.original_bbox)
        for item in replacements
        if item is not replacement
        and item.page_number == replacement.page_number
        and item.original_bbox is not None
        and (_rect(item.original_bbox) & source_rect).is_empty
    )
    if any(_is_material_overlap(aligned.bbox & obstacle) for obstacle in obstacles):
        return False

    replacement.fitted_bbox = _bbox(shifted)
    replacement.typography_warnings.append("source_baseline_preserved")
    if rendered_collisions:
        replacement.typography_warnings.append("source_baseline_aligned_around_vector_stroke")
    return True


def _fit_single_line_on_source_baseline(
    page: pymupdf.Page,
    fit_rect: pymupdf.Rect,
    replacement: PdfReplacement,
    asset: FontAsset,
    minimum_scale: float,
    *,
    geometry_guard: Callable[[pymupdf.Rect], bool] | None = None,
) -> bool:
    """Use the proven source baseline when HTML box leading alone causes overflow."""
    if (
        replacement.original_line_count != 1
        or replacement.source_bbox is None
        or replacement.alignment != "left"
        or "\n" in _rendered_translation(replacement)
        or not math.isfinite(minimum_scale)
        or not 0 < minimum_scale <= 1
    ):
        return False
    source = _replacement_source_line_geometry(page, replacement)
    if source is None:
        return False
    fit_rect = _bounded_by_enclosing_vector_stroke(page, _layout_rect(replacement), fit_rect)

    def measured(scale: float) -> _SingleLineGeometry | None:
        geometry = _direct_single_line_geometry(
            page.rect,
            source.bbox.x0,
            source.baseline_y,
            replacement,
            asset,
            scale,
        )
        if geometry is None or not _contains_with_tolerance(fit_rect, geometry.bbox):
            return None
        if geometry_guard is not None and not geometry_guard(geometry.bbox):
            return None
        return geometry

    exact = measured(1.0)
    if exact is not None:
        scale = 1.0
        geometry = exact
    else:
        geometry = measured(minimum_scale)
        if geometry is None:
            return False
        low, high = minimum_scale, 1.0
        for _ in range(24):
            candidate = (low + high) / 2
            candidate_geometry = measured(candidate)
            if candidate_geometry is None:
                high = candidate
            else:
                low = candidate
                geometry = candidate_geometry
        scale = low

    replacement.fitted_bbox = _bbox(geometry.bbox)
    replacement.scale = scale
    replacement.fitted_font_size = round(replacement.requested_font_size * scale, 2)
    replacement.fitted_line_count = 1
    if scale < 1.0:
        replacement.fit_strategy = "scaled"
    elif _contains_with_tolerance(_layout_rect(replacement), geometry.bbox):
        replacement.fit_strategy = "exact"
    else:
        replacement.fit_strategy = "expanded_bbox"
    replacement.fit_status = "fits"
    replacement.eligible = True
    replacement.resolution = "replacement_planned"
    replacement.typography_warnings.append("source_baseline_direct_write")
    return True


def _direct_single_line_geometry(
    page_rect: pymupdf.Rect,
    origin_x: float,
    baseline_y: float,
    replacement: PdfReplacement,
    asset: FontAsset,
    scale: float,
) -> _SingleLineGeometry | None:
    with pymupdf.open() as scratch:
        page = scratch.new_page(width=page_rect.width, height=page_rect.height)
        page.insert_font(fontname=asset.code, fontfile=str(asset.path))
        written = page.insert_text(
            (origin_x, baseline_y),
            _rendered_translation(replacement),
            fontname=asset.code,
            fontsize=replacement.requested_font_size * scale,
        )
        if written != 1:
            return None
        lines = [
            line
            for block in page.get_text("dict").get("blocks", [])
            for line in block.get("lines", [])
            if any(span.get("text", "").strip() for span in line.get("spans", []))
        ]
    if len(lines) != 1:
        return None
    extracted = "".join(span.get("text", "") for span in lines[0].get("spans", []))
    if not _written_text_matches(_rendered_translation(replacement), extracted):
        return None
    return _line_geometry(lines[0])


def _contains_with_tolerance(outer: pymupdf.Rect, inner: pymupdf.Rect) -> bool:
    return (
        inner.x0 >= outer.x0 - GEOMETRY_TOLERANCE
        and inner.y0 >= outer.y0 - GEOMETRY_TOLERANCE
        and inner.x1 <= outer.x1 + GEOMETRY_TOLERANCE
        and inner.y1 <= outer.y1 + GEOMETRY_TOLERANCE
    )


def _bounded_by_enclosing_vector_stroke(
    page: pymupdf.Page,
    source_rect: pymupdf.Rect,
    fit_rect: pymupdf.Rect,
) -> pymupdf.Rect:
    """Keep direct text inside the smallest stroked container that owns its source."""
    containers: list[tuple[float, pymupdf.Rect, float]] = []
    for drawing in page.get_drawings():
        if "s" not in str(drawing.get("type", "")):
            continue
        bounds = pymupdf.Rect(drawing["rect"])
        if not _contains_with_tolerance(bounds, source_rect):
            continue
        containers.append((bounds.width * bounds.height, bounds, float(drawing.get("width") or 0.0)))
    if not containers:
        return fit_rect
    _, bounds, stroke_width = min(containers, key=lambda item: item[0])
    clearance = max(GEOMETRY_TOLERANCE, stroke_width / 2)
    interior = pymupdf.Rect(
        bounds.x0 + clearance,
        bounds.y0 + clearance,
        bounds.x1 - clearance,
        bounds.y1 - clearance,
    )
    return fit_rect & interior


def _single_source_line_geometry(page: pymupdf.Page, rect: pymupdf.Rect) -> _SingleLineGeometry | None:
    lines = [
        line
        for block in page.get_text("dict", clip=rect).get("blocks", [])
        for line in block.get("lines", [])
        if any(span.get("text", "").strip() for span in line.get("spans", []))
    ]
    if len(lines) != 1:
        return None
    return _line_geometry(lines[0])


def _single_rendered_line_geometry(
    page_rect: pymupdf.Rect,
    rect: pymupdf.Rect,
    replacement: PdfReplacement,
    typography: PdfTypographyResolver,
    asset: FontAsset,
) -> _SingleLineGeometry | None:
    html_text, css = typography.html_and_css(_rendered_translation(replacement), replacement.typography, asset)
    with pymupdf.open() as scratch:
        page = scratch.new_page(width=page_rect.width, height=page_rect.height)
        spare, scale = page.insert_htmlbox(rect, html_text, css=css, scale_low=0.0, archive=typography.archive)
        # HTML preflight and the final writer can converge on slightly different
        # scales. Measure the writer's actual baseline using its accepted floor.
        if spare < 0 or replacement.scale is None or not _scale_meets_floor(scale, _accepted_writer_scale_floor(replacement)):
            return None
        lines = [
            line
            for block in page.get_text("dict", clip=rect).get("blocks", [])
            for line in block.get("lines", [])
            if any(span.get("text", "").strip() for span in line.get("spans", []))
        ]
    if len(lines) != 1:
        return None
    return _line_geometry(lines[0])


def _line_geometry(line: dict) -> _SingleLineGeometry | None:
    spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
    if not spans or any("origin" not in span or "bbox" not in span for span in spans):
        return None
    baseline_values = [float(span["origin"][1]) for span in spans]
    if max(baseline_values) - min(baseline_values) > GEOMETRY_TOLERANCE:
        return None
    bbox = pymupdf.Rect(spans[0]["bbox"])
    for span in spans[1:]:
        bbox |= pymupdf.Rect(span["bbox"])
    return _SingleLineGeometry(bbox=bbox, baseline_y=sum(baseline_values) / len(baseline_values))


def _vector_stroke_obstacles(page: pymupdf.Page) -> list[pymupdf.Rect]:
    obstacles: list[pymupdf.Rect] = []
    for drawing in page.get_drawings():
        half_width = max(0.01, float(drawing.get("width") or 0.0) / 2)
        for item in drawing.get("items", []):
            if item[0] != "l":
                continue
            start, end = item[1], item[2]
            obstacles.append(pymupdf.Rect(
                min(start.x, end.x) - half_width,
                min(start.y, end.y) - half_width,
                max(start.x, end.x) + half_width,
                max(start.y, end.y) + half_width,
            ))
    return obstacles


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
    candidate = _bounded_by_enclosing_vector_stroke(page, rect, candidate)
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
        direct_origins = {
            item.segment_id: geometry
            for item in page_items
            if "source_baseline_direct_write" in item.typography_warnings
            and (geometry := _replacement_source_line_geometry(page, item)) is not None
        }
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
            if "source_baseline_direct_write" in item.typography_warnings:
                source = direct_origins.get(item.segment_id)
                if source is None or item.scale is None:
                    raise PdfExportValidationError(
                        "insert_failed",
                        f"Izvorne osnovnice segmenta {item.segment_id} pri končnem zapisu ni bilo mogoče potrditi.",
                    )
                page.insert_font(fontname=asset.code, fontfile=str(asset.path))
                written = page.insert_text(
                    (source.bbox.x0, source.baseline_y),
                    _rendered_translation(item),
                    fontname=asset.code,
                    fontsize=item.requested_font_size * item.scale,
                    color=_style_color(item.color),
                    overlay=True,
                )
                if written != 1:
                    raise PdfExportValidationError(
                        "insert_failed",
                        f"Enovrstični prevod segmenta {item.segment_id} pri končnem zapisu ni bil vstavljen.",
                    )
                applied += 1
                continue
            html_text, css = typography.html_and_css(
                _rendered_translation(item), item.typography, asset,
                use_full_table_interior=isinstance(item, PdfTableCellReplacement),
            )
            writer_floor = _accepted_writer_scale_floor(item)
            spare, scale = page.insert_htmlbox(
                # Measure the natural writer scale again and compare it with the
                # independently computed preflight result below.
                _rect(item.fitted_bbox), html_text, css=css, scale_low=0.0,
                archive=typography.archive, overlay=True,
            )
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
    if isinstance(replacement, PdfTableCellReplacement):
        return replacement.scale if replacement.scale is not None else _table_minimum_scale(replacement)
    if replacement.fit_strategy in {"exact", "expanded_bbox"}:
        return 1.0
    return _minimum_text_scale(replacement)


def _verify_inserted_text(
    path: Path, plan: PdfExportPlan, *, measured_glyph_regions: dict[str, list[BBox]] | None = None,
) -> None:
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
            # Adjacent paragraphs can become one extraction block. Server-measured
            # writer glyph boxes allow strict full-text comparison without including
            # neighboring glyphs from the unused part of an HTML layout rectangle.
            measured = (measured_glyph_regions or {}).get(item.segment_id, [])
            bounds = _rect(item.source_bbox) | _rect(item.fitted_bbox or item.source_bbox)
            if measured and all(_contains_with_tolerance(bounds, _rect(box)) for box in measured):
                extracted_regions.append("\n".join(page.get_text("text", clip=_rect(box), sort=True) for box in measured))
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
    value = replacement.layout_text if replacement.layout_text is not None else replacement.translated_text
    if replacement.list_marker_preserved:
        return re.sub(r"^\s*[-–—•▪◦*]+\s*", "", value, count=1)
    return value


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
        "unsupported_table_structure", "missing_table_provenance", "stale_table_provenance",
        "span_crosses_cell_boundary", "graphic_cell", "rotated_cell",
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
        "missing_table_provenance", "stale_table_provenance", "span_crosses_cell_boundary",
        "graphic_cell", "rotated_cell",
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
