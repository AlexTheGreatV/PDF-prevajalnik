from pathlib import Path

import pymupdf
import pytest

from app.models import BBox
from app.pdf_export_models import PdfExportJob, PdfExportSegment
from app.services.pdf_analyzer import analyze_pdf
from app.services import pdf_exporter as exporter
from app.services.pdf_document_typography import DocumentTypographyResolver, export_document_typography_pilot
from app.services.pdf_image_overlap import PdfImageOverlapAnalyzer
from app.services.pdf_typography import PdfTypographyResolver
from fastapi.testclient import TestClient
from app.main import app
from app.services.pdf_document_typography import PROFILE_VERSION
from app.services import pdf_document_typography as document_typography


def test_ordinary_api_profile_plan_is_read_only_and_download_matches_counts(tmp_path: Path):
    source = tmp_path / "source.pdf"
    job = _fixture(source)
    job.typography_profile = PROFILE_VERSION
    before = job.model_dump_json()
    client = TestClient(app)
    data = {"translation_job": before, "mode": "replacement_preview"}
    files = {"file": (source.name, source.read_bytes(), "application/pdf")}
    planned = client.post("/api/pdf/export-translated/plan", data=data, files=files)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["summary"]["eligible"] == 0
    assert plan["typography_coverage"]["source_formatted"] >= 2
    result = client.post("/api/pdf/export-translated", data=data, files=files)
    assert result.status_code == 200, result.text
    assert result.headers["x-pdf-typography-profile"] == PROFILE_VERSION
    assert result.headers["x-pdf-typography-revision"] == plan["typography_revision"]
    assert result.headers["x-pdf-replacements-applied"] == "0"
    assert int(result.headers["x-pdf-source-formatted"]) == plan["typography_coverage"]["source_formatted"]
    with pymupdf.open(stream=result.content, filetype="pdf") as doc:
        assert "NEPOTRJEN OSNUTEK" not in doc[0].get_text()
        assert "Technical heading" in doc[0].get_text()
    assert job.model_dump_json() == before
    bad = client.post("/api/pdf/export-translated", data={**data, "mode": "diagnostic"}, files=files)
    assert bad.status_code == 400


def test_plan_only_typography_never_calls_writer(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.pdf"
    job = _fixture(source)
    def fail(*args, **kwargs):
        pytest.fail("A plan must never write a PDF")
    monkeypatch.setattr(exporter, "_write_replacements", fail)
    result = export_document_typography_pilot(source, job, tmp_path / "output", plan_only=True)
    assert not result.path.exists()
    assert result.translation_plan.typography_coverage.source_formatted >= 2


def _fixture(path: Path) -> PdfExportJob:
    with pymupdf.open() as doc:
        page = doc.new_page(width=500, height=500)
        page.insert_text((40, 60), "Technical heading", fontsize=14, fontname="hebo")
        page.insert_text((40, 100), "A short source paragraph.", fontsize=10)
        page.insert_text((40, 140), "Another source paragraph.", fontsize=10)
        doc.save(path)
    analysis = analyze_pdf(path)
    segments = []
    with pymupdf.open(path) as doc:
        for index, block in enumerate(doc[0].get_text("dict")["blocks"]):
            segments.append(PdfExportSegment(segment_id=f"s{index}",page_number=1,block_id=f"b{index}",
                block_type="heading" if index==0 else "paragraph",source_text="".join(s['text'] for l in block['lines'] for s in l['spans']),
                source_text_origin="native",source_page_type="digital",source_rotation=0,
                source_bbox=BBox(x0=block['bbox'][0],y0=block['bbox'][1],x1=block['bbox'][2],y1=block['bbox'][3]),
                edited_translation="NEPOTRJEN OSNUTEK",status="translated"))
    return PdfExportJob(schema_version=10,source_filename=path.name,segments=segments,document_identity={
        "sha256":analysis.sha256,"page_count":1,"document_model_version":analysis.document_model_version,
        "pages":[{"page_number":1,"width":500,"height":500,"rotation":0}]})


def test_typography_formats_exact_source_without_reviewing_or_using_unreviewed_targets(tmp_path: Path):
    source=tmp_path/'source.pdf'
    job=_fixture(source)
    before=job.model_dump_json()
    artifact=export_document_typography_pilot(source,job,tmp_path/'output')
    assert artifact.translated_written==0
    assert artifact.source_formatted>=2
    assert job.model_dump_json()==before
    assert all(s.status=='translated' for s in job.segments)
    with pymupdf.open(artifact.path) as doc:
        assert 'NEPOTRJEN OSNUTEK' not in doc[0].get_text()
        for s in job.segments:
            assert s.source_text in doc[0].get_text()
    for item in artifact.source_formatting:
        if item.eligible:
            assert item.source_text==item.translated_text and not item.reviewed
            assert item.fitted_font_size==pytest.approx(item.requested_font_size,abs=.01)


def test_profile_is_role_bound_deterministic_and_content_identity_changes_invalidate_fit(tmp_path: Path):
    source=tmp_path/'source.pdf';job=_fixture(source)
    for name in ('a','b','c'):(tmp_path/name).mkdir()
    with pymupdf.open(source) as doc:
        a=DocumentTypographyResolver(tmp_path/'a',doc,job)
        b=DocumentTypographyResolver(tmp_path/'b',doc,job)
        assert a.profile_fingerprint==b.profile_fingerprint
        assert a.assignments['s0']['group']!=a.assignments['s1']['group']
        assert a.assignments['s1']['group']==a.assignments['s2']['group']
        assert a.assignments['s0']['font_size']>a.assignments['s1']['font_size']
        changed=job.model_copy(deep=True)
        changed.document_identity.sha256='0'*64
        c=DocumentTypographyResolver(tmp_path/'c',doc,changed)
        assert c.profile_fingerprint!=a.profile_fingerprint
        changed=job.model_copy(deep=True)
        changed.segments[1].edited_translation='Spremenjen osnutek'
        d=DocumentTypographyResolver(tmp_path/'c',doc,changed)
        assert d.profile_fingerprint==a.profile_fingerprint
        assert d.typography_revision!=a.typography_revision


def _table_fixture(path: Path) -> PdfExportJob:
    with pymupdf.open() as doc:
        page=doc.new_page(width=400,height=180)
        for x in (30,200,370):page.draw_line((x,40),(x,68),width=.5)
        for y in (40,54,68):page.draw_line((30,y),(370,y),width=.5)
        for x,y,text in [(34,50,'Strength'),(204,50,'20 L'),(34,64,'Label'),(204,64,'1200')]:
            page.insert_text((x,y),text,fontsize=9)
        doc.save(path)
    analysis=analyze_pdf(path)
    block=next(b for p in analysis.document.pages for r in p.regions for b in r.blocks if b.table_provenance)
    table=block.table_provenance
    segments=[]
    for cell in table.cells:
        text=block.table_cells[cell.row_index][cell.column_index]
        segments.append(PdfExportSegment(segment_id=cell.cell_id,page_number=1,block_id="synthetic-table",
            block_type='table_cell',source_text=text,source_text_origin='native',source_page_type='digital',source_rotation=0,
            source_bbox=cell.source_bbox,table_cell_provenance={**cell.model_dump(),
                'table_id':table.table_id,'table_bbox':table.bbox,'table_geometry_evidence':table.geometry_evidence,
                'table_warnings':table.warnings},edited_translation='NEPOTRJENO',status='translated'))
    return PdfExportJob(schema_version=10,source_filename=path.name,segments=segments,document_identity={
        'sha256':analysis.sha256,'page_count':1,'document_model_version':analysis.document_model_version,
        'pages':[{'page_number':1,'width':400,'height':180,'rotation':0}]})


def test_source_table_values_preserve_content_graphics_and_group_size(tmp_path: Path):
    source=tmp_path/'table.pdf';job=_table_fixture(source)
    before=job.model_dump_json()
    artifact=export_document_typography_pilot(source,job,tmp_path/'output')
    assert artifact.translated_written==0 and artifact.source_formatted==4
    assert job.model_dump_json()==before
    with pymupdf.open(source) as original,pymupdf.open(artifact.path) as result:
        assert original[0].get_drawings()==result[0].get_drawings()
        for text in ('Strength','20 L','Label','1200'):
            assert text in result[0].get_text()
        assert 'NEPOTRJENO' not in result[0].get_text()
    assert {r.fitted_font_size for r in artifact.source_formatting if r.eligible}=={8.55}


@pytest.mark.parametrize('damage',['missing','ambiguous','bbox'])
def test_source_formatting_cannot_bypass_table_provenance(tmp_path: Path,damage: str):
    source=tmp_path/'table.pdf';job=_table_fixture(source)
    if damage=='missing':job.segments[0].table_cell_provenance=None
    elif damage=='ambiguous':job.segments[0].table_cell_provenance.ownership='ambiguous'
    else:job.segments[0].table_cell_provenance.cell_bbox.x1+=5
    artifact=export_document_typography_pilot(source,job,tmp_path/'output')
    assert not any(r.segment_id==job.segments[0].segment_id and r.eligible for r in artifact.source_formatting)


def test_table_values_misclassified_as_paragraph_are_not_reflowed(tmp_path: Path):
    source=tmp_path/'table.pdf';job=_table_fixture(source)
    first=job.segments[1]
    last=job.segments[3]
    projection=first.model_copy(update={'segment_id':'loose-values','block_id':'loose-values',
        'block_type':'paragraph','table_cell_provenance':None,'source_text':'20 L 1200',
        'source_bbox':BBox(x0=first.source_bbox.x0,y0=first.source_bbox.y0,x1=last.source_bbox.x1,y1=last.source_bbox.y1)})
    job.segments.append(projection)
    artifact=export_document_typography_pilot(source,job,tmp_path/'output')
    assert artifact.exceptions['loose-values']=='source_intersects_table_without_cell_ownership'
    assert not any(r.segment_id=='loose-values' for r in artifact.source_formatting)


def test_exact_source_intent_rejects_translation_and_ocr(tmp_path: Path):
    source=tmp_path/'source.pdf';job=_fixture(source)
    (tmp_path/'fonts').mkdir()
    resolver=PdfTypographyResolver(tmp_path/'fonts')
    with pymupdf.open(source) as doc:
        with pytest.raises(ValueError,match='exact native'):
            exporter._plan_segment(doc,job.segments[1],resolver,PdfImageOverlapAnalyzer(),source_formatting=True)
        ocr=job.segments[1].model_copy(update={'source_text_origin':'accepted_ocr','edited_translation':job.segments[1].source_text})
        with pytest.raises(ValueError,match='exact native'):
            exporter._plan_segment(doc,ocr,resolver,PdfImageOverlapAnalyzer(),source_formatting=True)


def test_profile_cannot_independently_shrink_long_reviewed_translation(tmp_path: Path):
    source=tmp_path/'source.pdf';job=_fixture(source)
    job.segments[1].status='reviewed'
    job.segments[1].edited_translation='Predolg prevod '*500
    artifact=export_document_typography_pilot(source,job,tmp_path/'output')
    assert not next(r for r in artifact.translation_plan.replacements if r.segment_id=='s1').eligible
    assert job.segments[1].status=='reviewed'
    with pymupdf.open(artifact.path) as doc:
        assert 'Predolg prevod' not in doc[0].get_text()
        assert job.segments[1].source_text in doc[0].get_text()


def test_blank_html_leading_is_not_a_write_collision_but_redactions_are(tmp_path: Path):
    source = tmp_path / 'source.pdf'
    job = _fixture(source)
    output = tmp_path / 'fonts'
    output.mkdir()
    with pymupdf.open(source) as doc:
        resolver = DocumentTypographyResolver(output, doc, job)
        items = []
        for segment in job.segments[1:]:
            source_only = segment.model_copy(update={'edited_translation': segment.source_text})
            item = exporter._plan_segment(doc, source_only, resolver, PdfImageOverlapAnalyzer(), source_formatting=True)
            items.append(item)
        first, second = items
        # This is a synthetic layout measurement, not approval of real translations.
        rect = pymupdf.Rect(40, first.original_bbox.y0, 350, second.original_bbox.y1 + 4)
        asset = exporter._asset_for(first, resolver)
        measurement = exporter._htmlbox_preflight(doc[0].rect, rect, first, resolver, asset, 1.0)
        assert measurement[0] >= 0
        exporter._accept_fit(first, rect, measurement, 'expanded_bbox')
        exporter._fit_replacement(doc[0], second, items, resolver)
        left = document_typography._write_footprint(doc[0], first, resolver)
        right = document_typography._write_footprint(doc[0], second, resolver)
        assert left and right
        assert exporter._is_material_overlap(exporter._rect(first.fitted_bbox) & exporter._rect(second.original_bbox))
        assert not document_typography._footprints_overlap(left, right)
        assert document_typography._footprints_overlap(left, [left[0]])
        # A background patch is occupied even when it contains only whitespace.
        first.background_patch_used = True
        first.patch_rects = [second.original_bbox]
        assert document_typography._footprints_overlap(document_typography._write_footprint(doc[0], first, resolver), right)
        first.background_patch_used = False
        first.patch_rects = []
        plan = exporter.plan_pdf_export(source, job, 'replacement_preview', output, typography=resolver)
        plan.replacements = items
        assert exporter._write_replacements(doc, plan, resolver) == 2
        result_path = tmp_path / 'measured-footprints.pdf'
        doc.save(result_path)
    exporter._verify_inserted_text(result_path, plan)
    with pymupdf.open(result_path) as result:
        text = result[0].get_text()
        for segment in job.segments:
            assert text.count(segment.source_text) == 1
        assert 'NEPOTRJEN OSNUTEK' not in text


def test_unmeasurable_formatting_footprint_fails_closed(tmp_path: Path, monkeypatch):
    source = tmp_path / 'source.pdf'
    job = _fixture(source)
    monkeypatch.setattr(document_typography, '_write_footprint', lambda *args: None)
    result = export_document_typography_pilot(source, job, tmp_path / 'output', plan_only=True)
    assert result.source_formatted == 0
    assert all(item.skip_reason == 'overlap_conflict' for item in result.source_formatting)


def test_fixed_group_paragraph_has_no_default_html_margins(tmp_path: Path):
    source = tmp_path / 'source.pdf'
    job = _fixture(source)
    fonts = tmp_path / 'fonts'
    fonts.mkdir()
    with pymupdf.open(source) as doc:
        profile = DocumentTypographyResolver(fonts, doc, job)
        source_only = job.segments[1].model_copy(update={'edited_translation': 'First line\nSecond line', 'status': 'reviewed'})
        item = exporter._plan_segment(doc, source_only, profile, PdfImageOverlapAnalyzer())
        item.typography = item.typography.model_copy(update={'font_size': 10.0, 'line_height': 1.2})
        asset = exporter._asset_for(item, profile)
        rect = pymupdf.Rect(40, 90, 220, 114.1)
        tight = exporter._htmlbox_preflight(doc[0].rect, rect, item, profile, asset, 1.0)
        ordinary = PdfTypographyResolver(fonts)
        default = exporter._htmlbox_preflight(doc[0].rect, rect, item, ordinary, asset, 1.0)
        assert tight[0] >= 0 and tight[1] == 1.0 and tight[2] == 2
        assert default[0] < 0
        assert item.typography.font_size == 10.0


def test_measured_glyph_verification_stays_exact_and_rejects_unrelated_regions(tmp_path: Path):
    source = tmp_path / 'source.pdf'
    job = _fixture(source)
    fonts = tmp_path / 'fonts'
    fonts.mkdir()
    plan = exporter.plan_pdf_export(source, job, 'replacement_preview', fonts)
    item = plan.replacements[1]
    item.eligible = True
    item.translated_text = job.segments[1].source_text
    item.fitted_bbox = job.segments[1].source_bbox
    item.layout_text = None
    measured = {item.segment_id: [job.segments[1].source_bbox]}
    exporter._verify_inserted_text(source, plan, measured_glyph_regions=measured)
    item.translated_text = 'A short source'
    with pytest.raises(exporter.PdfExportValidationError):
        exporter._verify_inserted_text(source, plan, measured_glyph_regions=measured)
    item.translated_text = job.segments[2].source_text
    with pytest.raises(exporter.PdfExportValidationError):
        exporter._verify_inserted_text(source, plan, measured_glyph_regions={item.segment_id:[job.segments[2].source_bbox]})


def test_profile_html_preserves_proven_source_text_inset(tmp_path: Path):
    source = tmp_path / 'source.pdf'
    job = _fixture(source)
    segment = job.segments[1]
    segment.source_bbox.x0 -= 4
    segment.status = 'reviewed'  # Synthetic fixture setup, not a real human review.
    segment.edited_translation = 'Prva vrstica\nDruga vrstica'
    result = export_document_typography_pilot(source, job, tmp_path / 'output')
    item = next(r for r in result.translation_plan.replacements if r.segment_id == segment.segment_id)
    assert item.eligible and item.scale == 1.0
    assert 'source_text_inset_preserved' in item.typography_warnings
    with pymupdf.open(result.path) as doc:
        lines = [line for block in doc[0].get_text('dict')['blocks'] for line in block.get('lines', [])
                 if any('Prva vrstica' in span.get('text','') for span in line['spans'])]
        assert len(lines) == 1
        assert lines[0]['spans'][0]['origin'][0] == pytest.approx(40, abs=.01)
        assert doc[0].search_for('Druga vrstica')


def test_source_symbol_definitions_keep_their_separate_physical_lines(tmp_path: Path):
    source = tmp_path / 'legend.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        page.insert_text((40, 100), '- = No use\nT = Test first', fontsize=8)
        doc.save(source)
    analysis = analyze_pdf(source)
    with pymupdf.open(source) as doc:
        bbox = doc[0].get_text('blocks')[0][:4]
    job = PdfExportJob(schema_version=10, source_filename=source.name,
        document_identity={'sha256':analysis.sha256,'page_count':1,'document_model_version':analysis.document_model_version,
            'pages':[{'page_number':1,'width':400,'height':300,'rotation':0}]},
        segments=[PdfExportSegment(segment_id='legend',page_number=1,block_id='legend',block_type='list_item',
            source_text='- = No use T = Test first',source_text_origin='native',source_page_type='digital',source_rotation=0,
            source_bbox=BBox(x0=bbox[0],y0=bbox[1],x1=bbox[2],y1=bbox[3]),edited_translation='UNREVIEWED',status='translated')])
    result = export_document_typography_pilot(source, job, tmp_path / 'output')
    assert result.source_formatted == 1 and result.translated_written == 0
    with pymupdf.open(result.path) as doc:
        text = doc[0].get_text()
        assert '- = No use\nT = Test first' in text
        assert 'UNREVIEWED' not in text
