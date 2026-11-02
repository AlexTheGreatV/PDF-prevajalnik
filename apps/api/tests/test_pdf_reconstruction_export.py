"""Transport tests use synthetic reviews only, never real user approvals."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pymupdf
import pytest

from app.main import app
from app.models import BBox
from app.pdf_export_models import PdfExportSegment, AcceptedOcrSource
from app.services.ocr_review_registry import ocr_review_registry
from app.services.pdf_reconstruction_export import reconstruction_previews
from app.services import pdf_exporter as exporter
from app.services.pdf_reconstruction_export import _assert_outside, _outside_job
from app.services.pdf_table_reconstruction import (
    TableReconstructionError, TableReconstructionSelection, inspect_reconstruction_table, plan_table_reconstruction,
)
from app.services.pdf_typography import PdfTypographyResolver
from test_pdf_table_reconstruction import fixture


def post(source, job, selection, action="preview"):
    with TestClient(app) as client:
        return client.post(f"/api/pdf/export-translated/reconstruction/{action}",
            files={"file": ("synthetic.pdf", source.read_bytes(), "application/pdf")},
            data={"translation_job": job.model_dump_json(), "selection": json.dumps(selection)})


def setup(tmp_path):
    source = tmp_path / "source.pdf"
    job, selection = fixture(source, shared=True)
    with pymupdf.open(source) as doc:
        rect = doc[0].search_for("OUTSIDE 20 L")[0]
    job.segments.append(PdfExportSegment(segment_id="outside", block_id="outside", block_type="paragraph",
        page_number=1, source_text="OUTSIDE 20 L", edited_translation="ZUNAJ 20 L", status="reviewed",
        source_text_origin="native", source_page_type="digital", source_rotation=0,
        source_bbox=BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)))
    return source, job, {"page_number": 1, "table_id": selection.table_id}


@pytest.mark.parametrize("profile", ["original", "document-role-typography-pilot-v1"])
def test_combined_preview_excludes_selected_cells_from_ordinary_writer(tmp_path: Path, profile: str):
    source, job, selection = setup(tmp_path)
    job.typography_profile = profile
    before = job.model_dump_json()
    preview = post(source, job, selection)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["ready"] and data["cell_count"] == 8 and data["row_count"] == 4
    assert data["source_preview"].startswith("data:image/png;base64,")
    assert data["table_preview"].startswith("data:image/png;base64,")
    selected_ids = {s.segment_id for s in job.segments if s.table_cell_provenance}
    assert not selected_ids.intersection(r["segment_id"] for r in data["ordinary_plan"]["replacements"])
    assert data["counts"]["ordinary_replacements"] == 1
    assert data["counts"]["translated_units"] == 9
    response = post(source, job, {**selection, "token": data["token"]}, "download")
    assert response.status_code == 200, response.text
    assert json.loads(response.headers["x-pdf-reconstruction-counts"]) == data["counts"]
    assert response.headers["x-pdf-reconstruction-token"] == data["token"]
    with pymupdf.open(stream=response.content) as document:
        text = document[0].get_text()
        assert text.count("Lastnost") == 1
        assert "ZUNAJ 20 L" in text and "2,5 MPa" in text.replace("\u00a0", " ")
    assert job.model_dump_json() == before
    assert post(source, job, {**selection, "token": data["token"]}, "download").status_code == 400


@pytest.mark.parametrize("change", ["target", "source", "profile", "geometry", "selection", "status"])
def test_preview_cannot_authorize_changed_job_or_selection(tmp_path: Path, change: str):
    source, job, selection = setup(tmp_path)
    data = post(source, job, selection).json()
    if change == "target":
        job.segments[0].edited_translation = "Druga lastnost"
    elif change == "source":
        job.segments[0].source_text = "Fake"
    elif change == "profile":
        job.typography_profile = "document-role-typography-pilot-v1"
    elif change == "geometry":
        job.segments[0].source_bbox.x0 += 1
    elif change == "selection":
        selection["table_id"] = "another-table"
    else:
        job.segments[0].status = "translated"
    response = post(source, job, {**selection, "token": data["token"]}, "download")
    assert response.status_code == 400 and "preview_changed" in response.text
    assert response.headers.get("content-type") != "application/pdf"


@pytest.mark.parametrize("change", ["bbox", "missing", "unreviewed", "number", "unknown_table"])
def test_invalid_region_missing_content_and_failed_qc_never_produce_pdf(tmp_path: Path, change: str):
    source, job, selection = setup(tmp_path)
    if change == "bbox":
        selection["bbox"] = {"x0": 0, "y0": 0, "x1": 620, "y1": 260}
    elif change == "missing":
        job.segments.pop(0)
    elif change == "unreviewed":
        job.segments[0].status = "translated"
    elif change == "number":
        next(s for s in job.segments if s.source_text == "2.5 MPa").edited_translation = "2,6 MPa"
    else:
        selection["table_id"] = "not-present"
    response = post(source, job, selection)
    if change in {"missing", "unreviewed"}:
        assert response.status_code == 200 and not response.json()["ready"]
        assert response.json()["issues"] and response.json()["token"] is None
    else:
        assert response.status_code == 400


def test_expired_or_restarted_preview_is_rejected(tmp_path: Path):
    source, job, selection = setup(tmp_path)
    data = post(source, job, selection).json()
    reconstruction_previews.discard(data["token"])
    response = post(source, job, {**selection, "token": data["token"]}, "download")
    assert response.status_code == 400 and "preview_expired" in response.text


def test_alias_cannot_write_selected_content_through_ordinary_path(tmp_path: Path):
    source, job, selection = setup(tmp_path)
    alias = job.segments[0].model_copy(deep=True)
    alias.segment_id = "alias"
    alias.block_type = "paragraph"
    alias.table_cell_provenance = None
    job.segments.append(alias)
    response = post(source, job, selection)
    assert response.status_code == 400 and "ordinary_source_intersects_reconstruction:alias" in response.text


def _blank_layout_overlap(tmp_path: Path):
    source, job, _ = setup(tmp_path)
    table_id = job.segments[0].table_cell_provenance.table_id
    inventory = inspect_reconstruction_table(source, 1, table_id)
    selection = TableReconstructionSelection(enabled=True, source_sha256=inventory["source_sha256"],
        base_pdf_sha256=inventory["source_sha256"], page_number=1, table_id=table_id,
        inventory_sha256=inventory["inventory_sha256"])
    initial = plan_table_reconstruction(source, source, job, selection, tmp_path / "initial")
    work = tmp_path / "ordinary"
    ordinary = exporter.plan_pdf_export(source, _outside_job(job, initial), "replacement_preview", work)
    item = next(r for r in ordinary.replacements if r.segment_id == "outside")
    # A right-aligned caption: the box overlaps the table's right edge, while
    # the actual writer puts every glyph well to the right of the selection.
    item.alignment = item.typography.alignment = "right"
    exporter._accept_fit(item, pymupdf.Rect(270, 45, 590, 85), (0, 1, 1), "exact")
    base = tmp_path / "ordinary.pdf"
    with pymupdf.open(source) as document:
        assert exporter._write_replacements(document, ordinary, PdfTypographyResolver(work)) == 1
        document.save(base)
    exporter._verify_inserted_text(base, ordinary)
    return source, base, ordinary, initial, item


def test_blank_layout_overlap_requires_unchanged_saved_region(tmp_path: Path):
    source, base, ordinary, initial, item = _blank_layout_overlap(tmp_path)
    region = exporter._rect(initial.replacement_region)
    assert not (exporter._rect(item.fitted_bbox) & region).is_empty
    with pymupdf.open(source) as document:
        glyphs = exporter._replacement_glyph_rects(document[0], item, PdfTypographyResolver(tmp_path / "ordinary"))
        assert glyphs and all((g & region).is_empty for g in glyphs)
    _assert_outside(ordinary, initial, source=source, base_pdf=base)


@pytest.mark.parametrize("damage", ["original", "patch", "redaction", "missing_bbox", "missing_source",
    "missing_base", "unreadable_base", "glyph", "invisible_glyph", "pixels", "matrix"])
def test_layout_overlap_blocks_real_writes_and_missing_proof(tmp_path: Path, damage: str):
    source, base, ordinary, initial, item = _blank_layout_overlap(tmp_path)
    hit = BBox(x0=30, y0=60, x1=40, y1=70)
    if damage == "original":
        item.original_bbox = hit
    elif damage == "patch":
        item.patch_rects.append(hit)
    elif damage == "redaction":
        item.redaction_rects.append(hit)
    elif damage == "missing_bbox":
        item.fitted_bbox = None
    elif damage == "missing_source":
        source = None
    elif damage == "missing_base":
        base = None
    elif damage == "unreadable_base":
        base = tmp_path / "absent.pdf"
    else:
        changed = tmp_path / "changed.pdf"
        with pymupdf.open(base) as document:
            if damage in {"glyph", "invisible_glyph"}:
                document[0].insert_text((30, 80), "intrusion", fontsize=9,
                                        render_mode=3 if damage == "invisible_glyph" else 0)
            elif damage == "pixels":
                document[0].draw_rect((30, 75, 45, 80), color=None, fill=(1, 0, 0))
            else:
                document[0].set_rotation(180)
            document.save(changed)
        base = changed
    with pytest.raises(TableReconstructionError, match="ordinary_write_intersects_reconstruction:outside"):
        _assert_outside(ordinary, initial, source=source, base_pdf=base)


@pytest.mark.parametrize("expires", [False, True])
def test_ocr_connection_after_preview_is_rechecked_for_combined_download(tmp_path: Path, expires: bool):
    source, job, selection = setup(tmp_path)
    segment = job.segments[-1]
    revision = ocr_review_registry.register(document_sha256=job.document_identity.sha256,
        review_id="synthetic-outside", page_number=1, bbox=segment.source_bbox,
        text=segment.source_text, source="manual", reason="manual_selection")
    ocr_review_registry.accept(revision.revision_id)
    segment.source_text_origin = "accepted_ocr"
    segment.accepted_ocr_source = AcceptedOcrSource(revision_id=revision.revision_id,
        review_id=revision.review_id, bbox=segment.source_bbox, accepted_text=segment.source_text,
        source="manual", reason="manual_selection")
    data = post(source, job, selection).json()
    assert data["ready"] and data["ordinary_plan"]["ocr_summary"]["eligible_regions"] == 1
    if expires:
        ocr_review_registry.clear()
    response = post(source, job, {**selection, "token": data["token"]}, "download")
    if expires:
        assert response.status_code == 400 and "ordinary_plan_changed" in response.text
    else:
        assert response.status_code == 200, response.text
        with pymupdf.open(stream=response.content) as output:
            assert "ZUNAJ 20 L" in output[0].get_text()
