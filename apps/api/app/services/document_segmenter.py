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
    TableCellProvenance,
    TableProvenance,
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
    rotated: bool = False

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
    page_number: int
    bbox: BBox
    cells: list[list[str]]
    table_id: str = ""
    cell_provenance: list[list[TableCellProvenance]] | None = None
    geometry_evidence: str = "open_table"
    warnings: list[str] | None = None
    order_span: int = 1
    derived_from_aligned_rows: bool = False


MIN_GRIDLESS_TABLE_ROWS = 5


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

        extracted_pages: list[tuple[fitz.Rect, int, list[RawLine], list[RawTable], list[DocumentImage], PdfKind]] = []
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            rect = page.rect
            extracted_lines = _extract_lines(page, page_number)
            tables = _extract_tables(page, extracted_lines, page_number)
            tables.extend(_extract_gridless_tables(page, extracted_lines, tables, float(rect.height), page_number))
            for table_index, table in enumerate(sorted(tables, key=lambda item: (item.bbox.y0, item.bbox.x0))):
                table.table_id = f"p{page_number}-t{table_index + 1}"
            lines = _prepare_lines(extracted_lines)
            images = _extract_images(page)
            text_chars = sum(len(line.original_text.strip()) for line in lines)
            page_kind = _classify_page(text_chars=text_chars, image_count=len(images))

            for line in lines:
                if line.y1 <= rect.height * 0.08:
                    repeated_headers[_fingerprint(line.original_text)] += 1
                if line.y0 >= rect.height * 0.92:
                    repeated_footers[_fingerprint(line.original_text)] += 1

            extracted_pages.append((rect, page.rotation, lines, tables, images, page_kind))

        _classify_image_relevance(extracted_pages)

        header_texts = _repeated_texts(repeated_headers, len(extracted_pages))
        footer_texts = _repeated_texts(repeated_footers, len(extracted_pages))

        for page_index, (rect, rotation, lines, tables, images, page_kind) in enumerate(extracted_pages):
            pages.append(
                _build_page(
                    page_number=page_index + 1,
                    rect=rect,
                    rotation=rotation,
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
    for block_index, block in enumerate(payload.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            spans = [
                _span_from_payload(span, f"p{page_number}-b{block_index + 1}-l{line_index + 1}-s{span_index + 1}")
                for span_index, span in enumerate(line.get("spans", [])) if span.get("text")
            ]
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
                    rotated=tuple(round(float(value), 4) for value in line.get("dir", (1.0, 0.0))) != (1.0, 0.0),
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


def _extract_tables(page: fitz.Page, lines: list[RawLine], page_number: int) -> list[RawTable]:
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
        normalized_cells = _preserve_table_cells(
            [[_normalize_spaces(str(cell or "")) for cell in row] for row in cells]
        )
        table_rect = fitz.Rect(bbox)
        rows = list(getattr(table, "rows", []) or [])
        cleaned_cells, source_row_indexes, source_column_indexes = _logical_table_cells(normalized_cells, rows)
        x_edges = _logical_column_edges(rows, source_row_indexes, source_column_indexes)
        y_edges = _logical_row_edges(rows, source_row_indexes)
        provenance: list[list[TableCellProvenance]] = []
        for row_index, row in enumerate(cleaned_cells):
            source_row_index = source_row_indexes[row_index]
            geometry_row = list(getattr(rows[source_row_index], "cells", []) or []) if source_row_index < len(rows) else []
            provenance_row: list[TableCellProvenance] = []
            for column_index, text in enumerate(row):
                source_column_index = source_column_indexes[column_index]
                value = geometry_row[source_column_index] if source_column_index < len(geometry_row) else None
                cell_rect = fitz.Rect(value) if value else None
                row_span = _covered_intervals(cell_rect.y0, cell_rect.y1, y_edges) if cell_rect else 1
                column_span = _covered_intervals(cell_rect.x0, cell_rect.x1, x_edges) if cell_rect else 1
                provenance_row.append(_cell_provenance(
                    page,
                    lines,
                    page_number,
                    row_index,
                    column_index,
                    text,
                    cell_rect,
                    "merged_cell" if row_span > 1 or column_span > 1 else "vector_grid",
                    row_span=row_span,
                    column_span=column_span,
                ))
            provenance.append(provenance_row)
        extracted.append(RawTable(
            page_number=page_number,
            bbox=_bbox(table_rect),
            cells=cleaned_cells,
            cell_provenance=provenance,
            geometry_evidence="vector_grid",
            warnings=[],
        ))
    return extracted


def _extract_gridless_tables(
    page: fitz.Page,
    lines: list[RawLine],
    existing: list[RawTable],
    page_height: float,
    page_number: int,
) -> list[RawTable]:
    """Detect repeated aligned rows without relying on table ruling lines."""
    if len(lines) < 6:
        return []
    rows = _cluster_visual_rows(lines)
    anchors = _repeated_column_anchors(rows)
    if len(anchors) < 2:
        return []
    candidate_rows = [
        row for row in rows
        if len(_row_anchor_indexes(row, anchors)) >= 2
        and min(line.y0 for line in row) < page_height * 0.9
    ]
    groups = _contiguous_row_groups(candidate_rows)
    tables: list[RawTable] = []
    for group in groups:
        first_center = median([(line.y0 + line.y1) / 2 for line in group[0]])
        last_center = median([(line.y0 + line.y1) / 2 for line in group[-1]])
        group = [
            row for row in rows
            if first_center <= median([(line.y0 + line.y1) / 2 for line in row]) <= last_center
            and min(line.y0 for line in row) < page_height * 0.9
        ]
        if len(group) < MIN_GRIDLESS_TABLE_ROWS:
            continue
        group_anchors = [anchor for anchor in anchors if sum(
            any(abs(line.x0 - anchor) <= 5.0 for line in row) for row in group
        ) >= 2]
        if len(group_anchors) < 2:
            continue
        if sum(len(_row_anchor_indexes(row, group_anchors)) >= 2 for row in group) < 3:
            continue
        bbox = _union_bbox([line.bbox for row in group for line in row])
        if any(_bbox_overlap_ratio(bbox, table.bbox) > 0.5 for table in existing):
            continue
        body_sizes = [
            _dominant_font_size(line) for row in group for line in row
            if len(line.original_text.strip()) > 1
        ]
        body_size = median(body_sizes) if body_sizes else 0.0
        cells: list[list[str]] = []
        assigned_rows: list[list[list[RawLine]]] = []
        for row in group:
            row_cells: list[list[str]] = [[] for _ in group_anchors]
            row_lines: list[list[RawLine]] = [[] for _ in group_anchors]
            for line in sorted(row, key=lambda item: item.x0):
                column = min(range(len(group_anchors)), key=lambda index: abs(line.x0 - group_anchors[index]))
                text = line.original_text.strip()
                if len(text) <= 2 and body_size and _dominant_font_size(line) > body_size * 1.8:
                    text = ""
                if text:
                    row_cells[column].append(text)
                    row_lines[column].append(line)
            cells.append([_normalize_spaces(" ".join(parts)) for parts in row_cells])
            assigned_rows.append(row_lines)
        if len(group_anchors) == 2 and any(not any(row[index] for row in cells) for index in range(2)):
            continue
        populated_columns = [index for index in range(len(group_anchors)) if any(row[index] for row in cells)]
        if len(populated_columns) >= 2 and len(populated_columns) < len(group_anchors):
            cells = [[row[index] for index in populated_columns] for row in cells]
            assigned_rows = [[row[index] for index in populated_columns] for row in assigned_rows]
            group_anchors = [group_anchors[index] for index in populated_columns]
        occupancy = sum(bool(cell) for row in cells for cell in row) / max(1, len(cells) * len(cells[0]))
        if occupancy < 2 / 3:
            continue
        provenance = _aligned_grid_provenance(page, page_number, bbox, group_anchors, assigned_rows, cells)
        tables.append(RawTable(
            page_number=page_number,
            bbox=bbox,
            cells=cells,
            cell_provenance=provenance,
            geometry_evidence="aligned_grid",
            warnings=[],
            derived_from_aligned_rows=True,
        ))
    return tables


def _aligned_grid_provenance(
    page: fitz.Page,
    page_number: int,
    table_bbox: BBox,
    anchors: list[float],
    assigned_rows: list[list[list[RawLine]]],
    cells: list[list[str]],
) -> list[list[TableCellProvenance]]:
    all_lines = [line for row in assigned_rows for column in row for line in column]
    if not assigned_rows or not anchors or not all_lines:
        return []

    left_edges = [min(line.x0 for line in all_lines) - 1.0]
    column_boundaries_valid: list[bool] = []
    for column in range(len(anchors) - 1):
        left = [line.x1 for row in assigned_rows for line in row[column]]
        right = [line.x0 for row in assigned_rows for line in row[column + 1]]
        gap_left = max(left, default=anchors[column])
        gap_right = min(right, default=anchors[column + 1])
        column_boundaries_valid.append(gap_right - gap_left > 0.5)
        left_edges.append((gap_left + gap_right) / 2 if gap_right > gap_left else (anchors[column] + anchors[column + 1]) / 2)
    left_edges.append(max(line.x1 for line in all_lines) + 1.0)

    row_lines = [[line for column in row for line in column] for row in assigned_rows]
    top_edges = [min(line.y0 for line in all_lines) - 1.0]
    row_boundaries_valid: list[bool] = []
    for row_index in range(len(row_lines) - 1):
        upper_bottom = max((line.y1 for line in row_lines[row_index]), default=top_edges[-1])
        lower_top = min((line.y0 for line in row_lines[row_index + 1]), default=upper_bottom)
        row_boundaries_valid.append(lower_top - upper_bottom > 0.25)
        top_edges.append((upper_bottom + lower_top) / 2)
    top_edges.append(max(line.y1 for line in all_lines) + 1.0)

    output: list[list[TableCellProvenance]] = []
    for row_index, row in enumerate(cells):
        output_row: list[TableCellProvenance] = []
        for column_index, text in enumerate(row):
            rect = fitz.Rect(
                left_edges[column_index],
                top_edges[row_index],
                left_edges[column_index + 1],
                top_edges[row_index + 1],
            )
            lines = assigned_rows[row_index][column_index]
            source_spans = [span for line in lines for span in line.spans]
            crosses_boundary = any(not _rect_contains_bbox(rect, span.bbox, 0.15) for span in source_spans)
            boundary_valid = (
                (column_index == 0 or column_boundaries_valid[column_index - 1])
                and (column_index == len(row) - 1 or column_boundaries_valid[column_index])
                and (row_index == 0 or row_boundaries_valid[row_index - 1])
                and (row_index == len(cells) - 1 or row_boundaries_valid[row_index])
            )
            graphic_overlap = _has_cell_graphic_overlap(page, rect)
            rotated = any(line.rotated for line in lines)
            if not text and not source_spans:
                ownership = "empty"
                block_reason = None
            elif not boundary_valid:
                ownership = "ambiguous"
                block_reason = "ambiguous_cell_boundary"
            elif crosses_boundary:
                ownership = "ambiguous"
                block_reason = "span_crosses_cell_boundary"
            elif rotated:
                ownership = "unsupported"
                block_reason = "rotated_text"
            elif graphic_overlap:
                ownership = "unsupported"
                block_reason = "graphic_overlap"
            elif source_spans:
                ownership = "owned"
                block_reason = None
            else:
                ownership = "ambiguous"
                block_reason = "missing_source_spans"
            content = _inset_bbox(rect, 0.5)
            source_bbox = _union_bbox([span.bbox for span in source_spans]) if source_spans else None
            output_row.append(TableCellProvenance(
                cell_id=f"p{page_number}-pending-r{row_index + 1}-c{column_index + 1}",
                row_index=row_index,
                column_index=column_index,
                cell_bbox=_bbox(rect),
                source_bbox=source_bbox,
                content_bbox=_bbox(content) if content else None,
                source_span_ids=[span.span_id for span in source_spans],
                geometry_evidence="aligned_grid",
                proven_edges=["top", "right", "bottom", "left"] if boundary_valid else [],
                ownership=ownership,
                block_reason=block_reason,
                has_graphic_overlap=graphic_overlap,
                rotated_text=rotated,
                background_fill=_cell_background_fill(page, rect),
            ))
        output.append(output_row)
    return output


def _cell_provenance(
    page: fitz.Page,
    lines: list[RawLine],
    page_number: int,
    row_index: int,
    column_index: int,
    text: str,
    cell_rect: fitz.Rect | None,
    evidence: str,
    *,
    row_span: int = 1,
    column_span: int = 1,
) -> TableCellProvenance:
    if cell_rect is None:
        return TableCellProvenance(
            cell_id=f"p{page_number}-pending-r{row_index + 1}-c{column_index + 1}",
            row_index=row_index,
            column_index=column_index,
            row_span=row_span,
            column_span=column_span,
            geometry_evidence="partial_grid",
            ownership="empty" if not text else "unsupported",
            block_reason=None if not text else "missing_cell_bbox",
        )
    owned_lines = [
        line for line in lines
        if cell_rect.contains(((line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2))
    ]
    spans = [span for line in owned_lines for span in line.spans]
    span_bbox = _union_bbox([span.bbox for span in spans]) if spans else None
    refined_rect = _text_enclosing_fill_rect(page, cell_rect, span_bbox)
    proof_rect = refined_rect or cell_rect
    proven_edges = _proven_vector_edges(page, proof_rect)
    crosses_boundary = any(not _rect_contains_bbox(cell_rect, span.bbox, 0.15) for span in spans)
    rotated = any(line.rotated for line in owned_lines)
    graphic_overlap = _has_cell_graphic_overlap(page, cell_rect)
    if row_span > 1 or column_span > 1:
        ownership = "unsupported"
        block_reason = "merged_cell"
    elif len(proven_edges) != 4:
        ownership = "ambiguous"
        block_reason = "missing_cell_edge"
    elif not text and not spans:
        ownership = "empty"
        block_reason = None
    elif crosses_boundary:
        ownership = "ambiguous"
        block_reason = "span_crosses_cell_boundary"
    elif rotated:
        ownership = "unsupported"
        block_reason = "rotated_text"
    elif graphic_overlap:
        ownership = "unsupported"
        block_reason = "graphic_overlap"
    elif spans:
        ownership = "owned"
        block_reason = None
    else:
        ownership = "ambiguous"
        block_reason = "missing_source_spans"
    content = _inset_bbox(cell_rect, 0.5)
    source_bbox = span_bbox
    return TableCellProvenance(
        cell_id=f"p{page_number}-pending-r{row_index + 1}-c{column_index + 1}",
        row_index=row_index,
        column_index=column_index,
        row_span=row_span,
        column_span=column_span,
        cell_bbox=_bbox(cell_rect),
        source_bbox=source_bbox,
        content_bbox=_bbox(content) if content else None,
        source_span_ids=[span.span_id for span in spans],
        geometry_evidence="partial_grid" if len(proven_edges) != 4 and evidence == "vector_grid" else evidence,
        proven_edges=proven_edges,
        ownership=ownership,
        block_reason=block_reason,
        has_graphic_overlap=graphic_overlap,
        rotated_text=rotated,
        background_fill=_cell_background_fill(page, cell_rect),
    )


def _rect_contains_bbox(rect: fitz.Rect, bbox: BBox, tolerance: float) -> bool:
    return (
        bbox.x0 >= rect.x0 - tolerance
        and bbox.y0 >= rect.y0 - tolerance
        and bbox.x1 <= rect.x1 + tolerance
        and bbox.y1 <= rect.y1 + tolerance
    )


def _inset_bbox(rect: fitz.Rect, amount: float) -> fitz.Rect | None:
    inset = fitz.Rect(rect.x0 + amount, rect.y0 + amount, rect.x1 - amount, rect.y1 - amount)
    return inset if not inset.is_empty else None


def _has_cell_graphic_overlap(page: fitz.Page, rect: fitz.Rect) -> bool:
    inner = _inset_bbox(rect, 0.75) or rect
    drawings = page.get_drawings()
    for image in page.get_image_info(xrefs=True):
        if (fitz.Rect(image["bbox"]) & inner).is_empty:
            continue
        image_sequence = int(image.get("seqno", -1))
        covered = any(
            drawing.get("fill") is not None
            and int(drawing.get("seqno", -1)) > image_sequence
            and ((fitz.Rect(drawing["rect"]) & inner).get_area() / max(1.0, inner.get_area())) >= 0.9
            for drawing in drawings
        )
        if not covered:
            return True
    for drawing in drawings:
        fill = drawing.get("fill")
        drawing_rect = fitz.Rect(drawing["rect"])
        intersection = drawing_rect & inner
        if fill is not None and not intersection.is_empty and intersection.get_area() > 1.0:
            coverage = intersection.get_area() / max(1.0, inner.get_area())
            if coverage < 0.9:
                return True
    return False


def _cell_background_fill(page: fitz.Page, rect: fitz.Rect) -> str | None:
    inner = _inset_bbox(rect, 0.75) or rect
    candidates: list[tuple[float, tuple[float, ...]]] = []
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        if fill is None:
            continue
        intersection = fitz.Rect(drawing["rect"]) & inner
        coverage = intersection.get_area() / max(1.0, inner.get_area())
        if coverage >= 0.9:
            candidates.append((fitz.Rect(drawing["rect"]).get_area(), fill))
    if not candidates:
        return None
    _area, fill = min(candidates, key=lambda item: item[0])
    return "#" + "".join(f"{round(max(0, min(1, value)) * 255):02x}" for value in fill[:3])


def _proven_vector_edges(page: fitz.Page, rect: fitz.Rect) -> list[str]:
    segments: list[tuple[fitz.Point, fitz.Point]] = []
    for drawing in page.get_drawings():
        if drawing.get("color") is None and drawing.get("fill") is None:
            continue
        for item in drawing.get("items", []):
            if item[0] == "l" and drawing.get("color") is not None:
                segments.append((fitz.Point(item[1]), fitz.Point(item[2])))
            elif item[0] == "re":
                box = fitz.Rect(item[1])
                segments.extend([
                    (box.tl, box.tr), (box.tr, box.br),
                    (box.br, box.bl), (box.bl, box.tl),
                ])

    tolerance = 0.75

    def horizontal(y: float, start: float, end: float) -> bool:
        return any(
            abs(first.y - second.y) <= tolerance
            and abs(first.y - y) <= tolerance
            and min(first.x, second.x) <= start + tolerance
            and max(first.x, second.x) >= end - tolerance
            for first, second in segments
        )

    def vertical(x: float, start: float, end: float) -> bool:
        return any(
            abs(first.x - second.x) <= tolerance
            and abs(first.x - x) <= tolerance
            and min(first.y, second.y) <= start + tolerance
            and max(first.y, second.y) >= end - tolerance
            for first, second in segments
        )

    checks = [
        ("top", horizontal(rect.y0, rect.x0, rect.x1)),
        ("right", vertical(rect.x1, rect.y0, rect.y1)),
        ("bottom", horizontal(rect.y1, rect.x0, rect.x1)),
        ("left", vertical(rect.x0, rect.y0, rect.y1)),
    ]
    return [name for name, present in checks if present]


def _text_enclosing_fill_rect(page: fitz.Page, cell_rect: fitz.Rect, source_bbox: BBox | None) -> fitz.Rect | None:
    if source_bbox is None:
        return None
    source = _rect_from_bbox(source_bbox)
    candidates: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        if drawing.get("fill") is None:
            continue
        for item in drawing.get("items", []):
            if item[0] != "re":
                continue
            rect = fitz.Rect(item[1])
            ratio = rect.get_area() / max(1.0, cell_rect.get_area())
            if 0.5 <= ratio <= 1.2 and rect.contains(source) and not (rect & cell_rect).is_empty:
                candidates.append(rect)
    return min(candidates, key=lambda rect: rect.get_area(), default=None)


def _rect_from_bbox(bbox: BBox) -> fitz.Rect:
    return fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)


def _cluster_visual_rows(lines: list[RawLine]) -> list[list[RawLine]]:
    rows: list[list[RawLine]] = []
    centers: list[float] = []
    for line in sorted(lines, key=lambda item: ((item.y0 + item.y1) / 2, item.x0)):
        center = (line.y0 + line.y1) / 2
        candidates = [(abs(center - row_center), index) for index, row_center in enumerate(centers)]
        distance, index = min(candidates, default=(float("inf"), -1))
        if distance <= 4.0:
            rows[index].append(line)
            centers[index] = median([(item.y0 + item.y1) / 2 for item in rows[index]])
        else:
            rows.append([line])
            centers.append(center)
    return [row for _, row in sorted(zip(centers, rows), key=lambda item: item[0])]


def _repeated_column_anchors(rows: list[list[RawLine]]) -> list[float]:
    clusters: list[list[float]] = []
    for value in sorted(line.x0 for row in rows for line in row):
        cluster = next((item for item in clusters if abs(median(item) - value) <= 5.0), None)
        if cluster is None:
            clusters.append([value])
        else:
            cluster.append(value)
    return [median(cluster) for cluster in clusters if len(cluster) >= 3]


def _row_anchor_indexes(row: list[RawLine], anchors: list[float]) -> set[int]:
    return {
        min(range(len(anchors)), key=lambda index: abs(line.x0 - anchors[index]))
        for line in row
        if min(abs(line.x0 - anchor) for anchor in anchors) <= 5.0
    }


def _contiguous_row_groups(rows: list[list[RawLine]]) -> list[list[list[RawLine]]]:
    groups: list[list[list[RawLine]]] = []
    for row in rows:
        center = median([(line.y0 + line.y1) / 2 for line in row])
        if not groups:
            groups.append([row])
            continue
        previous = groups[-1][-1]
        previous_center = median([(line.y0 + line.y1) / 2 for line in previous])
        # One visually empty cell can leave only the opposite column as text.
        # Allow one normal table-row gap between proven multi-column rows.
        if center - previous_center <= 40.0:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _bbox_overlap_ratio(left: BBox, right: BBox) -> float:
    width = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    height = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
    area = max(1.0, (left.x1 - left.x0) * (left.y1 - left.y0))
    return width * height / area


def _extract_images(page: fitz.Page) -> list[DocumentImage]:
    images: list[DocumentImage] = []
    seen_xrefs: set[int] = set()
    for image in page.get_images(full=True):
        xref = int(image[0])
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        rects = page.get_image_rects(xref)
        placements = rects or [None]
        for rect in placements:
            images.append(
                DocumentImage(
                    bbox=_bbox(rect) if rect is not None else None,
                    width=int(image[2]) if len(image) > 2 else None,
                    height=int(image[3]) if len(image) > 3 else None,
                    xref=xref,
                )
            )
    return images


def _classify_image_relevance(
    pages: list[tuple[fitz.Rect, int, list[RawLine], list[RawTable], list[DocumentImage], PdfKind]],
) -> None:
    repeated_xrefs = Counter(
        image.xref
        for _rect, _rotation, _lines, _tables, images, _page_kind in pages
        for image in images
        if image.xref is not None
    )
    for page_rect, _rotation, _lines, _tables, images, page_kind in pages:
        for image in images:
            if page_kind in {"scanned", "mixed"}:
                image.ocr_relevance = "text_candidate"
                image.needs_ocr_check = True
                continue
            bbox = image.bbox
            repeated_decoration = (
                image.xref is not None
                and repeated_xrefs[image.xref] > 1
                and bbox is not None
                and (bbox.y1 <= page_rect.height * 0.25 or bbox.y0 >= page_rect.height * 0.75)
            )
            image.ocr_relevance = "decorative" if repeated_decoration else "export_safety_only"
            image.needs_ocr_check = False


def _build_page(
    page_number: int,
    rect: fitz.Rect,
    rotation: int,
    lines: list[RawLine],
    tables: list[RawTable],
    images: list[DocumentImage],
    page_kind: PdfKind,
    repeated_headers: set[str],
    repeated_footers: set[str],
) -> DocumentPage:
    height = float(rect.height)
    body_lines: list[RawLine] = []
    baseline_body_lines: list[RawLine] = []
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
        else:
            baseline_body_lines.append(line)
            if not _inside_any_table(line, tables):
                body_lines.append(line)

    baseline_blocks: list[DocumentBlock] = []
    for baseline_region in _segment_body_regions(baseline_body_lines, [], float(rect.width), height):
        baseline_model, _ = _make_layout_region(baseline_region, 0)
        baseline_blocks.extend(baseline_model.blocks)
    for table in tables:
        if not table.derived_from_aligned_rows:
            continue
        table.order_span = max(
            1,
            sum(
                _inside_any_table(
                    RawLine(page_number, block.bbox, block.original_text, [], "body"),
                    [table],
                )
                for block in baseline_blocks
            ),
        )

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
        rotation=rotation,
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
            order += payload.order_span
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
            rotated=line.rotated,
        )
        blocks.append(_make_text_block([item_line], body_size, start_order + index, forced_kind="list_item"))
    return blocks


def _make_table_block(table: RawTable, reading_order: int) -> DocumentBlock:
    text = "\n".join("\t".join(cell for cell in row) for row in table.cells)
    provenance_cells = [
        cell.model_copy(update={"cell_id": f"{table.table_id}-r{cell.row_index + 1}-c{cell.column_index + 1}"})
        for row in (table.cell_provenance or [])
        for cell in row
    ]
    return DocumentBlock(
        kind="table",
        bbox=table.bbox,
        original_text=text,
        normalized_text=text,
        lines=[],
        style=SpanStyle(),
        reading_order=reading_order,
        table_cells=table.cells,
        table_provenance=TableProvenance(
            table_id=table.table_id,
            bbox=table.bbox,
            row_count=len(table.cells),
            column_count=max((len(row) for row in table.cells), default=0),
            geometry_evidence=table.geometry_evidence,
            cells=provenance_cells,
            warnings=list(table.warnings or []),
        ),
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
        rotated=any(line.rotated for line in ordered),
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


def _preserve_table_cells(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return []
    max_columns = max((len(row) for row in rows), default=0)
    return [row + [""] * (max_columns - len(row)) for row in rows]


def _logical_table_cells(rows: list[list[str]], geometry_rows: list[Any]) -> tuple[list[list[str]], list[int], list[int]]:
    source_row_indexes = [index for index, row in enumerate(rows) if any(cell.strip() for cell in row)]
    if not source_row_indexes:
        source_row_indexes = list(range(len(rows)))
    column_count = max((len(row) for row in rows), default=0)
    source_column_indexes: list[int] = []
    for column_index in range(column_count):
        has_text = any(
            column_index < len(rows[row_index]) and bool(rows[row_index][column_index].strip())
            for row_index in source_row_indexes
        )
        widths = []
        for row_index in source_row_indexes:
            geometry_row = list(getattr(geometry_rows[row_index], "cells", []) or []) if row_index < len(geometry_rows) else []
            value = geometry_row[column_index] if column_index < len(geometry_row) else None
            if value:
                widths.append(fitz.Rect(value).width)
        if has_text or (widths and median(widths) >= 8.0):
            source_column_indexes.append(column_index)
    cleaned = [
        [rows[row_index][column_index] for column_index in source_column_indexes]
        for row_index in source_row_indexes
    ]
    return cleaned, source_row_indexes, source_column_indexes


def _logical_column_edges(geometry_rows: list[Any], row_indexes: list[int], column_indexes: list[int]) -> list[float]:
    columns: list[list[fitz.Rect]] = []
    for column_index in column_indexes:
        values: list[fitz.Rect] = []
        for row_index in row_indexes:
            row = list(getattr(geometry_rows[row_index], "cells", []) or []) if row_index < len(geometry_rows) else []
            if column_index < len(row) and row[column_index]:
                values.append(fitz.Rect(row[column_index]))
        columns.append(values)
    if not columns or any(not values for values in columns):
        return []
    edges = [min(rect.x0 for rect in columns[0])]
    for left, right in zip(columns, columns[1:]):
        edges.append((max(rect.x1 for rect in left) + min(rect.x0 for rect in right)) / 2)
    edges.append(max(rect.x1 for rect in columns[-1]))
    return edges


def _logical_row_edges(geometry_rows: list[Any], row_indexes: list[int]) -> list[float]:
    rows = [
        [fitz.Rect(value) for value in (getattr(geometry_rows[index], "cells", []) or []) if value]
        for index in row_indexes
    ]
    if not rows or any(not values for values in rows):
        return []
    edges = [min(rect.y0 for rect in rows[0])]
    for upper, lower in zip(rows, rows[1:]):
        edges.append((max(rect.y1 for rect in upper) + min(rect.y0 for rect in lower)) / 2)
    edges.append(max(rect.y1 for rect in rows[-1]))
    return edges


def _covered_intervals(start: float, end: float, edges: list[float]) -> int:
    return max(1, sum(start <= left + 0.25 and end >= right - 0.25 for left, right in zip(edges, edges[1:])))


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


def _span_from_payload(span: dict[str, Any], span_id: str) -> DocumentSpan:
    font = span.get("font")
    flags = int(span.get("flags") or 0)
    return DocumentSpan(
        span_id=span_id,
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
