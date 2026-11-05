"""Explicit local pilot, deliberately separate from ordinary replacement export.

The selected table's old appearance is clipped, not claimed to be preserved.
Image streams (including shared images outside the selection) are never edited.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf
from PIL import Image, ImageChops, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from app.models import BBox, TableProvenance
from app.pdf_export_models import PdfExportJob
from app.services import pdf_exporter as exporter
from app.services.document_segmenter import segment_pdf
from app.services.pdf_typography import PdfTypographyResolver

RECONSTRUCTION_VERSION = "explicit-table-reconstruction-pilot-v1"
BOUNDARY_MARGIN = 0.5
PADDING = 1.5


class TableReconstructionError(ValueError):
    pass


class TableReconstructionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_number: int = Field(ge=1)
    table_id: str
    inventory_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReconstructedCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cell_id: str
    segment_id: str | None
    row: int
    column: int
    column_span: int
    source: str
    target: str
    source_bbox: BBox | None
    source_span_ids: tuple[str, ...]
    source_value: Literal["reviewed_translation", "identical_source", "unchanged_technical_source", "structural_empty"]
    role: Literal["title", "header", "body"]
    bbox: BBox
    lines: tuple[str, ...]
    baselines: tuple[float, ...]
    font_size: float


class TableReconstructionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = RECONSTRUCTION_VERSION
    selection: TableReconstructionSelection
    job_sha256: str
    source_table: TableProvenance
    source_rows: tuple[tuple[str, ...], ...]
    replacement_region: BBox
    row_heights: tuple[float, ...]
    cells: tuple[ReconstructedCell, ...]
    image_effects: tuple[dict, ...]
    vector_effects: tuple[dict, ...]


@dataclass(frozen=True)
class TableReconstructionArtifact:
    path: Path
    plan: TableReconstructionPlan
    verification: dict


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _rect(box: BBox) -> pymupdf.Rect:
    return pymupdf.Rect(box.x0, box.y0, box.x1, box.y1)


def _bbox(rect: pymupdf.Rect) -> BBox:
    return BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)


def _nonempty_chars(page: pymupdf.Page):
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if char["c"].strip():
                        yield char


def _closed_outer_border(page: pymupdf.Page, rect: pymupdf.Rect) -> bool:
    """Prove connected thin-rule coverage, including borders drawn row by row."""
    horizontal, vertical = [], []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if item[0] == "l" and drawing.get("color") is not None:
                first, second = item[1:3]
                if abs(first.y-second.y) < 0.1:
                    horizontal.append((first.y, min(first.x, second.x), max(first.x, second.x)))
                if abs(first.x-second.x) < 0.1:
                    vertical.append((first.x, min(first.y, second.y), max(first.y, second.y)))
            elif item[0] == "re":
                box = pymupdf.Rect(item[1])
                if drawing.get("color") is not None or (drawing.get("fill") is not None and min(box.width, box.height) <= 0.75):
                    horizontal.extend([(box.y0, box.x0, box.x1), (box.y1, box.x0, box.x1)])
                    vertical.extend([(box.x0, box.y0, box.y1), (box.x1, box.y0, box.y1)])

    def covered(segments, position, start, end):
        intervals = sorted((a, b) for coordinate, a, b in segments if abs(coordinate-position) <= 0.75)
        cursor = start
        for a, b in intervals:
            if a > cursor+0.75:
                break
            cursor = max(cursor, b)
        return cursor >= end-0.75

    return (covered(horizontal, rect.y0, rect.x0, rect.x1)
            and covered(horizontal, rect.y1, rect.x0, rect.x1)
            and covered(vertical, rect.x0, rect.y0, rect.y1)
            and covered(vertical, rect.x1, rect.y0, rect.y1))


def inspect_reconstruction_table(source: Path, page_number: int, table_id: str) -> dict:
    """Return a fresh authoritative inventory before an explicit destructive selection."""
    model = segment_pdf(source)
    blocks = [b for p in model.pages if p.page_number == page_number for r in p.regions
              for b in r.blocks if b.table_provenance and b.table_provenance.table_id == table_id]
    if len(blocks) != 1:
        raise TableReconstructionError("missing_or_ambiguous_table")
    block = blocks[0]
    table = block.table_provenance
    rows = block.table_cells
    if (not rows or table.geometry_evidence != "vector_grid" or table.row_count > 100
            or table.column_count > 10 or len(rows) != table.row_count
            or any(len(row) != table.column_count for row in rows)):
        raise TableReconstructionError("unproven_table_structure")
    with pymupdf.open(source) as document:
        if not 1 <= page_number <= document.page_count:
            raise TableReconstructionError("invalid_page")
        page = document[page_number - 1]
        region = _rect(table.bbox)
        if (page.rotation or page.cropbox != page.mediabox or page.mediabox.x0 != 0
                or page.mediabox.y0 != 0 or not all(math.isfinite(v) for v in region)
                or region.is_empty or not page.rect.contains(region)
                or not _closed_outer_border(page, region)):
            raise TableReconstructionError("unproven_outer_boundary")
        region = pymupdf.Rect(region.x0 - BOUNDARY_MARGIN, region.y0 - BOUNDARY_MARGIN,
                              region.x1 + BOUNDARY_MARGIN, region.y1 + BOUNDARY_MARGIN)
        if not page.rect.contains(region):
            raise TableReconstructionError("boundary_outside_page")
        cells = {(c.row_index, c.column_index): c for c in table.cells}
        if len(cells) != len(table.cells):
            raise TableReconstructionError("duplicate_source_cell")
        covered = set()
        source_boxes = []
        for row in range(table.row_count):
            for column in range(table.column_count):
                cell = cells.get((row, column))
                if cell is None:
                    raise TableReconstructionError("missing_source_cell")
                text = rows[row][column]
                if (row, column) in covered:
                    if text.strip():
                        raise TableReconstructionError("overlapping_merged_content")
                    continue
                if (cell.row_span != 1 or cell.rotated_text or cell.cell_bbox is None
                        or len(cell.proven_edges) != 4
                        or cell.column_index + cell.column_span > table.column_count
                        or cell.block_reason not in {None, "merged_cell", "graphic_overlap"}):
                    raise TableReconstructionError("ambiguous_cell_boundary")
                for other in range(column, column + cell.column_span):
                    if (row, other) in covered:
                        raise TableReconstructionError("overlapping_source_cells")
                    covered.add((row, other))
                if text.strip():
                    if (cell.source_bbox is None or not cell.source_span_ids
                            or not region.contains(_rect(cell.source_bbox))
                            or not exporter._text_matches(text, page.get_text(clip=_rect(cell.source_bbox)))):
                        raise TableReconstructionError("missing_or_mismatched_source_content")
                    source_boxes.append(_rect(cell.source_bbox))
        # A table bbox is not evidence that every native glyph in it is a table glyph.
        for char in _nonempty_chars(page):
            rect = pymupdf.Rect(char["bbox"])
            if (rect & region).is_empty:
                continue
            if not region.contains(rect) or not any(box.contains(rect) for box in source_boxes):
                raise TableReconstructionError("unaccounted_text_in_selected_region")
        inventory = {"table": table.model_dump(mode="json"), "rows": rows}
        return {**inventory, "inventory_sha256": _canonical(inventory),
                "source_sha256": _sha(source), "replacement_region": _bbox(region).model_dump()}


def _wrap(text: str, font: pymupdf.Font, size: float, width: float) -> tuple[str, ...]:
    words = exporter._protect_table_layout_text(text).split(" ")
    lines = []
    line = ""
    for word in words:
        if not word:
            continue
        if font.text_length(word, fontsize=size) > width:
            raise TableReconstructionError("unbreakable_cell_overflow")
        proposal = f"{line} {word}" if line else word
        if font.text_length(proposal, fontsize=size) > width:
            lines.append(line)
            line = word
        else:
            line = proposal
    if line:
        lines.append(line)
    return tuple(lines)


def plan_table_reconstruction(
    source: Path, base_pdf: Path, job: PdfExportJob,
    selection: TableReconstructionSelection, temp_dir: Path,
) -> TableReconstructionPlan:
    if not selection.enabled:
        raise TableReconstructionError("explicit_reconstruction_selection_required")
    if _sha(source) != selection.source_sha256 or _sha(base_pdf) != selection.base_pdf_sha256:
        raise TableReconstructionError("document_identity_changed")
    inventory = inspect_reconstruction_table(source, selection.page_number, selection.table_id)
    if inventory["inventory_sha256"] != selection.inventory_sha256:
        raise TableReconstructionError("table_inventory_changed")
    table = TableProvenance.model_validate(inventory["table"])
    rows = inventory["rows"]
    region = _rect(BBox.model_validate(inventory["replacement_region"]))
    temp_dir.mkdir(parents=True, exist_ok=True)
    typography = PdfTypographyResolver(temp_dir)
    with pymupdf.open(source) as original, pymupdf.open(base_pdf) as base:
        exporter._validate_document_identity(source, original, job)
        if len(base) != len(original) or any(a.rect != b.rect or a.rotation != b.rotation
                                             for a, b in zip(original, base)):
            raise TableReconstructionError("base_geometry_changed")
        page = original[selection.page_number - 1]
        targetpage = base[selection.page_number - 1]
        if targetpage.rotation or targetpage.cropbox != targetpage.mediabox:
            raise TableReconstructionError("unsupported_base_coordinates")
        if any(not (annot.rect & region).is_empty for annot in targetpage.annots() or []):
            raise TableReconstructionError("annotation_in_selected_region")
        if any(not (widget.rect & region).is_empty for widget in targetpage.widgets() or []):
            raise TableReconstructionError("widget_in_selected_region")
        if any(not region.contains(pymupdf.Rect(c["bbox"])) for c in _nonempty_chars(targetpage)
               if not (pymupdf.Rect(c["bbox"]) & region).is_empty):
            raise TableReconstructionError("base_text_crosses_selected_boundary")
        table_segments = [s for s in job.segments if s.page_number == selection.page_number
                          and s.table_cell_provenance and s.table_cell_provenance.table_id == table.table_id]
        if any(not s.source_text.strip() and s.edited_translation.strip() for s in table_segments):
            raise TableReconstructionError("unexpected_translation_in_empty_cell")
        table_segments = [s for s in table_segments if s.source_text.strip()]
        source_cells = []
        covered = set()
        sizes = []
        for cell in sorted(table.cells, key=lambda c: (c.row_index, c.column_index)):
            pos = (cell.row_index, cell.column_index)
            if pos in covered:
                continue
            covered.update((cell.row_index, c) for c in range(cell.column_index, cell.column_index+cell.column_span))
            text = rows[cell.row_index][cell.column_index]
            segment = None
            if text.strip():
                matches = [s for s in table_segments if s.table_cell_provenance.cell_id == cell.cell_id]
                if len(matches) != 1:
                    raise TableReconstructionError("missing_or_duplicate_job_cell")
                segment = matches[0]
                unchanged_value = (segment.status == "skipped" and exporter.is_technical_value(text)
                                   and segment.edited_translation in {"", text})
                if (segment.source_text_origin != "native"
                        or (not unchanged_value and (segment.status != "reviewed" or not segment.edited_translation.strip()))
                        or not exporter._provenance_matches(segment.table_cell_provenance, table, cell)
                        or segment.source_text != text):
                    raise TableReconstructionError("unreviewed_or_stale_cell")
                if not unchanged_value:
                    exporter._require_technical_integrity(segment, text, source, page_number=page.number + 1, source_bbox=cell.source_bbox.model_dump(), source_kind="native")
                sizes.extend(typography.source_font_sizes(page, _rect(cell.source_bbox)))
            source_cells.append((cell, segment, text))
        if len(table_segments) != sum(bool(text.strip()) for _, _, text in source_cells):
            raise TableReconstructionError("extra_job_cells")
        if not sizes:
            raise TableReconstructionError("unknown_group_font_size")
        body_size = round(statistics.median(sizes), 2)
        if not 6 <= body_size <= 24:
            raise TableReconstructionError("unsupported_group_font_size")
        edges = [region.x0 + 0.5]
        for column in range(1, table.column_count):
            left = [c.cell_bbox.x1 for c, _, _ in source_cells if c.column_index == column-1 and c.column_span == 1]
            right = [c.cell_bbox.x0 for c, _, _ in source_cells if c.column_index == column and c.column_span == 1]
            if not left or not right:
                raise TableReconstructionError("unknown_column_boundary")
            edges.append((statistics.median(left)+statistics.median(right))/2)
        edges.append(region.x1 - 0.5)
        if any(b-a <= 2*PADDING for a, b in zip(edges, edges[1:])):
            raise TableReconstructionError("invalid_column_geometry")
        prepared = []
        heights = [0.0] * table.row_count
        has_title = any(c.row_index == 0 and c.column_span == table.column_count for c, _, _ in source_cells)
        for cell, segment, text in source_cells:
            role = "title" if has_title and cell.row_index == 0 else "header" if cell.row_index == int(has_title) else "body"
            size = round(body_size*1.1, 3) if role == "title" else body_size
            asset = typography.assets[(role != "body", False)]
            target = text if segment and segment.status == "skipped" else segment.edited_translation if segment else ""
            if not typography.supports(asset, target):
                raise TableReconstructionError("missing_target_glyphs")
            font = pymupdf.Font(fontfile=str(asset.path))
            width = edges[cell.column_index+cell.column_span]-edges[cell.column_index]-2*PADDING
            lines = _wrap(target, font, size, width)
            glyph_height = (font.ascender-font.descender)*size
            ink_height = glyph_height + max(0, len(lines)-1)*size*1.2
            heights[cell.row_index] = max(heights[cell.row_index], ink_height+2*PADDING)
            prepared.append((cell, segment, text, role, size, lines, ink_height, font.ascender))
        interior = pymupdf.Rect(region.x0+0.5, region.y0+0.5, region.x1-0.5, region.y1-0.5)
        spare = interior.height-sum(heights)
        if spare < -0.001:
            raise TableReconstructionError(
                f"complete_table_overflow: required={sum(heights):.3f}pt; available={interior.height:.3f}pt"
            )
        heights = [h+max(0.0, spare)/len(heights) for h in heights]
        ys = [interior.y0]
        for height in heights:
            ys.append(ys[-1]+height)
        output = []
        for cell, segment, text, role, size, lines, ink_height, ascender in prepared:
            rect = pymupdf.Rect(edges[cell.column_index], ys[cell.row_index],
                                edges[cell.column_index+cell.column_span], ys[cell.row_index+1])
            baseline = rect.y0+(rect.height-ink_height)/2+ascender*size
            output.append(ReconstructedCell(cell_id=cell.cell_id,
                segment_id=segment.segment_id if segment else None, row=cell.row_index,
                column=cell.column_index, column_span=cell.column_span, source=text,
                target=text if segment and segment.status == "skipped" else segment.edited_translation if segment else "", source_bbox=cell.source_bbox,
                source_span_ids=tuple(cell.source_span_ids), source_value="structural_empty" if not segment else
                "unchanged_technical_source" if segment.status == "skipped" else
                "identical_source" if text == segment.edited_translation else "reviewed_translation",
                role=role, bbox=_bbox(rect), lines=lines,
                baselines=tuple(baseline+i*size*1.2 for i in range(len(lines))), font_size=size))
        image_effects = []
        for info in targetpage.get_image_info(xrefs=True):
            if (pymupdf.Rect(info["bbox"]) & region).is_empty:
                continue
            digest = info["digest"].hex()
            image_effects.append({"xref": info["xref"], "digest": digest, "bbox": info["bbox"],
                "effect": "appearance_clipped_only_inside_selection; image_stream_unchanged",
                "placements": [{"page": number+1, "bbox": item["bbox"]}
                    for number, p in enumerate(base) for item in p.get_image_info(xrefs=True)
                    if item["digest"].hex() == digest]})
        vector_effects = [{"seqno": d["seqno"], "bbox": list(d["rect"]),
                           "effect": "appearance_replaced_inside_selection; retained_outside"}
                          for d in targetpage.get_drawings() if not (pymupdf.Rect(d["rect"]) & region).is_empty]
        return TableReconstructionPlan(selection=selection, job_sha256=_canonical(job.model_dump(mode="json")),
            source_table=table, source_rows=tuple(tuple(row) for row in rows), replacement_region=_bbox(region),
            row_heights=tuple(heights), cells=tuple(output), image_effects=tuple(image_effects),
            vector_effects=tuple(vector_effects))


def _outside_pixels(before: pymupdf.Document, after: pymupdf.Document, plan: TableReconstructionPlan) -> list[dict]:
    results = []
    scale = 2.0
    for number, (a, b) in enumerate(zip(before, after), start=1):
        left = a.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        right = b.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        imga = Image.frombytes("RGB", (left.width, left.height), left.samples)
        imgb = Image.frombytes("RGB", (right.width, right.height), right.samples)
        diff = ImageChops.difference(imga, imgb)
        if number == plan.selection.page_number:
            r = _rect(plan.replacement_region)
            # Pixels straddling the selected boundary belong to the changed region.
            ImageDraw.Draw(diff).rectangle((math.floor(r.x0*scale), math.floor(r.y0*scale),
                math.ceil(r.x1*scale), math.ceil(r.y1*scale)), fill=(0, 0, 0))
        if diff.getbbox() is not None:
            raise TableReconstructionError(f"outside_pixels_changed_page_{number}: {diff.getbbox()}")
        results.append({"page": number, "dpi": 144, "outside_changed_pixels": 0})
    return results


def export_table_reconstruction(
    source: Path, base_pdf: Path, job: PdfExportJob, plan: TableReconstructionPlan, temp_dir: Path,
) -> TableReconstructionArtifact:
    # Rebuild from current authoritative source/job, never execute edited or stale plan data.
    current = plan_table_reconstruction(source, base_pdf, job, plan.selection, temp_dir)
    if current.model_dump(mode="json") != plan.model_dump(mode="json"):
        raise TableReconstructionError("reconstruction_plan_changed")
    output = temp_dir / "table-reconstruction-pilot.pdf"
    if output.exists() or output.resolve() in {source.resolve(), base_pdf.resolve()}:
        raise TableReconstructionError("output_must_be_new_file")
    typography = PdfTypographyResolver(temp_dir)
    region = _rect(plan.replacement_region)
    with pymupdf.open(base_pdf) as document:
        page = document[plan.selection.page_number-1]
        page.add_redact_annot(region, fill=False, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        if any(not (pymupdf.Rect(c["bbox"]) & region).is_empty for c in _nonempty_chars(page)):
            raise TableReconstructionError("old_native_table_text_remains")
        page.wrap_contents()
        original_content = page.read_contents()
        x, y = region.x0, page.rect.height-region.y1
        # The page already supplies its outer clip. Keep this auxiliary outer
        # contour beyond it so fractional page edges are not anti-aliased twice.
        clip = (f"q\n{-page.rect.width:.8f} {-page.rect.height:.8f} {3*page.rect.width:.8f} {3*page.rect.height:.8f} re\n"
                f"{x:.8f} {y:.8f} {region.width:.8f} {region.height:.8f} re W* n\n").encode()
        stream = document.get_new_xref()
        document.update_object(stream, "<<>>")
        document.update_stream(stream, clip+original_content+b"\nQ\n")
        page.set_contents(stream)
        page.draw_rect(region, color=None, fill=(1, 1, 1), overlay=True)
        for bold, name in [(False, "reconstruction_regular"), (True, "reconstruction_bold")]:
            page.insert_font(fontname=name, fontfile=str(typography.assets[(bold, False)].path))
        for cell in plan.cells:
            rect = _rect(cell.bbox)
            fill = (0.20, 0.24, 0.28) if cell.role == "title" else (0.90, 0.93, 0.95) if cell.role == "header" else (
                (0.95, 0.96, 0.97) if cell.row % 2 == 0 else (1, 1, 1))
            page.draw_rect(rect, color=None, fill=fill, overlay=True)
        # Union collinear edges so shared borders are not painted twice. A
        # spanning title has no interior divider through its own row.
        horizontal, vertical = {}, {}
        for cell in plan.cells:
            r = _rect(cell.bbox)
            for y in (r.y0, r.y1):
                horizontal.setdefault(round(y, 5), []).append((r.x0, r.x1))
            for x in (r.x0, r.x1):
                vertical.setdefault(round(x, 5), []).append((r.y0, r.y1))
        shape = page.new_shape()
        for direction, groups in (("horizontal", horizontal), ("vertical", vertical)):
            for position, intervals in groups.items():
                merged = []
                for start, end in sorted(intervals):
                    if merged and start <= merged[-1][1]+0.0001:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                    else:
                        merged.append((start, end))
                for start, end in merged:
                    if direction == "horizontal":
                        shape.draw_line((start, position), (end, position))
                    else:
                        shape.draw_line((position, start), (position, end))
        shape.finish(color=(0.77, 0.79, 0.81), width=0.3, closePath=False)
        shape.commit(overlay=True)
        for cell in plan.cells:
            rect = _rect(cell.bbox)
            for line, baseline in zip(cell.lines, cell.baselines):
                page.insert_text((rect.x0+PADDING, baseline), line, fontsize=cell.font_size,
                    fontname="reconstruction_bold" if cell.role != "body" else "reconstruction_regular",
                    color=(1, 1, 1) if cell.role == "title" else (0, 0, 0), overlay=True)
        document.save(output, deflate=True)
    try:
        with pymupdf.open(base_pdf) as before, pymupdf.open(output) as after:
            outside = _outside_pixels(before, after, plan)
            page = after[plan.selection.page_number-1]
            for cell in plan.cells:
                actual = page.get_text(clip=_rect(cell.bbox))
                if not exporter._written_text_matches(cell.target, actual):
                    raise TableReconstructionError(f"cell_output_mismatch:{cell.cell_id}")
            # All searchable native text in the selected area must be from the new table.
            expected = Counter(c for cell in plan.cells for c in exporter._verification_text(cell.target) if not c.isspace())
            actual = Counter(c for c in exporter._verification_text(page.get_text(clip=region)) if not c.isspace())
            if actual != expected:
                raise TableReconstructionError("unexpected_hidden_or_duplicate_table_text")
            def image_streams(doc):
                return {hashlib.sha256(doc.extract_image(i[0])["image"]).hexdigest()
                        for p in doc for i in p.get_images(full=True)}

            def outside_images(doc):
                return Counter((number, bytes(i["digest"]), tuple(i["bbox"]))
                    for number, p in enumerate(doc, start=1) for i in p.get_image_info(hashes=True)
                    if number != plan.selection.page_number or not region.contains(pymupdf.Rect(i["bbox"])))

            if image_streams(before) != image_streams(after) or outside_images(before) != outside_images(after):
                raise TableReconstructionError("image_stream_or_placement_changed")
            def outside_text(doc):
                return Counter((number, c["c"], tuple(round(v, 3) for v in c["bbox"]),
                                tuple(round(v, 3) for v in c["origin"]))
                    for number, p in enumerate(doc, start=1) for c in _nonempty_chars(p)
                    if number != plan.selection.page_number or (pymupdf.Rect(c["bbox"]) & region).is_empty)

            if outside_text(before) != outside_text(after):
                raise TableReconstructionError("outside_native_text_changed")
        verification = {"outside_pages": outside, "cells_verified": len(plan.cells),
            "image_streams_and_outside_placements_retained": True,
            "outside_native_characters_and_positions_retained": True,
            "appearance_inside_region_intentionally_replaced": True,
            "hidden_native_table_text_removed": True}
        return TableReconstructionArtifact(output, plan, verification)
    except Exception:
        # A failed verifier must never leave a seemingly successful pilot document.
        try:
            with pymupdf.open(output) as rejected:
                rejected[plan.selection.page_number-1].get_pixmap(dpi=72, alpha=False).save(temp_dir / "rejected-preview.png")
        except (RuntimeError, ValueError, OSError):
            pass
        finally:
            output.unlink(missing_ok=True)
        raise
