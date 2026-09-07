from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pymupdf as fitz

from app.models import (
    BBox,
    DocumentBlock,
    DocumentImage,
    DocumentModel,
    DocumentPage,
    OcrCandidate,
    OcrReason,
    OcrResponse,
    OcrResult,
    OcrSource,
    OcrStatus,
    OcrWord,
)
from app.services.document_segmenter import segment_pdf

MIN_DPI = 100
MAX_DPI = 240
DEFAULT_DPI = 180
MAX_IMAGE_SIDE_PX = 3200
OCR_TIMEOUT_SECONDS = 20


class OcrUnavailableError(RuntimeError):
    pass


class OcrTimeoutError(RuntimeError):
    pass


class OcrProvider(Protocol):
    name: str

    def status(self) -> OcrStatus:
        ...

    def recognize(
        self,
        image_path: Path,
        *,
        bbox: BBox,
        language: str,
        source: OcrSource,
        dpi: int,
        page_bbox: BBox,
    ) -> OcrResult:
        ...


@dataclass
class TesseractCliOcrProvider:
    executable: str = "tesseract"
    timeout_seconds: int = OCR_TIMEOUT_SECONDS
    name: str = "tesseract-cli"

    def status(self) -> OcrStatus:
        executable_path = shutil.which(self.executable)
        pymupdf_ocr_available = hasattr(fitz.Page, "get_textpage_ocr") or hasattr(fitz.Pixmap, "pdfocr_tobytes")
        if not executable_path:
            return OcrStatus(
                available=False,
                provider=self.name,
                executable=None,
                tessdata_prefix=os.environ.get("TESSDATA_PREFIX"),
                pymupdf_ocr_available=pymupdf_ocr_available,
                message="Lokalni OCR ni na voljo: tesseract ni najden na PATH.",
            )

        version = _run_version(executable_path, self.timeout_seconds)
        languages = _run_languages(executable_path, self.timeout_seconds)
        return OcrStatus(
            available=True,
            provider=self.name,
            executable=executable_path,
            version=version,
            languages=languages,
            tessdata_prefix=os.environ.get("TESSDATA_PREFIX"),
            pymupdf_ocr_available=pymupdf_ocr_available,
        )

    def recognize(
        self,
        image_path: Path,
        *,
        bbox: BBox,
        language: str,
        source: OcrSource,
        dpi: int,
        page_bbox: BBox,
    ) -> OcrResult:
        executable_path = shutil.which(self.executable)
        if not executable_path:
            raise OcrUnavailableError("Lokalni OCR ni na voljo: tesseract ni najden na PATH.")

        start = time.perf_counter()
        command = [executable_path, str(image_path), "stdout", "-l", language, "--psm", "6", "tsv"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrTimeoutError("Lokalni OCR je presegel casovno omejitev.") from exc

        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or "Tesseract OCR ni uspel.").strip())

        words = _parse_tesseract_tsv(completed.stdout, bbox=bbox, dpi=dpi, page_bbox=page_bbox)
        text = " ".join(word.text for word in words)
        confidence = _average_confidence(words)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return OcrResult(
            text=text,
            words=words,
            confidence=confidence,
            bbox=bbox,
            language=language,
            provider=self.name,
            processing_time_ms=elapsed_ms,
            source=source,
        )


@dataclass
class MockOcrProvider:
    results: dict[str, OcrResult]
    available: bool = True
    name: str = "mock"

    def status(self) -> OcrStatus:
        return OcrStatus(available=self.available, provider=self.name, pymupdf_ocr_available=True)

    def recognize(
        self,
        image_path: Path,
        *,
        bbox: BBox,
        language: str,
        source: OcrSource,
        dpi: int,
        page_bbox: BBox,
    ) -> OcrResult:
        if not self.available:
            raise OcrUnavailableError("Mock OCR ni na voljo.")
        return self.results.get(
            source,
            OcrResult(
                text="",
                words=[],
                confidence=0,
                bbox=bbox,
                language=language,
                provider=self.name,
                processing_time_ms=0,
                source=source,
            ),
        )


def ocr_status(provider: OcrProvider | None = None) -> OcrStatus:
    return (provider or TesseractCliOcrProvider()).status()


def run_selective_ocr(
    pdf_path: Path,
    *,
    filename: str,
    temp_root: Path,
    provider: OcrProvider | None = None,
    language: str = "eng",
    dpi: int = DEFAULT_DPI,
) -> OcrResponse:
    provider = provider or TesseractCliOcrProvider()
    status = provider.status()
    document = segment_pdf(pdf_path)
    candidates = plan_ocr_candidates(document)

    if not status.available:
        for candidate in candidates:
            candidate.status = "unavailable"
            candidate.message = status.message
        return OcrResponse(filename=filename, status=status, candidates=candidates)

    safe_dpi = min(MAX_DPI, max(MIN_DPI, dpi))
    temp_root.mkdir(parents=True, exist_ok=True)
    work_dir = temp_root / f"ocr-{uuid.uuid4().hex}"
    work_dir.mkdir()
    try:
        with fitz.open(pdf_path) as pdf:
            for candidate in candidates:
                page = pdf[candidate.page_number - 1]
                page_bbox = BBox(x0=0, y0=0, x1=float(page.rect.width), y1=float(page.rect.height))
                image_path, scale = render_bbox_to_png(page, candidate.bbox, work_dir, safe_dpi)
                candidate.dpi = safe_dpi
                candidate.scale = scale
                try:
                    result = provider.recognize(
                        image_path,
                        bbox=candidate.bbox,
                        language=language,
                        source=candidate.source,
                        dpi=safe_dpi,
                        page_bbox=page_bbox,
                    )
                    candidate.result = result
                    candidate.ocr_text = result.text
                    candidate.ocr_confidence = result.confidence
                    candidate.status = "processed"
                    candidate.requires_review = _requires_review(candidate)
                    candidate.ocr_applied = _can_apply_automatically(candidate)
                    candidate.selected_text_source = "ocr" if candidate.ocr_applied else "manual_review"
                except Exception as exc:
                    candidate.status = "failed"
                    candidate.message = str(exc)
    finally:
        _safe_rmtree(work_dir, temp_root)

    return OcrResponse(filename=filename, status=status, candidates=candidates)


def plan_ocr_candidates(document: DocumentModel) -> list[OcrCandidate]:
    candidates: list[OcrCandidate] = []
    for page in document.pages:
        page_bbox = BBox(x0=0, y0=0, x1=page.width, y1=page.height)
        native_chars = sum(len(block.normalized_text) for block in _blocks(page))
        if page.page_type == "scanned" or native_chars < 25:
            candidates.append(
                OcrCandidate(
                    page_number=page.page_number,
                    source="full_page",
                    reasons=["scanned_page" if page.page_type == "scanned" else "low_native_text_coverage"],
                    bbox=page_bbox,
                    native_text=None,
                )
            )
            continue

        for block in _blocks(page):
            reasons: list[OcrReason] = []
            if "suspicious_text_low_confidence" in block.warnings:
                reasons.append("suspicious_glyph")
            if "font_encoding_anomaly" in block.warnings:
                reasons.append("font_encoding_anomaly")
            if not reasons:
                continue
            candidates.append(
                OcrCandidate(
                    page_number=page.page_number,
                    source="suspicious_block",
                    reasons=reasons,
                    bbox=_expand_bbox(block.bbox, page_bbox, margin=6),
                    native_text=block.original_text,
                    linked_block_reading_order=block.reading_order,
                    linked_heading=_nearest_heading(page, block),
                )
            )

        for image in _image_text_candidates(page):
            candidates.append(
                OcrCandidate(
                    page_number=page.page_number,
                    source="image_region",
                    reasons=["image_text_candidate"],
                    bbox=image.bbox or page_bbox,
                    native_text=None,
                    linked_heading=_nearest_heading_for_bbox(page, image.bbox),
                )
            )
    return candidates


def render_bbox_to_png(page: fitz.Page, bbox: BBox, output_dir: Path, dpi: int) -> tuple[Path, float]:
    safe_dpi = min(MAX_DPI, max(MIN_DPI, dpi))
    scale = safe_dpi / 72.0
    clip = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1) & page.rect
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
    if pixmap.width > MAX_IMAGE_SIDE_PX or pixmap.height > MAX_IMAGE_SIDE_PX:
        scale = min(MAX_IMAGE_SIDE_PX / pixmap.width, MAX_IMAGE_SIDE_PX / pixmap.height) * scale
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    output_path = output_dir / f"ocr-{uuid.uuid4().hex}.png"
    pixmap.save(output_path)
    return output_path, scale


def map_pixel_bbox_to_pdf_bbox(pixel_bbox: tuple[float, float, float, float], clip_bbox: BBox, scale: float, page_bbox: BBox) -> BBox:
    x0, y0, x1, y1 = pixel_bbox
    return BBox(
        x0=_clamp(clip_bbox.x0 + x0 / scale, page_bbox.x0, page_bbox.x1),
        y0=_clamp(clip_bbox.y0 + y0 / scale, page_bbox.y0, page_bbox.y1),
        x1=_clamp(clip_bbox.x0 + x1 / scale, page_bbox.x0, page_bbox.x1),
        y1=_clamp(clip_bbox.y0 + y1 / scale, page_bbox.y0, page_bbox.y1),
    )


def _parse_tesseract_tsv(tsv: str, *, bbox: BBox, dpi: int, page_bbox: BBox) -> list[OcrWord]:
    lines = [line for line in tsv.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    indices = {name: header.index(name) for name in header if name in {"level", "left", "top", "width", "height", "conf", "text"}}
    scale = dpi / 72.0
    words: list[OcrWord] = []
    for row in lines[1:]:
        parts = row.split("\t")
        try:
            text = parts[indices["text"]].strip()
            confidence = float(parts[indices["conf"]])
        except (KeyError, IndexError, ValueError):
            continue
        if not text or confidence < 0:
            continue
        left = float(parts[indices["left"]])
        top = float(parts[indices["top"]])
        width = float(parts[indices["width"]])
        height = float(parts[indices["height"]])
        words.append(
            OcrWord(
                text=text,
                confidence=confidence,
                bbox=map_pixel_bbox_to_pdf_bbox((left, top, left + width, top + height), bbox, scale, page_bbox),
            )
        )
    return words


def _run_version(executable: str, timeout_seconds: int) -> str | None:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except Exception:
        return None
    return completed.stdout.splitlines()[0] if completed.stdout else None


def _run_languages(executable: str, timeout_seconds: int) -> list[str]:
    try:
        completed = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except Exception:
        return []
    return [line.strip() for line in completed.stdout.splitlines()[1:] if line.strip()]


def _average_confidence(words: list[OcrWord]) -> float:
    if not words:
        return 0
    return sum(word.confidence for word in words) / len(words)


def _requires_review(candidate: OcrCandidate) -> bool:
    return not _can_apply_automatically(candidate)


def _can_apply_automatically(candidate: OcrCandidate) -> bool:
    if not candidate.result or candidate.result.confidence < 88:
        return False
    if candidate.source != "suspicious_block":
        return False
    if _changes_numbers_or_units(candidate.native_text or "", candidate.result.text):
        return False
    return bool(candidate.result.text)


def _changes_numbers_or_units(native_text: str, ocr_text: str) -> bool:
    units = ("°C", "%", "MPa", "RAL", "CO₂", "ml", "L", "months")
    native_tokens = {token for token in native_text.split() if any(char.isdigit() for char in token) or token in units}
    ocr_tokens = {token for token in ocr_text.split() if any(char.isdigit() for char in token) or token in units}
    return native_tokens != ocr_tokens


def _blocks(page: DocumentPage) -> list[DocumentBlock]:
    return [block for region in page.regions for block in region.blocks]


def _nearest_heading(page: DocumentPage, block: DocumentBlock) -> str | None:
    return _nearest_heading_for_bbox(page, block.bbox)


def _nearest_heading_for_bbox(page: DocumentPage, bbox: BBox | None) -> str | None:
    if bbox is None:
        return None
    headings = [block for block in _blocks(page) if block.kind == "heading"]
    if not headings:
        return None
    nearest = min(headings, key=lambda heading: abs(heading.bbox.y0 - bbox.y0) + abs(heading.bbox.x0 - bbox.x0))
    return nearest.normalized_text


def _image_text_candidates(page: DocumentPage) -> list[DocumentImage]:
    candidates: list[DocumentImage] = []
    for image in page.images:
        if not image.bbox:
            continue
        width = image.bbox.x1 - image.bbox.x0
        height = image.bbox.y1 - image.bbox.y0
        if width < 16 or height < 16:
            continue
        if width > page.width * 0.75 or height > page.height * 0.25:
            continue
        linked_heading = _nearest_heading_for_bbox(page, image.bbox)
        if linked_heading in {"Storage", "Shelf life"}:
            candidates.append(image)
    return candidates[:4]


def _expand_bbox(bbox: BBox, page_bbox: BBox, margin: float) -> BBox:
    return BBox(
        x0=_clamp(bbox.x0 - margin, page_bbox.x0, page_bbox.x1),
        y0=_clamp(bbox.y0 - margin, page_bbox.y0, page_bbox.y1),
        x1=_clamp(bbox.x1 + margin, page_bbox.x0, page_bbox.x1),
        y1=_clamp(bbox.y1 + margin, page_bbox.y0, page_bbox.y1),
    )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _safe_rmtree(target: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise RuntimeError("Refusing to remove a path outside the OCR temp root.")
    shutil.rmtree(resolved_target, ignore_errors=True)
