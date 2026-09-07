from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import pymupdf as fitz

from app.models import (
    BBox,
    BlockKind,
    DocumentBlock,
    DocumentImage,
    DocumentLine,
    DocumentModel,
    DocumentPage,
    DocumentRegion,
    DocumentSpan,
    LineBoundary,
    PdfKind,
    RegionKind,
    SpanStyle,
)

TEXT_PAGE_THRESHOLD = 25

LIST_MARKER_RE = re.compile(r"^\s*(?:[•\-\*\u2013\u2014]|\d+[\.)]|[A-Za-z][\.)])\s+")
PAGE_NUMBER_RE = re.compile(r"^\s*(?:page\s*)?\d+\s*(?:/\s*\d+)?\s*$", re.IGNORECASE)
SUSPICIOUS_RE = re.compile(r"\bq[A-Z]{4,}\b")
PRESERVE_HYPHENATED_TERMS = {
    "one-component",
    "water-based",
    "shrink-free",
}


@dataclass
class RawLine:
    page_number: int
    bbox: BBox
    original_text: str
    spans: list[DocumentSpan]
    region_kind: RegionKind
    column_index: int = 0

    @property
    def x0(self) -> float:
        return self.bbox.x0

    @property
    def y0(self) -> float:
        return self.bbox.y0

    @property
    def x1(self) -> float:
        return self.bbox.x1

    @property
    def y1(self) -> float:
        return self.bbox.y1


@dataclass
class RawTable:
    bbox: BBox
    cells: list[list[str]]


@dataclass
class LayoutRegion:
    kind: RegionKind
    bbox: BBox
    lines: list[RawLine]
    tables: list[RawTable]


def segment_pdf(path: Path) -> DocumentModel:
    pages: list[DocumentPage] = []
    repeated_headers: Counter[str] = Counter()
    repeated_footers: Counter[str] = Counter()

    with fitz.open(path) as document:
        metadata = dict(document.metadata or {})
        metadata["pymupdf_version"] = fitz.VersionBind
        metadata["table_detection"] = "pymupdf_find_tables" if hasattr(fitz.Page, "find_tables") else "none"

        extracted_pages: list[tuple[fitz.Rect, list[RawLine], list[RawTable], list[DocumentImage], PdfKind]] = []
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            rect = page.rect
            lines = _prepare_lines(_extract_lines(page, page_number))
            tables = _extract_tables(page)
            images = _extract_images(page)
            text_chars = sum(len(line.original_text.strip()) for line in lines)
            page_kind = _classify_page(text_chars=text_chars, image_count=len(images))

            for line in lines:
                if line.y1 <= rect.height * 0.08:
                    repeated_headers[_fingerprint(line.original_text)] += 1
                if line.y0 >= rect.height * 0.92:
                    repeated_footers[_fingerprint(line.original_text)] += 1

            extracted_pages.append((rect, lines, tables, images, page_kind))

        header_texts = _repeated_texts(repeated_headers, len(extracted_pages))
        footer_texts = _repeated_texts(repeated_footers, len(extracted_pages))

        for page_index, (rect, lines, tables, images, page_kind) in enumerate(extracted_pages):
            pages.append(
                _build_page(
                    page_number=page_index + 1,
                    rect=rect,
                    lines=lines,
                    tables=tables,
                    images=images,
                    page_kind=page_kind,
                    repeated_headers=header_texts,
                    repeated_footers=footer_texts,
                )
            )

    return DocumentModel(pages=pages, metadata=metadata)


def _classify_page(text_chars: int, image_count: int) -> PdfKind:
    if text_chars >= TEXT_PAGE_THRESHOLD:
        return "digital"
    if image_count > 0:
        return "scanned"
    return "empty"


def normalize_lines(lines: list[str]) -> str:
    normalized, _ = normalize_lines_with_boundaries(lines)
    return normalized


def normalize_lines_with_boundaries(lines: list[str]) -> tuple[str, list[LineBoundary]]:
    normalized = ""
    boundaries: list[LineBoundary] = []
    previous_line_index: int | None = None

    for index, raw_line in enumerate(lines):
        line = _normalize_spaces(raw_line)
        if not line:
            continue
        if not normalized:
            normalized = line
            previous_line_index = index
            continue

        boundary = _line_boundary(normalized, line, previous_line_index or 0, index)
        boundaries.append(boundary)
        if boundary.kind == "soft_hyphen":
            normalized = normalized[:-1] + line.lstrip()
        elif boundary.kind == "visible_hyphen":
            normalized = normalized + line.lstrip()
        else:
            normalized = f"{normalized} {line}"
        previous_line_index = index

    return _normalize_spaces(normalized.replace("\u00ad", "")), boundaries


def _extract_lines(page: fitz.Page, page_number: int) -> list[RawLine]:
    payload = page.get_text("dict")
    lines: list[RawLine] = []
    for block in payload.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [_span_from_payload(span) for span in line.get("spans", []) if span.get("text")]
            original_text = "".join(span.text for span in spans)
            original_text = _normalize_spaces(original_text)
            if not original_text:
                continue
            bbox = _bbox(line.get("bbox") or _union_bbox([span.bbox for span in spans]))
            lines.append(
                RawLine(
                    page_number=page_number,
                    bbox=bbox,
                    original_text=original_text,
                    spans=spans,
                    region_kind="body",
                )
            )
    return lines


def _prepare_lines(lines: list[RawLine]) -> list[RawLine]:
    return _merge_marker_lines(_merge_same_row_fragments(lines))


def _merge_same_row_fragments(lines: list[RawLine]) -> list[RawLine]:
    remaining = sorted(lines, key=lambda line: (line.y0, line.x0))
    merged: list[RawLine] = []
    used: set[int] = set()

    for index, line in enumerate(remaining):
        if index in used:
            continue
        row = [line]
        used.add(index)
        changed = True
        while changed:
            changed = False
            current = row[-1]
            for candidate_index, candidate in enumerate(remaining):
                if candidate_index in used:
                    continue
                if _same_visual_row(current, candidate) and _can_merge_inline(current, candidate):
                    row.append(candidate)
                    used.add(candidate_index)
                    changed = True
                    break
        merged.append(_merge_lines(row) if len(row) > 1 else line)

    return sorted(merged, key=lambda line: (line.y0, line.x0))


def _merge_marker_lines(lines: list[RawLine]) -> list[RawLine]:
    ordered = sorted(lines, key=lambda line: (line.y0, line.x0))
    merged: list[RawLine] = []
    used: set[int] = set()

    for index, line in enumerate(ordered):
        if index in used:
            continue
        marker = line.original_text.strip()
        if marker not in {"-", "•", "*", "–", "—"}:
            merged.append(line)
            used.add(index)
            continue

        candidates = [
            (candidate_index, candidate)
            for candidate_index, candidate in enumerate(ordered)
            if candidate_index not in used
            and candidate_index != index
            and candidate.x0 > line.x0
            and _vertical_overlap(line.bbox, candidate.bbox) > 0.35
            and candidate.x0 - line.x1 < 35
        ]
        if not candidates:
            merged.append(line)
            used.add(index)
            continue

        candidate_index, candidate = min(candidates, key=lambda item: item[1].x0)
        merged.append(_merge_lines([line, candidate], separator=" "))
        used.update({index, candidate_index})

    return sorted(merged, key=lambda line: (line.y0, line.x0))


def _extract_tables(page: fitz.Page) -> list[RawTable]:
    if not hasattr(page, "find_tables"):
        return []
    try:
        found = page.find_tables()
    except Exception:
        return []

    tables = getattr(found, "tables", []) or []
    extracted: list[RawTable] = []
    for table in tables:
        bbox = getattr(table, "bbox", None)
        if not bbox:
            continue
        try:
            cells = table.extract()
        except Exception:
            cells = []
        cleaned_cells = _trim_empty_table_cells(
            [[_normalize_spaces(str(cell or "")) for cell in row] for row in cells]
        )
        extracted.append(RawTable(bbox=_bbox(bbox), cells=cleaned_cells))
    return extracted


def _extract_images(page: fitz.Page) -> list[DocumentImage]:
    images: list[DocumentImage] = []
    for image in page.get_images(full=True):
        xref = int(image[0])
        rects = page.get_image_rects(xref)
        bbox = _bbox(rects[0]) if rects else None
        images.append(
            DocumentImage(
                bbox=bbox,
                width=int(image[2]) if len(image) > 2 else None,
                height=int(image[3]) if len(image) > 3 else None,
                xref=xref,
            )
        )
    return images


def _build_page(
    page_number: int,
    rect: fitz.Rect,
    lines: list[RawLine],
    tables: list[RawTable],
    images: list[DocumentImage],
    page_kind: PdfKind,
    repeated_headers: set[str],
    repeated_footers: set[str],
) -> DocumentPage:
    height = float(rect.height)
    body_lines: list[RawLine] = []
    header_lines: list[RawLine] = []
    footer_lines: list[RawLine] = []

    for line in lines:
        fingerprint = _fingerprint(line.original_text)
        if line.y1 <= height * 0.08 or fingerprint in repeated_headers:
            line.region_kind = "header"
            header_lines.append(line)
        elif line.y0 >= height * 0.92 or fingerprint in repeated_footers or PAGE_NUMBER_RE.match(line.original_text):
            line.region_kind = "footer"
            footer_lines.append(line)
        elif _inside_any_table(line, tables):
            continue
        else:
            body_lines.append(line)

    layout_regions = _segment_body_regions(body_lines, tables, float(rect.width), height)
    regions: list[DocumentRegion] = []
    reading_order = 1

    for kind, region_lines in (("header", header_lines),):
        if region_lines:
            region, reading_order = _make_text_region(kind, region_lines, reading_order)
            regions.append(region)

    for layout_region in layout_regions:
        region, reading_order = _make_layout_region(layout_region, reading_order)
        regions.append(region)

    if footer_lines:
        region, reading_order = _make_text_region("footer", footer_lines, reading_order)
        regions.append(region)

    regions = sorted(regions, key=lambda region: region.reading_order)
    return DocumentPage(
        page_number=page_number,
        width=float(rect.width),
        height=height,
        page_type=page_kind,
        regions=regions,
        images=images,
    )


def _make_text_region(kind: RegionKind, lines: list[RawLine], start_order: int) -> tuple[DocumentRegion, int]:
    ordered_lines = sorted(lines, key=lambda line: (line.y0, line.x0))
    blocks = _group_lines_into_blocks(ordered_lines, start_order)
    next_order = start_order + len(blocks)
    return (
        DocumentRegion(
            kind=kind,
            bbox=_union_bbox([block.bbox for block in blocks]),
            reading_order=start_order,
            blocks=blocks,
        ),
        next_order,
    )


def _make_layout_region(layout_region: LayoutRegion, start_order: int) -> tuple[DocumentRegion, int]:
    items: list[tuple[float, float, str, Any]] = []
    for line in layout_region.lines:
        items.append((line.y0, line.x0, "line", line))
    for table in layout_region.tables:
        items.append((table.bbox.y0, table.bbox.x0, "table", table))

    blocks: list[DocumentBlock] = []
    order = start_order
    pending_lines: list[RawLine] = []

    def flush_pending_lines() -> None:
        nonlocal order, pending_lines
        if not pending_lines:
            return
        line_blocks = _group_lines_into_blocks(pending_lines, order)
        blocks.extend(line_blocks)
        order += len(line_blocks)
        pending_lines = []

    for _, _, kind, payload in sorted(items, key=lambda item: (_row_key(item[0]), item[1])):
        if kind == "table":
            flush_pending_lines()
            blocks.append(_make_table_block(payload, order))
            order += 1
        else:
            pending_lines.append(payload)
    flush_pending_lines()

    return (
        DocumentRegion(
            kind=layout_region.kind,
            bbox=_union_bbox([block.bbox for block in blocks]),
            reading_order=start_order,
            blocks=blocks,
        ),
        order,
    )


def _segment_body_regions(
    lines: list[RawLine],
    tables: list[RawTable],
    page_width: float,
    page_height: float,
) -> list[LayoutRegion]:
    items: list[tuple[BBox, RawLine | RawTable]] = [(line.bbox, line) for line in lines]
    items.extend((table.bbox, table) for table in tables)
    if not items:
        return []

    clusters = _cluster_x_regions([bbox for bbox, _ in items], page_width)
    if len(clusters) <= 1:
        return [LayoutRegion(kind="body", bbox=_union_bbox([bbox for bbox, _ in items]), lines=lines, tables=tables)]

    regions: list[LayoutRegion] = []
    for cluster_index, cluster in enumerate(clusters):
        x0, x1 = cluster
        cluster_lines = [line for line in lines if _center_x(line.bbox) >= x0 and _center_x(line.bbox) <= x1]
        cluster_tables = [table for table in tables if _center_x(table.bbox) >= x0 and _center_x(table.bbox) <= x1]
        if not cluster_lines and not cluster_tables:
            continue
        for line in cluster_lines:
            line.column_index = cluster_index
        boxes = [line.bbox for line in cluster_lines] + [table.bbox for table in cluster_tables]
        regions.append(LayoutRegion(kind="column", bbox=_union_bbox(boxes), lines=cluster_lines, tables=cluster_tables))

    if len(regions) <= 1:
        return [LayoutRegion(kind="body", bbox=_union_bbox([bbox for bbox, _ in items]), lines=lines, tables=tables)]

    return sorted(regions, key=lambda region: (region.bbox.x0, region.bbox.y0))


def _cluster_x_regions(boxes: list[BBox], page_width: float) -> list[tuple[float, float]]:
    intervals = sorted((box.x0, box.x1) for box in boxes if box.x1 > box.x0)
    if not intervals:
        return []

    gap_threshold = max(18.0, page_width * 0.035)
    clusters: list[tuple[float, float]] = []
    current_x0, current_x1 = intervals[0]
    for x0, x1 in intervals[1:]:
        gap = x0 - current_x1
        if gap >= gap_threshold:
            clusters.append((current_x0, current_x1))
            current_x0, current_x1 = x0, x1
        else:
            current_x1 = max(current_x1, x1)
    clusters.append((current_x0, current_x1))

    return [(max(0.0, x0 - 8.0), min(page_width, x1 + 8.0)) for x0, x1 in clusters]


def _group_lines_into_blocks(lines: list[RawLine], start_order: int) -> list[DocumentBlock]:
    if not lines:
        return []

    body_size = _body_font_size(lines)
    blocks: list[DocumentBlock] = []
    current: list[RawLine] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        blocks.append(_make_text_block(current, body_size, start_order + len(blocks)))
        current = []

    previous: RawLine | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        line_kind = _line_kind(line, body_size)
        if line_kind in {"heading", "list_item"}:
            flush()
            if line_kind == "list_item":
                item_lines = [line]
                next_index = index + 1
                while next_index < len(lines) and _is_list_continuation(line, item_lines[-1], lines[next_index], body_size):
                    item_lines.append(lines[next_index])
                    next_index += 1
                if len(item_lines) == 1:
                    blocks.extend(_make_list_item_blocks(line, body_size, start_order + len(blocks)))
                else:
                    blocks.append(
                        _make_text_block(item_lines, body_size, start_order + len(blocks), forced_kind="list_item")
                    )
                previous = item_lines[-1]
                index = next_index
                continue
            else:
                blocks.append(_make_text_block([line], body_size, start_order + len(blocks), forced_kind=line_kind))
            previous = line
            index += 1
            continue

        if previous is None:
            current.append(line)
            previous = line
            index += 1
            continue

        gap = line.y0 - previous.y1
        same_indent = abs(line.x0 - previous.x0) < 18
        if gap > max(14.0, _dominant_font_size(previous) * 1.35) or not same_indent:
            flush()
        current.append(line)
        previous = line
        index += 1

    flush()
    return blocks


def _make_text_block(
    lines: list[RawLine],
    body_size: float,
    reading_order: int,
    forced_kind: BlockKind | None = None,
) -> DocumentBlock:
    line_models = [
        DocumentLine(bbox=line.bbox, original_text=line.original_text, spans=line.spans)
        for line in lines
    ]
    original_text = "\n".join(line.original_text for line in lines)
    normalized_text, line_boundaries = normalize_lines_with_boundaries([line.original_text for line in lines])
    style = _style_from_lines(lines)
    kind = forced_kind or _block_kind(lines, body_size)
    warnings = _warnings_for_text(original_text)
    warnings.extend(boundary.warning for boundary in line_boundaries if boundary.warning)
    return DocumentBlock(
        kind=kind,
        bbox=_union_bbox([line.bbox for line in lines]),
        original_text=original_text,
        normalized_text=normalized_text,
        lines=line_models,
        line_boundaries=line_boundaries,
        style=style,
        reading_order=reading_order,
        confidence=0.4 if warnings else 1.0,
        warnings=warnings,
        needs_ocr_check=bool(warnings),
    )


def _make_list_item_blocks(line: RawLine, body_size: float, start_order: int) -> list[DocumentBlock]:
    parts = _split_inline_list_items(line.original_text)
    if len(parts) <= 1:
        return [_make_text_block([line], body_size, start_order, forced_kind="list_item")]

    blocks: list[DocumentBlock] = []
    for index, part in enumerate(parts):
        item_line = RawLine(
            page_number=line.page_number,
            bbox=line.bbox,
            original_text=part,
            spans=line.spans,
            region_kind=line.region_kind,
            column_index=line.column_index,
        )
        blocks.append(_make_text_block([item_line], body_size, start_order + index, forced_kind="list_item"))
    return blocks


def _make_table_block(table: RawTable, reading_order: int) -> DocumentBlock:
    text = "\n".join("\t".join(cell for cell in row) for row in table.cells)
    return DocumentBlock(
        kind="table",
        bbox=table.bbox,
        original_text=text,
        normalized_text=text,
        lines=[],
        style=SpanStyle(),
        reading_order=reading_order,
        table_cells=table.cells,
    )


def _assign_columns(lines: list[RawLine], page_width: float) -> list[list[RawLine]]:
    if not lines:
        return []
    sorted_by_x = sorted(lines, key=lambda line: line.x0)
    gaps: list[tuple[float, float]] = []
    for left, right in zip(sorted_by_x, sorted_by_x[1:]):
        gap = right.x0 - left.x0
        if gap > page_width * 0.18:
            gaps.append((gap, (left.x0 + right.x0) / 2))

    if not gaps:
        return [sorted(lines, key=lambda line: (line.y0, line.x0))]

    _, split_x = max(gaps, key=lambda item: item[0])
    left_column = [line for line in lines if (line.x0 + line.x1) / 2 <= split_x]
    right_column = [line for line in lines if (line.x0 + line.x1) / 2 > split_x]
    if len(left_column) < 2 or len(right_column) < 2:
        return [sorted(lines, key=lambda line: (line.y0, line.x0))]
    return [
        sorted(left_column, key=lambda line: (line.y0, line.x0)),
        sorted(right_column, key=lambda line: (line.y0, line.x0)),
    ]


def _merge_lines(lines: list[RawLine], separator: str = " ") -> RawLine:
    ordered = sorted(lines, key=lambda line: line.x0)
    text = ""
    spans: list[DocumentSpan] = []
    for line in ordered:
        if text:
            text = f"{text}{separator}{line.original_text}"
        else:
            text = line.original_text
        spans.extend(line.spans)
    return RawLine(
        page_number=ordered[0].page_number,
        bbox=_union_bbox([line.bbox for line in ordered]),
        original_text=_normalize_spaces(text),
        spans=spans,
        region_kind=ordered[0].region_kind,
        column_index=ordered[0].column_index,
    )


def _same_visual_row(left: RawLine, right: RawLine) -> bool:
    if right.x0 < left.x0:
        return False
    same_size = abs(_dominant_font_size(left) - _dominant_font_size(right)) <= 1.5
    return same_size and _vertical_overlap(left.bbox, right.bbox) > 0.45


def _can_merge_inline(left: RawLine, right: RawLine) -> bool:
    gap = right.x0 - left.x1
    if gap < -2 or gap > max(18.0, _dominant_font_size(left) * 2.4):
        return False
    if left.original_text.rstrip().endswith(":") and right.original_text[:1].isupper():
        return False
    if _looks_like_short_caption(left) and _looks_like_short_caption(right):
        return False
    return True


def _looks_like_short_caption(line: RawLine) -> bool:
    text = line.original_text.strip()
    return (
        len(text) <= 36
        and not text.endswith(".")
        and text[:1].isupper()
        and any(char.isalpha() for char in text)
    )


def _vertical_overlap(first: BBox, second: BBox) -> float:
    overlap = max(0.0, min(first.y1, second.y1) - max(first.y0, second.y0))
    shortest = max(1.0, min(first.y1 - first.y0, second.y1 - second.y0))
    return overlap / shortest


def _center_x(box: BBox) -> float:
    return (box.x0 + box.x1) / 2


def _row_key(y: float) -> int:
    return int(round(y / 4.0))


def _trim_empty_table_cells(rows: list[list[str]]) -> list[list[str]]:
    non_empty_rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not non_empty_rows:
        return []

    max_columns = max(len(row) for row in non_empty_rows)
    padded = [row + [""] * (max_columns - len(row)) for row in non_empty_rows]
    keep_columns = [
        column_index
        for column_index in range(max_columns)
        if any(row[column_index].strip() for row in padded)
    ]
    return [[row[column_index] for column_index in keep_columns] for row in padded]


def _split_inline_list_items(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped.startswith("- "):
        return [text]
    starts = [match.start() for match in re.finditer(r"(?<!\S)-\s+", stripped)]
    if len(starts) <= 1:
        return [text]
    parts: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(stripped)
        part = stripped[start:end].strip()
        if part:
            parts.append(part)
    return parts or [text]


def _line_kind(line: RawLine, body_size: float) -> BlockKind:
    if LIST_MARKER_RE.match(line.original_text):
        return "list_item"
    if _looks_like_heading(line, body_size):
        return "heading"
    return "paragraph"


def _block_kind(lines: list[RawLine], body_size: float) -> BlockKind:
    if len(lines) == 1:
        return _line_kind(lines[0], body_size)
    if all(LIST_MARKER_RE.match(line.original_text) for line in lines):
        return "list"
    return "paragraph"


def _is_list_continuation(first_line: RawLine, previous_line: RawLine, candidate: RawLine, body_size: float) -> bool:
    if _line_kind(candidate, body_size) in {"heading", "list_item"}:
        return False
    gap = candidate.y0 - previous_line.y1
    if gap > max(14.0, _dominant_font_size(previous_line) * 1.35):
        return False
    if abs(_dominant_font_size(candidate) - body_size) > 3.0:
        return False
    return candidate.x0 >= first_line.x0 - 3


def _looks_like_heading(line: RawLine, body_size: float) -> bool:
    text = line.original_text.strip()
    if len(text) < 3 or len(text) > 90 or text.endswith("."):
        return False
    font_size = _dominant_font_size(line)
    bold = any(span.bold for span in line.spans)
    upperish = text.upper() == text and any(char.isalpha() for char in text)
    return font_size >= body_size * 1.15 or bold or upperish


def _style_from_lines(lines: list[RawLine]) -> SpanStyle:
    spans = [span for line in lines for span in line.spans]
    if not spans:
        return SpanStyle()
    fonts = Counter(span.font for span in spans if span.font)
    sizes = [span.font_size for span in spans if span.font_size]
    colors = Counter(str(span.color) for span in spans if span.color is not None)
    flags = Counter(span.flags for span in spans if span.flags is not None)
    return SpanStyle(
        font=fonts.most_common(1)[0][0] if fonts else None,
        font_size=float(median(sizes)) if sizes else None,
        color=colors.most_common(1)[0][0] if colors else None,
        flags=flags.most_common(1)[0][0] if flags else None,
        bold=any(span.bold for span in spans),
        italic=any(span.italic for span in spans),
    )


def _body_font_size(lines: list[RawLine]) -> float:
    sizes = [span.font_size for line in lines for span in line.spans if span.font_size]
    return float(median(sizes)) if sizes else 12.0


def _dominant_font_size(line: RawLine) -> float:
    sizes = [span.font_size for span in line.spans if span.font_size]
    return float(median(sizes)) if sizes else 12.0


def _span_from_payload(span: dict[str, Any]) -> DocumentSpan:
    font = span.get("font")
    flags = int(span.get("flags") or 0)
    return DocumentSpan(
        text=str(span.get("text") or ""),
        bbox=_bbox(span.get("bbox")),
        font=str(font) if font is not None else None,
        font_size=float(span.get("size")) if span.get("size") is not None else None,
        color=span.get("color"),
        flags=flags,
        bold=_is_bold(str(font or ""), flags),
        italic=_is_italic(str(font or ""), flags),
    )


def _bbox(value: Any) -> BBox:
    rect = fitz.Rect(value)
    return BBox(x0=float(rect.x0), y0=float(rect.y0), x1=float(rect.x1), y1=float(rect.y1))


def _union_bbox(boxes: list[BBox]) -> BBox:
    if not boxes:
        return BBox(x0=0, y0=0, x1=0, y1=0)
    return BBox(
        x0=min(box.x0 for box in boxes),
        y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes),
        y1=max(box.y1 for box in boxes),
    )


def _inside_any_table(line: RawLine, tables: list[RawTable]) -> bool:
    center_x = (line.x0 + line.x1) / 2
    center_y = (line.y0 + line.y1) / 2
    return any(table.bbox.x0 <= center_x <= table.bbox.x1 and table.bbox.y0 <= center_y <= table.bbox.y1 for table in tables)


def _line_boundary(previous_text: str, next_line: str, previous_index: int, next_index: int) -> LineBoundary:
    if previous_text.endswith("\u00ad"):
        return LineBoundary(
            before_line_index=previous_index,
            after_line_index=next_index,
            kind="soft_hyphen",
        )
    if previous_text.endswith("-"):
        return LineBoundary(
            before_line_index=previous_index,
            after_line_index=next_index,
            kind="visible_hyphen",
            warning="hyphenation_ambiguous",
        )
    return LineBoundary(
        before_line_index=previous_index,
        after_line_index=next_index,
        kind="space",
    )


def _warnings_for_text(text: str) -> list[str]:
    warnings: list[str] = []
    if SUSPICIOUS_RE.search(text):
        warnings.append("suspicious_text_low_confidence")
    return warnings


def _fingerprint(text: str) -> str:
    return _normalize_spaces(text).lower()


def _repeated_texts(counter: Counter[str], page_count: int) -> set[str]:
    if page_count < 2:
        return set()
    threshold = max(2, page_count // 2)
    return {text for text, count in counter.items() if text and count >= threshold}


def _normalize_spaces(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _is_bold(font: str, flags: int) -> bool:
    name = font.lower()
    return bool(flags & 16) or "bold" in name or "black" in name or "heavy" in name


def _is_italic(font: str, flags: int) -> bool:
    name = font.lower()
    return bool(flags & 2) or "italic" in name or "oblique" in name
