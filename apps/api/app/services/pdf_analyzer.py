import hashlib
from pathlib import Path

import pymupdf as fitz

from app.models import PageAnalysis, PageDimensions, PdfAnalysis, PdfKind, TextBlock
from app.services.document_segmenter import DOCUMENT_MODEL_VERSION, segment_pdf

TEXT_PAGE_THRESHOLD = 25


def analyze_pdf(path: Path, filename: str | None = None) -> PdfAnalysis:
    source_name = filename or path.name
    pages: list[PageAnalysis] = []
    document_model = segment_pdf(path)

    with fitz.open(path) as document:
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            text_blocks = _extract_text_blocks(page, page_number)
            text_chars = sum(len(block.text.strip()) for block in text_blocks)
            image_count = len(page.get_images(full=True))
            page_kind = _classify_page(text_chars=text_chars, image_count=image_count)
            rect = page.rect

            pages.append(
                PageAnalysis(
                    page_number=page_number,
                    kind=page_kind,
                    extracted_text_chars=text_chars,
                    dimensions=PageDimensions(width=rect.width, height=rect.height),
                    rotation=page.rotation,
                    text_blocks=text_blocks,
                    image_count=image_count,
                )
            )

    total_text_chars = sum(page.extracted_text_chars for page in pages)
    return PdfAnalysis(
        filename=source_name,
        sha256=_sha256(path),
        page_count=len(pages),
        document_model_version=DOCUMENT_MODEL_VERSION,
        kind=_classify_document([page.kind for page in pages]),
        extracted_text_chars=total_text_chars,
        pages=pages,
        document=document_model,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_text_blocks(page: fitz.Page, page_number: int) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for block in page.get_text("blocks"):
        x0, y0, x1, y1, text, block_number, block_type = block[:7]
        if block_type != 0:
            continue

        cleaned_text = " ".join(str(text).split())
        if not cleaned_text:
            continue

        blocks.append(
            TextBlock(
                page_number=page_number,
                block_number=int(block_number),
                text=cleaned_text,
                x0=float(x0),
                y0=float(y0),
                x1=float(x1),
                y1=float(y1),
            )
        )
    return blocks


def _classify_page(text_chars: int, image_count: int) -> PdfKind:
    if text_chars >= TEXT_PAGE_THRESHOLD:
        return "digital"
    if image_count > 0:
        return "scanned"
    return "empty"


def _classify_document(page_kinds: list[PdfKind]) -> PdfKind:
    meaningful = [kind for kind in page_kinds if kind != "empty"]
    if not meaningful:
        return "empty"

    unique = set(meaningful)
    if unique == {"digital"}:
        return "digital"
    if unique == {"scanned"}:
        return "scanned"
    return "mixed"
