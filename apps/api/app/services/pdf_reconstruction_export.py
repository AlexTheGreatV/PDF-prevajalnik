"""Bounded, single-use previews combining ordinary export with explicit reconstruction."""
from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass
import hashlib
import secrets
from pathlib import Path
from threading import Lock, Timer
import time

import pymupdf
from pydantic import BaseModel, ConfigDict, Field

from app.models import BBox
from app.pdf_export_models import PdfExportJob, PdfExportPlan
from app.services import pdf_exporter as exporter
from app.services.pdf_document_typography import export_document_typography_pilot
from app.services.pdf_table_reconstruction import (
    TableReconstructionError, TableReconstructionPlan, TableReconstructionSelection,
    export_table_reconstruction, inspect_reconstruction_table, plan_table_reconstruction,
)

PREVIEW_TTL = 30 * 60
MAX_PREVIEWS = 8
MAX_PREVIEW_BYTES = 64 * 1024 * 1024


class ReconstructionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    table_id: str = Field(min_length=1, max_length=200)
    token: str | None = Field(default=None, min_length=1, max_length=100)


@dataclass(frozen=True)
class Preview:
    job_sha256: str
    base: bytes
    plan: TableReconstructionPlan
    ordinary: PdfExportPlan
    expires: float


class ReconstructionPreviews:
    def __init__(self):
        self._items: dict[str, Preview] = {}
        self._timers: dict[str, Timer] = {}
        self._lock = Lock()

    def _prune(self):
        now = time.monotonic()
        for key in list(self._items):
            if self._items[key].expires <= now:
                self._items.pop(key)
                timer = self._timers.pop(key, None)
                if timer:
                    timer.cancel()

    def discard(self, token):
        with self._lock:
            self._items.pop(token, None)
            timer = self._timers.pop(token, None)
            if timer:
                timer.cancel()

    def add(self, value: Preview) -> str:
        with self._lock:
            self._prune()
            if len(self._items) >= MAX_PREVIEWS or sum(len(v.base) for v in self._items.values()) + len(value.base) > MAX_PREVIEW_BYTES:
                raise TableReconstructionError("preview_capacity: Znova poskusite po poteku predogledov.")
            token = secrets.token_urlsafe(32)
            self._items[token] = value
            timer = Timer(PREVIEW_TTL, self.discard, args=(token,))
            timer.daemon = True
            self._timers[token] = timer
            timer.start()
        return token

    def take(self, token: str | None) -> Preview:
        with self._lock:
            self._prune()
            value = self._items.pop(token, None)
            timer = self._timers.pop(token, None)
            if timer:
                timer.cancel()
        if value is None:
            raise TableReconstructionError("preview_expired: Predogled je potekel, je bil uporabljen ali je API ponovno zagnan.")
        return value


reconstruction_previews = ReconstructionPreviews()


def _job_hash(job: PdfExportJob) -> str:
    return hashlib.sha256(job.model_dump_json().encode()).hexdigest()


def _crop(path: Path, page_number: int, region: BBox) -> str:
    if (region.x1 - region.x0) * (region.y1 - region.y0) * 4 > 4_000_000:
        raise TableReconstructionError("preview_crop_too_large")
    with pymupdf.open(path) as doc:
        data = doc[page_number - 1].get_pixmap(matrix=pymupdf.Matrix(2, 2),
            clip=exporter._rect(region), alpha=False).tobytes("png")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _outside_job(job: PdfExportJob, plan: TableReconstructionPlan) -> PdfExportJob:
    selected = {c.segment_id for c in plan.cells if c.segment_id}
    outside = job.model_copy(deep=True)
    outside.segments = [s for s in outside.segments if s.segment_id not in selected]
    # Neither a second alias nor an ordinary write may reach into the selection.
    region = exporter._rect(plan.replacement_region)
    for segment in outside.segments:
        if (segment.page_number == plan.selection.page_number and segment.source_bbox
                and not (exporter._rect(segment.source_bbox) & region).is_empty):
            raise TableReconstructionError(f"ordinary_source_intersects_reconstruction:{segment.segment_id}")
    return outside


def _reserved_region_unchanged(source: Path | None, base_pdf: Path | None, reconstruction: TableReconstructionPlan) -> bool:
    """Prove that a saved ordinary export left the complete reserved region intact."""
    if source is None or base_pdf is None:
        return False
    region = exporter._rect(reconstruction.replacement_region)
    try:
        if exporter._sha256(source) != reconstruction.selection.source_sha256:
            return False
        with pymupdf.open(source) as before, pymupdf.open(base_pdf) as after:
            number = reconstruction.selection.page_number - 1
            if len(before) != len(after) or not 0 <= number < len(before):
                return False
            left, right = before[number], after[number]
            if (region.is_empty or region.is_infinite or region.width * region.height * 4 > 4_000_000
                    or not left.rect.contains(region)):
                return False
            for attribute in ("rect", "mediabox", "cropbox", "rotation", "transformation_matrix",
                              "rotation_matrix", "derotation_matrix"):
                if getattr(left, attribute) != getattr(right, attribute):
                    return False

            def characters(page):
                # Inspect whole-page character boxes, including boundary-crossing
                # and invisible text that a clipped extraction or raster can miss.
                return Counter((char["c"], tuple(char["origin"]), tuple(char["bbox"]))
                    for block in page.get_text("rawdict").get("blocks", [])
                    for line in block.get("lines", []) for span in line.get("spans", [])
                    for char in span.get("chars", []) if char["c"].strip()
                    and not (pymupdf.Rect(char["bbox"]) & region).is_empty)

            if characters(left) != characters(right):
                return False
            a = left.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=region, alpha=False)
            b = right.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=region, alpha=False)
            return (a.irect == b.irect and a.n == b.n and a.samples == b.samples)
    except (OSError, RuntimeError, ValueError, IndexError):
        return False


def _assert_outside(plan: PdfExportPlan, reconstruction: TableReconstructionPlan, formatting=(),
                    *, source: Path | None = None, base_pdf: Path | None = None):
    region = exporter._rect(reconstruction.replacement_region)
    layout_intersections = []
    for item in [*plan.replacements, *plan.ocr_replacements, *formatting]:
        if not item.eligible or item.page_number != reconstruction.selection.page_number:
            continue
        if item.original_bbox is None or item.fitted_bbox is None:
            raise TableReconstructionError(f"ordinary_write_intersects_reconstruction:{item.segment_id}")
        boxes = [item.original_bbox, *item.redaction_rects, *item.patch_rects]
        if any(box and not (exporter._rect(box) & region).is_empty for box in boxes):
            raise TableReconstructionError(f"ordinary_write_intersects_reconstruction:{item.segment_id}")
        if item.fitted_bbox and not (exporter._rect(item.fitted_bbox) & region).is_empty:
            layout_intersections.append(item.segment_id)
    if layout_intersections and not _reserved_region_unchanged(source, base_pdf, reconstruction):
        raise TableReconstructionError(f"ordinary_write_intersects_reconstruction:{','.join(layout_intersections)}")


def prepare_reconstruction_export(source: Path, job: PdfExportJob, request: ReconstructionRequest, work: Path) -> dict:
    if request.token is not None:
        raise TableReconstructionError("preview_must_not_supply_token")
    inventory = inspect_reconstruction_table(source, request.page_number, request.table_id)
    selection = TableReconstructionSelection(enabled=True, source_sha256=inventory["source_sha256"],
        base_pdf_sha256=inventory["source_sha256"], page_number=request.page_number,
        table_id=request.table_id, inventory_sha256=inventory["inventory_sha256"])
    region = BBox.model_validate(inventory["replacement_region"])
    original = _crop(source, request.page_number, region)
    issues = []
    for cell in inventory["table"]["cells"]:
        text = inventory["rows"][cell["row_index"]][cell["column_index"]]
        if not text.strip():
            continue
        matches = [s for s in job.segments if s.page_number == request.page_number and s.table_cell_provenance
                   and s.table_cell_provenance.cell_id == cell["cell_id"]]
        if len(matches) != 1:
            issues.append(f"{cell['cell_id']}: manjka enolično povezan prevod ({text}).")
        elif not (matches[0].status == "reviewed" and matches[0].edited_translation.strip()) and not (
                matches[0].status == "skipped" and exporter.is_technical_value(text)
                and matches[0].edited_translation in {"", text}):
            issues.append(f"{matches[0].segment_id}: prevod ni potrjen ali manjka ({text}).")
    if issues:
        return {"ready": False, "source_preview": original, "table_preview": None,
                "row_count": inventory["table"]["row_count"],
                "cell_count": sum(bool(t.strip()) for r in inventory["rows"] for t in r),
                "issues": issues, "token": None}
    # This validates every selected source, review, technical token and cell before exclusion.
    try:
        initial = plan_table_reconstruction(source, source, job, selection, work / "initial")
    except TableReconstructionError as exc:
        return {"ready": False, "source_preview": original, "table_preview": None,
                "row_count": inventory["table"]["row_count"], "cell_count": sum(bool(t.strip()) for r in inventory["rows"] for t in r),
                "issues": [str(exc)], "token": None}
    outside = _outside_job(job, initial)
    if job.typography_profile == "original":
        base = exporter.export_translated_pdf(source, outside, "replacement_preview", work / "ordinary")
        ordinary = base.plan
        _assert_outside(ordinary, initial, source=source, base_pdf=base.path)
    else:
        base = export_document_typography_pilot(source, outside, work / "ordinary")
        ordinary = base.translation_plan
        _assert_outside(ordinary, initial, base.source_formatting, source=source, base_pdf=base.path)
    selection = selection.model_copy(update={"base_pdf_sha256": exporter._sha256(base.path)})
    plan = plan_table_reconstruction(source, base.path, job, selection, work / "reconstruction")
    result = export_table_reconstruction(source, base.path, job, plan, work / "reconstruction")
    counts = reconstruction_counts(ordinary, plan)
    token = reconstruction_previews.add(Preview(_job_hash(job), base.path.read_bytes(), plan,
                                               ordinary, time.monotonic() + PREVIEW_TTL))
    return {"ready": True, "source_preview": original,
            "table_preview": _crop(result.path, request.page_number, region),
            "row_count": len(plan.row_heights), "cell_count": counts["reconstructed_cells"],
            "issues": [], "token": token, "expires_in_seconds": PREVIEW_TTL,
            "ordinary_plan": ordinary.model_dump(mode="json"), "counts": counts,
            "cells": [c.model_dump(mode="json") for c in plan.cells],
            "replacement_region": region.model_dump(), "inventory_sha256": selection.inventory_sha256}


def reconstruction_counts(ordinary: PdfExportPlan, plan: TableReconstructionPlan) -> dict:
    changed = sum(c.source_value == "reviewed_translation" for c in plan.cells)
    identical = sum(c.source_value == "identical_source" for c in plan.cells)
    technical = sum(c.source_value == "unchanged_technical_source" for c in plan.cells)
    normal = ordinary.resolution_summary
    return {"ordinary_replacements": normal.replacement_planned,
            "reconstructed_cells": sum(bool(c.source.strip()) for c in plan.cells),
            "reconstructed_translations": changed, "reconstructed_identical": identical,
            "reconstructed_technical": technical,
            "translated_units": normal.replacement_planned + changed,
            "preserved_identical": normal.preserved_identical + identical,
            "not_applicable": normal.not_applicable + technical,
            "blocked": normal.blocked_total,
            "total_units": normal.total_segments + changed + identical + technical}


def download_reconstruction_export(source: Path, job: PdfExportJob, request: ReconstructionRequest, work: Path):
    preview = reconstruction_previews.take(request.token)
    selection = preview.plan.selection
    if (_job_hash(job) != preview.job_sha256 or exporter._sha256(source) != selection.source_sha256
            or request.page_number != selection.page_number or request.table_id != selection.table_id):
        raise TableReconstructionError("preview_changed: Vsebina, izbor ali profil se je spremenil. Pripravite nov predogled.")
    # Replan ordinary export too: in particular an expired OCR registry must not be cached as valid.
    outside = _outside_job(job, preview.plan)
    if job.typography_profile == "original":
        current = exporter.plan_pdf_export(source, outside, "replacement_preview", work / "recheck")
    else:
        current = export_document_typography_pilot(source, outside, work / "recheck", plan_only=True).translation_plan
    if current.model_dump() != preview.ordinary.model_dump():
        raise TableReconstructionError("ordinary_plan_changed: Načrt ali OCR povezava ni več aktualna.")
    base = work / "server-base.pdf"
    base.write_bytes(preview.base)
    result = export_table_reconstruction(source, base, job, preview.plan, work / "final")
    return result, reconstruction_counts(current, preview.plan), current
