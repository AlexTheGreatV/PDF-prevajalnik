from __future__ import annotations

import hashlib
import re
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
    PdfExportSegment,
    PdfExportSummary,
    PdfReplacement,
    SkipReason,
)
from app.services.pdf_typography import FontAsset, PdfTypographyResolver
from app.services.pdf_image_overlap import ImageOverlapAssessment, PdfImageOverlapAnalyzer

DOCUMENT_MODEL_VERSION = 1
MIN_FONT_SIZE = 6.0
GEOMETRY_TOLERANCE = 0.5
HEADING_MIN_SCALE = 0.85
BODY_MIN_SCALE = 0.75


class PdfExportValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    plan: PdfExportPlan


def plan_pdf_export(path: Path, job: PdfExportJob, mode: ExportMode, temp_dir: Path) -> PdfExportPlan:
    temp_dir.mkdir(parents=True, exist_ok=True)
    typography = PdfTypographyResolver(temp_dir)
    overlap_analyzer = PdfImageOverlapAnalyzer()
    with pymupdf.open(path) as document:
        _validate_document_identity(path, document, job)
        replacements = [_plan_segment(document, segment, typography, overlap_analyzer) for segment in job.segments]
        _mark_overlap_conflicts(replacements)
        for replacement in replacements:
            if replacement.skip_reason is not None:
                continue
            page = document[replacement.page_number - 1]
            _fit_replacement(page, replacement, replacements, typography)
            if replacement.eligible and replacement.background_patch_used:
                _validate_expanded_patch_area(page, replacement, overlap_analyzer)

    return PdfExportPlan(
        filename=_output_filename(job.source_filename),
        mode=mode,
        document_sha256=job.document_identity.sha256,
        summary=_summarize(replacements),
        replacements=replacements,
    )


def export_translated_pdf(path: Path, job: PdfExportJob, mode: ExportMode, temp_dir: Path) -> ExportArtifact:
    plan = plan_pdf_export(path, job, mode, temp_dir)
    output_path = temp_dir / plan.filename
    with pymupdf.open(path) as document:
        if mode == "diagnostic":
            _write_diagnostics(document, plan)
        else:
            _write_replacements(document, plan, PdfTypographyResolver(temp_dir))
        document.subset_fonts()
        document.save(output_path, garbage=4, deflate=True)
    if mode == "replacement_preview":
        _verify_inserted_text(output_path, plan)
    return ExportArtifact(path=output_path, plan=plan)


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
    proposed_size = _bounded_font_size(segment.source_style.font_size if segment.source_style else None)
    source_color = _style_color(segment.source_style.color if segment.source_style else None)
    replacement = PdfReplacement(
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
    if segment.status != "reviewed":
        return _skip(replacement, "not_reviewed", "Izvozi se samo ročno potrjene prevode.")
    if not segment.edited_translation.strip():
        return _skip(replacement, "empty_translation", "Potrjeni segment nima prevoda.")
    if segment.block_type in {"table", "table_cell"}:
        return _skip(replacement, "complex_table", "Kompleksne tabele v tem prototipu niso zamenjane.")
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


def _apply_overlap_assessment(replacement: PdfReplacement, assessment: ImageOverlapAssessment) -> None:
    replacement.image_overlap_kind = assessment.kind
    replacement.overlapping_image_xrefs = assessment.overlapping_image_xrefs
    replacement.overlap_ratio = assessment.overlap_ratio
    replacement.background_color = assessment.sampled_background_color
    replacement.background_metrics = assessment.metrics
    replacement.overlap_confidence = assessment.confidence
    replacement.patch_rects = assessment.patch_rects
    replacement.redaction_rects = assessment.redaction_rects
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
    original = _rect(replacement.original_bbox)
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
    candidates = [item for item in replacements if item.skip_reason is None and item.source_bbox is not None]
    conflicting: set[str] = set()
    for index, first in enumerate(candidates):
        first_rect = _rect(first.source_bbox)
        for second in candidates[index + 1 :]:
            if first.page_number != second.page_number:
                continue
            intersection = first_rect & _rect(second.source_bbox)
            if not intersection.is_empty and intersection.get_area() > GEOMETRY_TOLERANCE:
                conflicting.update((first.segment_id, second.segment_id))
    for replacement in candidates:
        if replacement.segment_id in conflicting:
            _skip(replacement, "overlap_conflict", "Območje se prekriva z drugim replacementom.")


def _fit_replacement(
    page: pymupdf.Page,
    replacement: PdfReplacement,
    replacements: list[PdfReplacement],
    typography: PdfTypographyResolver,
) -> None:
    original = _rect(replacement.original_bbox)
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
    minimum_scale = max(MIN_FONT_SIZE / replacement.requested_font_size, HEADING_MIN_SCALE if replacement.block_type == "heading" else BODY_MIN_SCALE)
    scaled = _htmlbox_preflight(page.rect, fit_rect, replacement, typography, asset, minimum_scale)
    if scaled[0] >= 0 and scaled[1] + 0.0001 >= minimum_scale:
        return _accept_fit(replacement, fit_rect, scaled, "scaled")

    replacement.fit_strategy = "overflow"
    _skip(replacement, "overflow", "Prevod se ne prilega znotraj dovoljene tipografske meje.", "overflow")


def _accept_fit(replacement: PdfReplacement, rect: pymupdf.Rect, result: tuple[float, float, int], strategy: FitStrategy) -> None:
    _, scale, line_count = result
    replacement.fitted_bbox = _bbox(rect)
    replacement.scale = round(scale, 4)
    replacement.fitted_font_size = round(replacement.requested_font_size * scale, 2)
    replacement.fitted_line_count = line_count
    replacement.fit_strategy = strategy
    replacement.fit_status = "fits"
    replacement.eligible = True


def _htmlbox_preflight(
    page_rect: pymupdf.Rect,
    rect: pymupdf.Rect,
    replacement: PdfReplacement,
    typography: PdfTypographyResolver,
    asset: FontAsset,
    scale_low: float,
) -> tuple[float, float, int]:
    html_text, css = typography.html_and_css(replacement.translated_text, replacement.typography, asset)
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
    for replacement in plan.replacements:
        if replacement.source_bbox is None:
            continue
        page = document[replacement.page_number - 1]
        rect = _rect(replacement.fitted_bbox or replacement.source_bbox)
        fit_colors = {"exact": (0.12, 0.55, 0.28), "expanded_bbox": (0.08, 0.45, 0.76), "scaled": (0.92, 0.48, 0.08)}
        overlap_colors = {
            "simple_background": (0.12, 0.55, 0.28),
            "complex_background": (0.78, 0.12, 0.16),
            "image_only_text": (0.55, 0.20, 0.70),
            "uncertain": (0.92, 0.48, 0.08),
        }
        color = overlap_colors.get(replacement.image_overlap_kind, fit_colors.get(replacement.fit_strategy, (0.78, 0.12, 0.16)))
        page.draw_rect(rect, color=color, width=0.8, overlay=True)
        label_y = rect.y0 - 2 if rect.y0 >= 8 else rect.y0 + 7
        label = replacement.image_overlap_kind if replacement.image_overlap_kind != "none" else replacement.fit_strategy
        page.insert_text((rect.x0, label_y), f"{replacement.segment_id} {label}", fontname="helv", fontsize=5, color=color, overlay=True)


def _write_replacements(document: pymupdf.Document, plan: PdfExportPlan, typography: PdfTypographyResolver) -> None:
    eligible = [item for item in plan.replacements if item.eligible and item.source_bbox is not None]
    pages = sorted({item.page_number for item in eligible})
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
            html_text, css = typography.html_and_css(item.translated_text, item.typography, asset)
            spare, scale = page.insert_htmlbox(
                _rect(item.fitted_bbox), html_text, css=css, scale_low=item.scale or 1.0, archive=typography.archive, overlay=True
            )
            if spare < 0 or scale + 0.0001 < (item.scale or 1.0):
                raise PdfExportValidationError("insert_failed", f"Prevod segmenta {item.segment_id} se pri končnem zapisu ni prilegal.")


def _verify_inserted_text(path: Path, plan: PdfExportPlan) -> None:
    with pymupdf.open(path) as document:
        for item in plan.replacements:
            if not item.eligible or item.source_bbox is None:
                continue
            extracted = document[item.page_number - 1].get_text("text", clip=_rect(item.fitted_bbox or item.source_bbox), sort=True)
            if not _text_matches(item.translated_text, extracted):
                raise PdfExportValidationError("output_verification_failed", f"Vstavljenega prevoda segmenta {item.segment_id} ni bilo mogoče preveriti.")


def _rect(bbox) -> pymupdf.Rect:
    return pymupdf.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)


def _bbox(rect: pymupdf.Rect) -> BBox:
    return BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)


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
    return " ".join(value.casefold().split())


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
    return replacement


def _summarize(replacements: list[PdfReplacement]) -> PdfExportSummary:
    return PdfExportSummary(
        total_segments=len(replacements),
        reviewed=sum(item.reviewed for item in replacements),
        eligible=sum(item.eligible for item in replacements),
        skipped=sum(not item.eligible for item in replacements),
        overflow=sum(item.skip_reason == "overflow" for item in replacements),
        conflicts=sum(item.skip_reason == "overlap_conflict" for item in replacements),
        exact=sum(item.fit_strategy == "exact" for item in replacements),
        expanded=sum(item.fit_strategy == "expanded_bbox" for item in replacements),
        scaled=sum(item.fit_strategy == "scaled" for item in replacements),
        background_patches=sum(item.background_patch_used for item in replacements),
        image_overlap_skipped=sum(item.skip_reason == "image_overlap" for item in replacements),
    )


def _output_filename(filename: str) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).stem).strip(".-") or "document"
    return f"{safe_stem}-sl-preview.pdf"
