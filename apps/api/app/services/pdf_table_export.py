from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pymupdf

from app.models import BBox


CELL_ID_RE = re.compile(r"-c(?P<row>\d+)-(?P<column>\d+)$")
TECHNICAL_VALUE_RE = re.compile(
    r"^(?:RAL\s+\d+|[+-]?\d+(?:[.,]\d+)?(?:\s*(?:-|–|—|to)\s*[+-]?\d+(?:[.,]\d+)?)?"
    r"\s*(?:%|°C|MPa|Pa|kPa|bar|ml|mL|L|l|g|kg|mm|cm|m|h|min|s|R\.h\.)"
    r"(?:\s*\([^)]*\))?)$",
    re.IGNORECASE,
)
TECHNICAL_TOKENS = {"bar", "c", "cm", "din", "g", "gr", "h", "kg", "kpa", "l", "m", "min", "ml", "mpa", "pa", "ral", "rh", "s", "to"}


@dataclass(frozen=True)
class TableExportThresholds:
    minimum_cell_padding_pt: float = 0.5
    border_tolerance_pt: float = 0.75
    geometry_tolerance_pt: float = 0.5
    minimum_text_density: float = 0.6


DEFAULT_TABLE_THRESHOLDS = TableExportThresholds()


@dataclass
class TableCellGeometry:
    row_index: int
    column_index: int
    row_span: int
    column_span: int
    text: str
    cell_bbox: BBox | None
    source_bbox: BBox | None
    content_bbox: BBox | None
    background_fill: str | None
    border_clearance: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class DetectedTable:
    table_id: str
    page_number: int
    bbox: BBox
    row_count: int
    column_count: int
    cells: list[list[TableCellGeometry]]
    reliable: bool
    warnings: list[str]
    overlapping_image_xrefs: list[int]


class PdfTableAnalyzer:
    def __init__(self, thresholds: TableExportThresholds = DEFAULT_TABLE_THRESHOLDS) -> None:
        self.thresholds = thresholds

    def detect(self, page: pymupdf.Page, page_number: int) -> list[DetectedTable]:
        if not hasattr(page, "find_tables"):
            return []
        try:
            tables = list(page.find_tables().tables or [])
        except Exception:
            return []
        return [self._canonicalize(page, table, page_number, index) for index, table in enumerate(tables)]

    def _canonicalize(self, page: pymupdf.Page, table: Any, page_number: int, index: int) -> DetectedTable:
        raw_rows = [[_normalize(cell) for cell in row] for row in (table.extract() or [])]
        row_geometries = [list(row.cells) for row in table.rows]
        row_width = max((len(row) for row in raw_rows), default=0)
        raw_rows = [row + [""] * (row_width - len(row)) for row in raw_rows]
        row_geometries = [row + [None] * (row_width - len(row)) for row in row_geometries]
        nonempty_rows = [row_index for row_index, row in enumerate(raw_rows) if any(row)]
        nonempty_columns = [
            column_index
            for column_index in range(row_width)
            if any(raw_rows[row_index][column_index] for row_index in nonempty_rows)
        ]
        warnings: list[str] = []
        table_rect = pymupdf.Rect(table.bbox)
        image_xrefs = sorted({
            int(image["xref"])
            for image in page.get_image_info(xrefs=True)
            if image.get("xref") and not (pymupdf.Rect(image["bbox"]) & table_rect).is_empty
        })
        if not nonempty_rows or not nonempty_columns:
            warnings.append("unsupported_table_structure")
        if nonempty_rows:
            internal_rows = range(nonempty_rows[0], nonempty_rows[-1] + 1)
            if any(not any(raw_rows[row_index]) for row_index in internal_rows):
                warnings.append("internal_empty_rows")
        if image_xrefs and "internal_empty_rows" in warnings:
            warnings.append("graphic_table_with_images")

        column_centers = _column_centers(row_geometries, nonempty_rows, nonempty_columns)
        row_centers = _row_centers(row_geometries, nonempty_rows, nonempty_columns)
        cells: list[list[TableCellGeometry]] = []
        used_geometries: set[tuple[float, float, float, float]] = set()
        nonempty_count = 0
        for logical_row, raw_row in enumerate(nonempty_rows):
            output_row: list[TableCellGeometry] = []
            for logical_column, raw_column in enumerate(nonempty_columns):
                text = raw_rows[raw_row][raw_column]
                cell_value = row_geometries[raw_row][raw_column]
                cell_rect = pymupdf.Rect(cell_value) if cell_value else None
                cell_warnings: list[str] = []
                if text:
                    nonempty_count += 1
                if text and cell_rect is None:
                    cell_warnings.append("missing_cell_bbox")
                geometry_key = _rect_key(cell_rect) if cell_rect else None
                if text and geometry_key in used_geometries:
                    cell_warnings.append("duplicate_cell_geometry")
                if text and geometry_key:
                    used_geometries.add(geometry_key)
                row_span = _span_count(cell_rect, row_centers) if cell_rect else 1
                column_span = _span_count(cell_rect, column_centers, horizontal=True) if cell_rect else 1
                source_rect = _source_text_rect(page, cell_rect, text) if cell_rect and text else None
                clearance = _border_clearance(page, cell_rect, self.thresholds) if cell_rect else self.thresholds.minimum_cell_padding_pt
                content_rect = _inset(cell_rect, clearance) if cell_rect else None
                if cell_rect and (content_rect is None or content_rect.is_empty):
                    cell_warnings.append("missing_cell_bbox")
                output_row.append(TableCellGeometry(
                    row_index=logical_row,
                    column_index=logical_column,
                    row_span=max(1, row_span),
                    column_span=max(1, column_span),
                    text=text,
                    cell_bbox=_bbox(cell_rect) if cell_rect else None,
                    source_bbox=_bbox(source_rect) if source_rect else None,
                    content_bbox=_bbox(content_rect) if content_rect else None,
                    background_fill=_background_fill(page, cell_rect) if cell_rect else None,
                    border_clearance=clearance,
                    warnings=cell_warnings,
                ))
            cells.append(output_row)

        total_cells = max(1, len(nonempty_rows) * len(nonempty_columns))
        density = nonempty_count / total_cells
        if density < self.thresholds.minimum_text_density:
            warnings.append("sparse_table_structure")
        reliable = bool(cells and nonempty_columns) and not any(
            warning in warnings for warning in ("unsupported_table_structure", "graphic_table_with_images", "sparse_table_structure")
        )
        return DetectedTable(
            table_id=f"p{page_number}-t{index + 1}",
            page_number=page_number,
            bbox=_bbox(table_rect),
            row_count=len(nonempty_rows),
            column_count=len(nonempty_columns),
            cells=cells,
            reliable=reliable,
            warnings=warnings,
            overlapping_image_xrefs=image_xrefs,
        )


def parse_cell_position(segment_id: str) -> tuple[int, int] | None:
    match = CELL_ID_RE.search(segment_id)
    if not match:
        return None
    return int(match.group("row")) - 1, int(match.group("column")) - 1


def is_technical_value(text: str) -> bool:
    normalized = _normalize(text)
    if "\ufffd" in normalized or not any(character.isdigit() for character in normalized):
        return False
    if TECHNICAL_VALUE_RE.fullmatch(normalized):
        return True
    words = re.findall(r"[A-Za-z]+", normalized.casefold())
    return bool(words) and all(word in TECHNICAL_TOKENS for word in words)


def canonical_text(text: str) -> str:
    return _normalize(text).casefold()


def _source_text_rect(page: pymupdf.Page, cell_rect: pymupdf.Rect, expected: str) -> pymupdf.Rect | None:
    spans: list[pymupdf.Rect] = []
    texts: list[str] = []
    for block in page.get_text("dict", clip=cell_rect).get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                rect = pymupdf.Rect(span["bbox"])
                center = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
                if text.strip() and cell_rect.contains(center):
                    texts.append(text)
                    spans.append(rect)
    if canonical_text(" ".join(texts)) != canonical_text(expected) or not spans:
        return None
    result = pymupdf.Rect(spans[0])
    for rect in spans[1:]:
        result |= rect
    return result


def _background_fill(page: pymupdf.Page, cell_rect: pymupdf.Rect) -> str | None:
    center = ((cell_rect.x0 + cell_rect.x1) / 2, (cell_rect.y0 + cell_rect.y1) / 2)
    candidates: list[tuple[float, tuple[float, ...]]] = []
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        rect = pymupdf.Rect(drawing["rect"])
        if fill and rect.contains(center):
            candidates.append((rect.get_area(), fill))
    if not candidates:
        return None
    _, fill = min(candidates, key=lambda item: item[0])
    return "#" + "".join(f"{round(max(0, min(1, value)) * 255):02x}" for value in fill[:3])


def _border_clearance(
    page: pymupdf.Page,
    cell_rect: pymupdf.Rect,
    thresholds: TableExportThresholds,
) -> float:
    widths: list[float] = []
    expanded = pymupdf.Rect(
        cell_rect.x0 - thresholds.border_tolerance_pt,
        cell_rect.y0 - thresholds.border_tolerance_pt,
        cell_rect.x1 + thresholds.border_tolerance_pt,
        cell_rect.y1 + thresholds.border_tolerance_pt,
    )
    for drawing in page.get_drawings():
        drawing_rect = pymupdf.Rect(drawing["rect"])
        if (drawing_rect & expanded).is_empty:
            continue
        width = drawing.get("width")
        if isinstance(width, (int, float)) and width > 0:
            widths.append(float(width))
        for item in drawing.get("items") or []:
            if item[0] != "re":
                continue
            rect = pymupdf.Rect(item[1])
            narrow = min(rect.width, rect.height)
            if 0 < narrow <= thresholds.border_tolerance_pt * 2:
                widths.append(narrow)
    border_width = max(widths, default=0.0)
    return round(max(thresholds.minimum_cell_padding_pt, border_width / 2 + 0.25), 3)


def _column_centers(rows, row_indices: list[int], columns: list[int]) -> list[float]:
    centers: list[float] = []
    for column in columns:
        values = [pymupdf.Rect(rows[row][column]) for row in row_indices if rows[row][column]]
        centers.append(sum((rect.x0 + rect.x1) / 2 for rect in values) / len(values))
    return centers


def _row_centers(rows, row_indices: list[int], columns: list[int]) -> list[float]:
    centers: list[float] = []
    for row in row_indices:
        values = [pymupdf.Rect(rows[row][column]) for column in columns if rows[row][column]]
        centers.append(sum((rect.y0 + rect.y1) / 2 for rect in values) / len(values))
    return centers


def _span_count(rect: pymupdf.Rect | None, centers: list[float], horizontal: bool = False) -> int:
    if rect is None:
        return 1
    if horizontal:
        return sum(rect.x0 - 0.1 <= center <= rect.x1 + 0.1 for center in centers)
    return sum(rect.y0 - 0.1 <= center <= rect.y1 + 0.1 for center in centers)


def _inset(rect: pymupdf.Rect, amount: float) -> pymupdf.Rect | None:
    value = pymupdf.Rect(rect.x0 + amount, rect.y0 + amount, rect.x1 - amount, rect.y1 - amount)
    return value if not value.is_empty else None


def _normalize(value: str | None) -> str:
    return " ".join((value or "").replace("\u00a0", " ").split())


def _rect_key(rect: pymupdf.Rect) -> tuple[float, float, float, float]:
    return tuple(round(value, 3) for value in (rect.x0, rect.y0, rect.x1, rect.y1))


def _bbox(rect: pymupdf.Rect) -> BBox:
    return BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)
