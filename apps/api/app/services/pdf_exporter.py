from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf

from app.pdf_export_models import (
    ExportMode,
    PdfExportJob,
    PdfExportPlan,
    PdfExportSegment,
    PdfExportSummary,
    PdfReplacement,
    SkipReason,
)

DOCUMENT_MODEL_VERSION = 1
FONT_NAME = "Droid Sans Fallback Regular"
PDF_FONT_NAME = "jubpreview"
MIN_FONT_SIZE = 6.0
MAX_FONT_REDUCTION = 0.30
GEOMETRY_TOLERANCE = 0.5


class PdfExportValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    plan: PdfExportPlan


def prepare_export_font(temp_dir: Path) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    font = pymupdf.Font("china-s")
    required = "čšžČŠŽ₂°C"
    if any(font.has_glyph(ord(character)) == 0 for character in required):
        raise PdfExportValidationError("font_missing_glyph", "Lokalna PDF pisava nima vseh zahtevanih znakov.")
    path = temp_dir / "DroidSansFallback.ttf"
    path.write_bytes(font.buffer)
    return path


def plan_pdf_export(path: Path, job: PdfExportJob, mode: ExportMode, temp_dir: Path) -> PdfExportPlan:
    font_path = prepare_export_font(temp_dir)
    with pymupdf.open(path) as document:
        _validate_document_identity(path, document, job)
        replacements = [_plan_segment(document, segment, font_path) for segment in job.segments]
        _mark_overlap_conflicts(replacements)
        for replacement in replacements:
            if replacement.skip_reason is not None:
                continue
            page = document[replacement.page_number - 1]
            fitted_size = _fit_font_size(
                page.rect,
                _rect(replacement.source_bbox),
                replacement.translated_text,
                replacement.proposed_font_size,
                font_path,
            )
            if fitted_size is None:
                _skip(replacement, "overflow", "Prevod se ne prilega niti pri dovoljeni najmanjši velikosti.", "overflow")
            else:
                replacement.fitted_font_size = fitted_size
                replacement.fit_status = "fits"
                replacement.eligible = True

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
            _write_replacements(document, plan, temp_dir / "DroidSansFallback.ttf")
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


def _plan_segment(document: pymupdf.Document, segment: PdfExportSegment, font_path: Path) -> PdfReplacement:
    proposed_size = _bounded_font_size(segment.source_style.font_size if segment.source_style else None)
    replacement = PdfReplacement(
        segment_id=segment.segment_id,
        page_number=segment.page_number,
        source_bbox=segment.source_bbox,
        source_text=segment.source_text,
        translated_text=segment.edited_translation,
        translation_origin=segment.translation_origin,
        reviewed=segment.status == "reviewed",
        block_type=segment.block_type,
        proposed_font=FONT_NAME,
        proposed_font_size=proposed_size,
        text_color=_style_color(segment.source_style.color if segment.source_style else None),
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
    if segment.source_text_origin != "native":
        return _skip(replacement, "ocr_only_source", "OCR območje nima varne povezave z digitalnim besedilom.")
    if segment.source_page_type != "digital":
        return _skip(replacement, "non_digital_source", "Segment ni iz digitalne besedilne strani.")
    page = document[segment.page_number - 1]
    if page.rotation != 0 or segment.source_rotation not in {0, None}:
        return _skip(replacement, "rotated_page", "Rotirana stran je v prototipu varno preskočena.")
    rect = _rect(segment.source_bbox)
    if rect.is_empty or rect.is_infinite or not _inside(rect, page.rect):
        return _skip(replacement, "bbox_outside_page", "Območje segmenta ni v celoti znotraj strani.")
    if _overlaps_embedded_image(page, rect):
        return _skip(replacement, "image_overlap", "Območje se prekriva z vdelano sliko in ga ni mogoče varno odstraniti.")
    extracted = page.get_text("text", clip=rect, sort=True)
    if not _text_matches(segment.source_text, extracted):
        return _skip(replacement, "source_text_mismatch", "Besedilo v navedenem območju se ne ujema z izvornim segmentom.")
    font = pymupdf.Font(fontfile=str(font_path))
    missing = sorted({character for character in segment.edited_translation if not character.isspace() and font.has_glyph(ord(character)) == 0})
    if missing:
        return _skip(replacement, "missing_glyph", "Lokalna pisava nima vseh znakov prevoda.")
    if segment.source_style and (segment.source_style.bold or segment.source_style.italic):
        replacement.warnings.append("font_style_fallback")
    return replacement


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


def _fit_font_size(page_rect: pymupdf.Rect, rect: pymupdf.Rect, text: str, proposed: float, font_path: Path) -> float | None:
    minimum = max(MIN_FONT_SIZE, proposed * (1 - MAX_FONT_REDUCTION))
    candidate = proposed
    while candidate + 0.001 >= minimum:
        with pymupdf.open() as scratch:
            page = scratch.new_page(width=page_rect.width, height=page_rect.height)
            spare = page.insert_textbox(
                rect,
                text,
                fontname=PDF_FONT_NAME,
                fontfile=str(font_path),
                fontsize=candidate,
                lineheight=1.1,
                set_simple=0,
            )
        if spare >= 0:
            return round(candidate, 2)
        candidate -= 0.5
    return None


def _write_diagnostics(document: pymupdf.Document, plan: PdfExportPlan) -> None:
    for replacement in plan.replacements:
        if not replacement.eligible or replacement.source_bbox is None:
            continue
        page = document[replacement.page_number - 1]
        rect = _rect(replacement.source_bbox)
        page.draw_rect(rect, color=(0.08, 0.45, 0.76), width=0.8, overlay=True)
        label_y = rect.y0 - 2 if rect.y0 >= 8 else rect.y0 + 7
        page.insert_text((rect.x0, label_y), replacement.segment_id, fontname="helv", fontsize=5, color=(0.08, 0.35, 0.65), overlay=True)


def _write_replacements(document: pymupdf.Document, plan: PdfExportPlan, font_path: Path) -> None:
    eligible = [item for item in plan.replacements if item.eligible and item.source_bbox is not None]
    pages = sorted({item.page_number for item in eligible})
    for page_number in pages:
        page = document[page_number - 1]
        page_items = [item for item in eligible if item.page_number == page_number]
        for item in page_items:
            page.add_redact_annot(_rect(item.source_bbox), fill=False, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        for item in page_items:
            spare = page.insert_textbox(
                _rect(item.source_bbox),
                item.translated_text,
                fontname=PDF_FONT_NAME,
                fontfile=str(font_path),
                fontsize=item.fitted_font_size or item.proposed_font_size,
                lineheight=1.1,
                color=_text_color(item),
                set_simple=0,
                overlay=True,
            )
            if spare < 0:
                raise PdfExportValidationError("insert_failed", f"Prevod segmenta {item.segment_id} se pri končnem zapisu ni prilegal.")


def _verify_inserted_text(path: Path, plan: PdfExportPlan) -> None:
    with pymupdf.open(path) as document:
        for item in plan.replacements:
            if not item.eligible or item.source_bbox is None:
                continue
            extracted = document[item.page_number - 1].get_text("text", clip=_rect(item.source_bbox), sort=True)
            if not _text_matches(item.translated_text, extracted):
                raise PdfExportValidationError("output_verification_failed", f"Vstavljenega prevoda segmenta {item.segment_id} ni bilo mogoče preveriti.")


def _text_color(replacement: PdfReplacement) -> tuple[float, float, float]:
    return replacement.text_color


def _rect(bbox) -> pymupdf.Rect:
    return pymupdf.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)


def _style_color(value: int | str | None) -> tuple[float, float, float]:
    if isinstance(value, str):
        candidate = value.removeprefix("#")
        try:
            value = int(candidate, 16)
        except ValueError:
            return (0.0, 0.0, 0.0)
    if not isinstance(value, int) or not 0 <= value <= 0xFFFFFF:
        return (0.0, 0.0, 0.0)
    return (((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)


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


def _overlaps_embedded_image(page: pymupdf.Page, rect: pymupdf.Rect) -> bool:
    return any(not (pymupdf.Rect(image["bbox"]) & rect).is_empty for image in page.get_image_info())


def _text_matches(source: str, extracted: str) -> bool:
    expected = _canonical_text(source)
    actual = _canonical_text(extracted)
    if not expected or not actual:
        return False
    if expected == actual or expected in actual:
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
    )


def _output_filename(filename: str) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).stem).strip(".-") or "document"
    return f"{safe_stem}-sl-preview.pdf"
