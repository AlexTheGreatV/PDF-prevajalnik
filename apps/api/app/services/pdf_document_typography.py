"""Opt-in local typography pilot; source formatting is not translation approval."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import pymupdf

from app.pdf_export_models import (PdfExportJob, PdfExportPlan, PdfReplacement, PdfTableCellReplacement,
                                   PdfTypographyCoverage, PdfTypographyGroupCoverage)
from app.services import pdf_exporter as exporter
from app.services.document_segmenter import segment_pdf
from app.services.pdf_image_overlap import PdfImageOverlapAnalyzer
from app.services.pdf_typography import PdfTypographyResolver

PROFILE_VERSION = "document-role-typography-pilot-v1"
PROFILE_LAYOUT_REVISION = "source-style-heading-label-groups-v9"


class DocumentTypographyResolver(PdfTypographyResolver):
    def __init__(self, temp_dir: Path, document: pymupdf.Document, job: PdfExportJob) -> None:
        super().__init__(temp_dir)
        self.assignments: dict[str, dict] = {}
        self.exceptions: dict[str, str] = {}
        self.groups: dict[str, dict] = {}
        for segment in job.segments:
            if segment.source_text_origin != "native" or segment.source_bbox is None:
                self.exceptions[segment.segment_id] = "non_native_or_missing_source"
                continue
            if not 1 <= segment.page_number <= len(document):
                self.exceptions[segment.segment_id] = "invalid_page"
                continue
            page = document[segment.page_number - 1]
            rect = exporter._rect(segment.source_bbox)
            style, _, warnings, _ = super().resolve(page, rect, segment)
            raw = page.get_text("dict", clip=rect)
            fonts = {span["font"] for block in raw["blocks"] for line in block.get("lines", [])
                     for span in line.get("spans", []) if span.get("text", "").strip()}
            # Mixed styles, symbols, marginalia and display/brand text are outside this pilot.
            if (not fonts or len(fonts) != 1 or style.dominant_style_confidence < 0.99
                    or warnings or style.rotation or style.writing_direction != (1.0, 0.0)
                    or style.font_size < 6 or style.font_size > 18
                    or any("sym" in f.lower() or "wingding" in f.lower() for f in fonts)
                    or segment.block_type not in {"heading", "paragraph", "list_item", "table_cell"}):
                self.exceptions[segment.segment_id] = "mixed_symbol_display_or_unsupported_style"
                continue
            role = segment.block_type
            bold = style.bold
            if role == "heading":
                # Structural heading detection may include a regular label/code.
                # Source emphasis, not the block label, determines its style group.
                role = "heading_" + str(round(style.font_size, 2)) if bold else "paragraph"
            elif role == "table_cell":
                proof = segment.table_cell_provenance
                if proof is None:
                    self.exceptions[segment.segment_id] = "missing_table_provenance"
                    continue
                role = "table_header" if bold or proof.column_span > 1 else "table_label" if proof.column_index == 0 else "table_value"
                bold = bold or role == "table_header"
            elif style.font_size < 8:
                role = "footnote"
            size = round(style.font_size, 3)
            size = max(size, 6.0)
            group = f"{role}:{style.font_size:g}:{int(bold)}:{int(style.italic)}:{style.color}"
            rule = {"group": group, "role": role, "font_family": "FiraGO", "font_size": size,
                    "bold": bold, "italic": style.italic, "color": style.color,
                    "line_height": 1.0 if role.startswith("table_") else 1.2,
                    "source_size": style.font_size, "independent_shrink_allowed": False}
            self.groups[group] = rule
            self.assignments[segment.segment_id] = rule
        font_hashes = {asset.code: hashlib.sha256(asset.path.read_bytes()).hexdigest() for asset in self.assets.values()}
        payload = {"version": PROFILE_VERSION, "layout_revision": PROFILE_LAYOUT_REVISION,
                   "document": job.document_identity.model_dump(),
                   "groups": self.groups, "assignments": self.assignments, "assets": font_hashes}
        self.profile_fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.typography_revision = hashlib.sha256(
            (self.profile_fingerprint + job.model_dump_json()).encode()
        ).hexdigest()

    def resolve(self, page, rect, segment, *, layout_rect=None):
        style, asset, warnings, count = super().resolve(page, rect, segment, layout_rect=layout_rect)
        rule = self.assignments.get(segment.segment_id)
        if rule is None:
            return style, asset, warnings, count
        style = style.model_copy(update={k: rule[k] for k in ("font_size", "bold", "italic", "color", "line_height")})
        asset = self.assets[(style.bold, style.italic)]
        if not self.supports(asset, segment.edited_translation):
            return style, None, [*warnings, "profile_missing_glyph"], count
        return style, asset, [*warnings, "document_profile_fixed_size"], count

    def html_and_css(self, text, style, asset, *, use_full_table_interior=False):
        html, css = super().html_and_css(text, style, asset,
            use_full_table_interior=use_full_table_interior)
        # MuPDF's default p margins outrank the universal selector. Reset the actual
        # elements so empty paragraph margins do not consume a fixed-size group's box.
        return html, css if use_full_table_interior else css + "body,p {margin:0;padding:0;}"


@dataclass(frozen=True)
class TypographyPilotArtifact:
    path: Path
    translation_plan: PdfExportPlan
    source_formatting: list[PdfReplacement]
    translated_written: int
    source_formatted: int
    profile_fingerprint: str
    groups: dict
    exceptions: dict


def _write_footprint(page, item, typography) -> list[pymupdf.Rect] | None:
    """Measure the writer's actual glyph boxes; blank HTML leading is not occupied ink.

    Redactions and patches remain occupied, even when no replacement glyph touches them.
    Failure to reproduce the accepted writer scale or text is not a geometric proof.
    """
    glyphs = exporter._replacement_glyph_rects(page, item, typography)
    if glyphs is None:
        return None
    redactions = item.redaction_rects if item.background_patch_used and item.redaction_rects else [item.original_bbox]
    patches = item.patch_rects if item.background_patch_used else []
    return [*[exporter._rect(rect) for rect in [*redactions, *patches]], *glyphs]


def _footprints_overlap(left, right) -> bool:
    return any(exporter._is_material_overlap(a & b) for a in left for b in right)



def _formatting_footprints_conflict(left, right, left_regions, right_regions) -> bool:
    """Only equal, proven-uniform background patches may overlap one another.

    Erasure rectangles and measured glyphs never gain this permission. All patches
    have already passed the ordinary uniform-background and expanded-area checks.
    """
    def split(item, regions):
        erasures = len(item.redaction_rects) if item.background_patch_used and item.redaction_rects else 1
        patches = len(item.patch_rects) if item.background_patch_used else 0
        return regions[:erasures] + regions[erasures + patches:], regions[erasures:erasures + patches]
    left_core, left_patches = split(left, left_regions)
    right_core, right_patches = split(right, right_regions)
    if (_footprints_overlap(left_core, right_regions) or _footprints_overlap(left_patches, right_core)):
        return True
    if not _footprints_overlap(left_patches, right_patches):
        return False
    return not (left.background_patch_used and right.background_patch_used
                and left.image_overlap_kind == right.image_overlap_kind == "simple_background"
                and left.background_color is not None and left.background_color == right.background_color)


def export_document_typography_pilot(
    path: Path, job: PdfExportJob, temp_dir: Path, *, plan_only: bool = False,
) -> TypographyPilotArtifact:
    """Use the ordinary planner/writer, with a separately authorized exact-source intent.

    No job statuses are changed. An unreviewed target is never passed to the writer.
    No cached plan is accepted; profile and every fit are recomputed for this call.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)
    before = job.model_dump_json()
    original_hash = exporter._sha256(path)
    formatting: list[PdfReplacement] = []
    with pymupdf.open(path) as document:
        exporter._validate_document_identity(path, document, job)
        typography = DocumentTypographyResolver(temp_dir, document, job)
        plan = exporter.plan_pdf_export(path, job, "replacement_preview", temp_dir, typography=typography)
        analyzer = PdfImageOverlapAnalyzer()
        authoritative = segment_pdf(path)
        cells = {}
        table_rects: dict[int, list[pymupdf.Rect]] = {}
        for page in authoritative.pages:
            for region in page.regions:
                for block in region.blocks:
                    table = block.table_provenance
                    if table is not None:
                        table_rects.setdefault(page.page_number, []).append(exporter._rect(table.bbox))
                        for cell in table.cells:
                            text = (block.table_cells or [])[cell.row_index][cell.column_index]
                            cells[cell.cell_id] = (page.page_number, table, cell, text)
        counts = Counter(s.table_cell_provenance.cell_id for s in job.segments if s.table_cell_provenance)
        containers = {(s.page_number, s.block_id) for s in job.segments if s.block_type == "table"}
        translated = {r.segment_id for r in [*plan.replacements, *plan.ocr_replacements] if r.eligible}
        for segment in job.segments:
            if segment.segment_id in translated or segment.segment_id not in typography.assignments:
                continue
            if not segment.source_text.strip():
                continue
            # Only the projection changes; original source, target, reviews and job are immutable.
            source = segment.model_copy(update={"edited_translation": segment.source_text})
            page = document[source.page_number - 1]
            if source.block_type != "table_cell" and any(
                exporter._is_material_overlap(exporter._rect(source.source_bbox) & bounds)
                for bounds in table_rects.get(source.page_number, [])
            ):
                typography.exceptions[source.segment_id] = "source_intersects_table_without_cell_ownership"
                continue
            if source.block_type == "table_cell":
                client = source.table_cell_provenance
                registered = cells.get(client.cell_id) if client else None
                if not registered or counts[client.cell_id] != 1 or (source.page_number, source.block_id) in containers:
                    typography.exceptions[source.segment_id] = "missing_or_duplicate_provenance"
                    continue
                pn, table, proof, text = registered
                if pn != source.page_number or not exporter._provenance_matches(client, table, proof):
                    typography.exceptions[source.segment_id] = "stale_table_provenance"
                    continue
                item = exporter._plan_provenance_cell(page, table, proof, text, source, typography, analyzer, source_formatting=True)
            else:
                item = exporter._plan_segment(document, source, typography, analyzer, source_formatting=True)
                if source.block_type == "list_item" and item.skip_reason is None:
                    source_lines = ["".join(span.get("text", "") for span in line.get("spans", []))
                        for block in page.get_text("dict", clip=exporter._rect(source.source_bbox)).get("blocks", [])
                        for line in block.get("lines", [])]
                    definitions = [line.partition("=") for line in source_lines]
                    # Separate, explicitly labelled symbol definitions are not prose
                    # to join onto one line. Retain their proven physical breaks.
                    if (len(definitions) > 1 and all(separator and len(label.strip()) == 1 for label, separator, _ in definitions)
                            and len({label.strip() for label, _, _ in definitions}) > 1
                            and exporter._written_text_matches(source.source_text, "\n".join(source_lines))):
                        item.layout_text = "\n".join(source_lines)
            formatting.append(item)
        candidates = [*plan.replacements, *plan.ocr_replacements, *formatting]
        exporter._mark_overlap_conflicts(formatting)
        for item in formatting:
            if item.skip_reason is not None:
                continue
            page = document[item.page_number - 1]
            if isinstance(item, PdfTableCellReplacement):
                exporter._fit_table_cell(page, item, typography, analyzer)
            else:
                exporter._fit_replacement(page, item, candidates, typography)
                if item.eligible and item.background_patch_used:
                    exporter._validate_expanded_patch_area(page, item, analyzer, typography)
        # A formatting attempt must not collide with either an approved translation or another formatting attempt.
        live = [r for r in [*plan.replacements, *plan.ocr_replacements, *formatting] if r.eligible]
        footprints = {id(r): _write_footprint(document[r.page_number - 1], r, typography) for r in live}
        for item in formatting:
            if not item.eligible:
                continue
            affected = footprints[id(item)]
            if affected is None:
                exporter._skip(item, "overlap_conflict", "Dejanskega območja oblikovnega zapisa ni mogoče potrditi.")
                continue
            for other in live:
                if other is item or other.page_number != item.page_number:
                    continue
                occupied = footprints[id(other)] or [exporter._rect(other.original_bbox), exporter._rect(other.fitted_bbox)]
                # Unknown writer geometry remains a hard obstacle.
                conflict = (_footprints_overlap(affected, occupied) if footprints[id(other)] is None
                            else _formatting_footprints_conflict(item, other, affected, occupied))
                if conflict:
                    exporter._skip(item, "overlap_conflict", "Oblikovanje se prekriva z drugim posegom.")
                    break
        written_translations = sum(r.eligible for r in [*plan.replacements, *plan.ocr_replacements])
        written_formatting = sum(r.eligible for r in formatting)
        unified = translated | {r.segment_id for r in formatting if r.eligible}
        attempts = {r.segment_id: r for r in formatting}
        groups = []
        for key, rule in typography.groups.items():
            members = [s for s in job.segments if typography.assignments.get(s.segment_id, {}).get("group") == key]
            reasons = Counter(
                typography.exceptions.get(s.segment_id)
                or (attempts[s.segment_id].skip_reason if s.segment_id in attempts else None)
                or "not_safely_formatted"
                for s in members if s.segment_id not in unified
            )
            count = sum(s.segment_id in unified for s in members)
            groups.append(PdfTypographyGroupCoverage(group=key, role=rule["role"],
                pages=sorted({s.page_number for s in members}), total=len(members), unified=count,
                partial=count != len(members), excluded_reasons=dict(reasons)))
        plan.typography_coverage = PdfTypographyCoverage(profile=PROFILE_VERSION,
            translated=written_translations, source_formatted=written_formatting,
            untouched=len(job.segments)-written_translations-written_formatting, groups=groups)
        if plan_only:
            return TypographyPilotArtifact(temp_dir / "not-written.pdf", plan, formatting,
                written_translations, written_formatting, typography.profile_fingerprint,
                typography.groups, typography.exceptions)
        combined = plan.model_copy(deep=True)
        combined.replacements.extend(formatting)
        combined.summary = exporter._summarize(combined.replacements)
        combined.resolution_summary = exporter._summarize_resolutions([*combined.replacements, *combined.ocr_replacements])
        measured_glyph_regions = {}
        for item in live:
            footprint = footprints[id(item)]
            if not item.eligible or footprint is None:
                continue
            redaction_count = len(item.redaction_rects) if item.background_patch_used and item.redaction_rects else 1
            patch_count = len(item.patch_rects) if item.background_patch_used else 0
            measured_glyph_regions[item.segment_id] = [exporter._bbox(rect) for rect in footprint[redaction_count + patch_count:]]
        total = exporter._write_replacements(document, combined, typography)
        if total != written_translations + written_formatting:
            raise exporter.PdfExportValidationError("output_replacement_count_mismatch", "Pilot ni zapisal vseh varnih posegov.")
        output = temp_dir / "document-typography-partial-pilot.pdf"
        document.subset_fonts()
        document.save(output, garbage=4, deflate=True)
    exporter._verify_inserted_text(output, combined, measured_glyph_regions=measured_glyph_regions)
    if job.model_dump_json() != before or exporter._sha256(path) != original_hash:
        raise RuntimeError("Typography pilot mutated its input")
    return TypographyPilotArtifact(output, plan, formatting, written_translations, written_formatting,
                                   typography.profile_fingerprint, typography.groups, typography.exceptions)
