from pathlib import Path

import pymupdf
import pytest

from app.models import BBox
from app.pdf_export_models import PdfExportJob, PdfExportSegment, PdfTableCellProvenance
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_table_reconstruction import (
    TableReconstructionError,
    TableReconstructionSelection,
    export_table_reconstruction,
    inspect_reconstruction_table,
    plan_table_reconstruction,
)


def fixture(path: Path, *, shared=False, boundaries=True, fractional=False):
    rows = [('Property', 'Value'), ('Type', 'Polymer'), ('Weather resistance', 'Excellent'), ('Strength', '2.5 MPa')]
    with pymupdf.open() as document:
        width, height = (620.32, 260.48) if fractional else (620, 260)
        page = document.new_page(width=width, height=height)
        if fractional:
            background=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,10,10),False)
            background.clear_with(80)
            page.insert_image(page.rect,pixmap=background)
        if boundaries:
            for y in range(50, 155, 26):
                page.draw_line((20, y), (280, y), color=(0, 0, 0), width=0.4)
            for x in (20, 150, 280):
                page.draw_line((x, 50), (x, 154), color=(0, 0, 0), width=0.4)
        for row, values in enumerate(rows):
            for col, text in enumerate(values):
                page.insert_text((25+130*col, 67+26*row), text, fontsize=9)
        raster = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(20, 50, 280, 154), alpha=False).tobytes('png')
        xref = page.insert_image(pymupdf.Rect(20, 50, 280, 154), stream=raster)
        if shared:
            page.insert_image(pymupdf.Rect(330, 50, 590, 154), xref=xref)
        page.insert_text((330, 30), 'OUTSIDE 20 L', fontsize=10)
        document.save(path)
    analysis = analyze_pdf(path)
    blocks = [b for p in analysis.document.pages for r in p.regions for b in r.blocks if b.table_provenance]
    if not blocks:
        return None, None
    table = blocks[0].table_provenance
    segments = []
    targets = {'Property': 'Lastnost', 'Value': 'Vrednost', 'Type': 'Vrsta', 'Polymer': 'Polimer',
               'Weather resistance': 'Zanesljiva odpornost proti dolgotrajnim vremenskim vplivom',
               'Excellent': 'Odlično', 'Strength': 'Trdnost', '2.5 MPa': '2,5 MPa'}
    for cell in table.cells:
        text = blocks[0].table_cells[cell.row_index][cell.column_index]
        if not text:
            continue
        # Synthetic review setup; this does not approve any real document translation.
        segments.append(PdfExportSegment(segment_id=cell.cell_id, page_number=1, block_id='synthetic',
            block_type='table_cell', source_text=text, source_text_origin='native', source_bbox=cell.source_bbox,
            source_page_type='digital', source_rotation=0, edited_translation=targets[text], status='reviewed',
            table_cell_provenance=PdfTableCellProvenance(**cell.model_dump(), table_id=table.table_id,
                table_bbox=table.bbox, table_geometry_evidence=table.geometry_evidence, table_warnings=table.warnings)))
    identity = {'sha256': analysis.sha256, 'page_count':1, 'document_model_version':analysis.document_model_version,
                'pages':[{'page_number':1, 'width':width, 'height':height, 'rotation':0}]}
    job = PdfExportJob(schema_version=10,source_filename=path.name,document_identity=identity,segments=segments)
    inventory = inspect_reconstruction_table(path,1,table.table_id)
    selection = TableReconstructionSelection(enabled=True,source_sha256=analysis.sha256,
        base_pdf_sha256=analysis.sha256,page_number=1,table_id=table.table_id,inventory_sha256=inventory['inventory_sha256'])
    return job, selection


@pytest.mark.parametrize('shared,fractional', [(False,False),(True,False),(True,True)])
def test_raster_table_rebuild_preserves_shared_images_and_outside_content(tmp_path: Path, shared: bool, fractional: bool):
    source = tmp_path/'source.pdf'
    job, selection = fixture(source,shared=shared,fractional=fractional)
    original_job = job.model_dump_json()
    plan = plan_table_reconstruction(source,source,job,selection,tmp_path/'work')
    long = next(c for c in plan.cells if c.source=='Weather resistance')
    assert len(long.lines) > 1
    assert long.bbox.y1-long.bbox.y0 > min(plan.row_heights)
    assert {c.font_size for c in plan.cells if c.role=='body'} == {9.0}
    assert all(c.role=='header' for c in plan.cells if c.row==0)
    result = export_table_reconstruction(source,source,job,plan,tmp_path/'work')
    assert job.model_dump_json()==original_job
    assert result.verification['cells_verified']==8
    assert result.verification['image_streams_and_outside_placements_retained']
    assert result.verification['outside_native_characters_and_positions_retained']
    assert all(p['outside_changed_pixels']==0 for p in result.verification['outside_pages'])
    with pymupdf.open(result.path) as document:
        table_text = document[0].get_text(clip=pymupdf.Rect(tuple(plan.replacement_region.model_dump().values())))
        assert 'Weather resistance' not in table_text
        assert 'Zanesljiva odpornost' in table_text
        assert '2,5 MPa' in table_text.replace('\u00a0', ' ')
        assert 'OUTSIDE 20 L' in document[0].get_text()
        grid=next(d for d in document[0].get_drawings() if d['type']=='s' and len(d['items'])==8)
        edges=[tuple((item[0],tuple(item[1]),tuple(item[2]))) for item in grid['items']]
        assert len(set(edges))==8
        assert grid['width']==pytest.approx(.3)
        for cell in plan.cells:
            assert document[0].get_text(clip=pymupdf.Rect(tuple(cell.bbox.model_dump().values()))).strip()


@pytest.mark.parametrize('damage', ['unselected','missing','unreviewed','number','stale_inventory','stale_plan'])
def test_reconstruction_rejects_incomplete_unreviewed_or_changed_content(tmp_path: Path, damage: str):
    source = tmp_path/'source.pdf'
    job, selection = fixture(source)
    plan = plan_table_reconstruction(source,source,job,selection,tmp_path/'work')
    if damage=='unselected':
        selection = selection.model_copy(update={'enabled':False})
    elif damage=='missing':
        job.segments.pop()
    elif damage=='unreviewed':
        job.segments[0].status='translated'
    elif damage=='number':
        job.segments[-1].edited_translation='2,6 MPa'
    elif damage=='stale_inventory':
        selection = selection.model_copy(update={'inventory_sha256':'0'*64})
    else:
        plan = plan.model_copy(update={'row_heights':(1.0,)*4})
    with pytest.raises(ValueError):
        if damage=='stale_plan':
            export_table_reconstruction(source,source,job,plan,tmp_path/'work')
        else:
            plan_table_reconstruction(source,source,job,selection,tmp_path/'work')
    assert not (tmp_path/'work/table-reconstruction-pilot.pdf').exists()


def test_unbounded_or_unaccounted_table_content_is_rejected(tmp_path: Path):
    source = tmp_path/'open.pdf'
    with pymupdf.open() as document:
        page=document.new_page(width=300,height=200)
        page.insert_text((30,50),'Property  Value\nStrength  2.5 MPa')
        document.save(source)
    with pytest.raises(TableReconstructionError):
        inspect_reconstruction_table(source,1,'p1-t1')


@pytest.mark.parametrize('gap', [0, 3])
def test_rowwise_outer_border_requires_connected_coverage(gap: int):
    from app.services.pdf_table_reconstruction import _closed_outer_border
    with pymupdf.open() as doc:
        page=doc.new_page(width=300,height=200)
        for y in (30,130):page.draw_line((20,y),(280,y))
        for x in (20,280):
            page.draw_line((x,30),(x,80-gap))
            page.draw_line((x,80),(x,130))
        assert _closed_outer_border(page,pymupdf.Rect(20,30,280,130)) is (gap==0)


def test_skipped_technical_value_is_copied_without_approval(tmp_path: Path):
    source=tmp_path/'source.pdf'
    job, selection=fixture(source)
    value=next(s for s in job.segments if s.source_text=='2.5 MPa')
    value.status='skipped'
    value.edited_translation=''
    before=job.model_dump_json()
    plan=plan_table_reconstruction(source,source,job,selection,tmp_path/'work')
    cell=next(c for c in plan.cells if c.segment_id==value.segment_id)
    assert cell.target=='2.5 MPa' and cell.source_value=='unchanged_technical_source'
    assert job.model_dump_json()==before


def test_complete_table_overflow_is_not_solved_by_per_cell_shrinking(tmp_path: Path):
    source=tmp_path/'source.pdf'
    job, selection=fixture(source)
    job.segments[0].edited_translation='Preverjena obsežna lastnost '*100
    with pytest.raises(TableReconstructionError,match='complete_table_overflow'):
        plan_table_reconstruction(source,source,job,selection,tmp_path/'work')
