import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from app.core.config import settings
from app.models import OcrResponse, PdfAnalysis, RenderResult
from app.pdf_export_models import ExportMode, PdfExportJob, PdfExportPlan
from app.services.ocr import run_selective_ocr
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_renderer import render_pdf_pages_as_data_urls
from app.services.pdf_exporter import PdfExportValidationError, export_translated_pdf, plan_pdf_export

router = APIRouter()
PDF_MAGIC = b"%PDF-"
UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_TRANSLATION_JOB_BYTES = 5 * 1024 * 1024


@router.post("/analyze", response_model=PdfAnalysis)
async def analyze(file: UploadFile = File(...)) -> PdfAnalysis:
    with _processing_tempdir() as temp_dir:
        path = await _save_upload(file, Path(temp_dir))
        try:
            return analyze_pdf(path, filename=file.filename)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Datoteka ni berljiv PDF.") from exc


@router.post("/render", response_model=RenderResult)
async def render(
    file: UploadFile = File(...),
    dpi: int = Form(150, ge=72, le=400),
    page_number: int | None = Form(None, ge=1),
) -> RenderResult:

    with _processing_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        path = await _save_upload(file, temp_path)
        try:
            return render_pdf_pages_as_data_urls(
                path=path,
                output_root=temp_path / "renders",
                filename=file.filename,
                dpi=dpi,
                pdftoppm_bin=settings.poppler_pdftoppm,
                page_number=page_number,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="PDF strani ni bilo mogoce renderirati.") from exc


@router.post("/ocr", response_model=OcrResponse)
async def ocr(file: UploadFile = File(...), language: str = Form("eng"), dpi: int = Form(180)) -> OcrResponse:
    if dpi < 100 or dpi > 240:
        raise HTTPException(status_code=400, detail="OCR DPI mora biti med 100 in 240.")

    with _processing_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        path = await _save_upload(file, temp_path)
        try:
            return run_selective_ocr(
                path,
                filename=file.filename or "upload.pdf",
                temp_root=temp_path / "ocr",
                language=language,
                dpi=dpi,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="OCR priprava PDF-ja ni uspela.") from exc


@router.post("/export-translated/plan", response_model=PdfExportPlan)
async def plan_translated_export(
    file: UploadFile = File(...),
    translation_job: str = Form(...),
    mode: ExportMode = Form(...),
) -> PdfExportPlan:
    job = _parse_export_job(translation_job)
    with _processing_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        path = await _save_upload(file, temp_path)
        try:
            return plan_pdf_export(path, job, mode, temp_path)
        except PdfExportValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Načrta izvoza PDF ni bilo mogoče pripraviti.") from exc


@router.post("/export-translated")
async def translated_export(
    file: UploadFile = File(...),
    translation_job: str = Form(...),
    mode: ExportMode = Form(...),
) -> Response:
    job = _parse_export_job(translation_job)
    with _processing_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        path = await _save_upload(file, temp_path)
        try:
            artifact = export_translated_pdf(path, job, mode, temp_path)
            content = artifact.path.read_bytes()
        except PdfExportValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Izvoza PDF ni bilo mogoče izdelati.") from exc
    encoded_name = quote(artifact.plan.filename)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


def _parse_export_job(value: str) -> PdfExportJob:
    if len(value.encode("utf-8")) > MAX_TRANSLATION_JOB_BYTES:
        raise HTTPException(status_code=413, detail="Prevajalsko opravilo je preveliko.")
    try:
        return PdfExportJob.model_validate_json(value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="Prevajalsko opravilo ni veljavno za izvoz PDF.") from exc


async def _save_upload(file: UploadFile, upload_dir: Path) -> Path:
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload.pdf").suffix or ".pdf"
    path = upload_dir / f"{uuid.uuid4().hex}{suffix}"
    bytes_written = 0

    if file.content_type not in {None, "", "application/pdf", "application/x-pdf"}:
        raise HTTPException(status_code=400, detail="Dovoljene so samo PDF datoteke.")

    header = await file.read(len(PDF_MAGIC))
    if header != PDF_MAGIC:
        raise HTTPException(status_code=400, detail="Datoteka nima veljavne PDF glave.")

    with path.open("wb") as output:
        output.write(header)
        bytes_written += len(header)

        while chunk := await file.read(UPLOAD_CHUNK_SIZE):
            bytes_written += len(chunk)
            if bytes_written > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="PDF je prevelik.")
            output.write(chunk)

    return path


@contextmanager
def _processing_tempdir() -> Iterator[str]:
    root = settings.processing_tmp_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    temp_dir = root / f"pdf-pipeline-{uuid.uuid4().hex}"
    temp_dir.mkdir()
    try:
        yield str(temp_dir)
    finally:
        _safe_rmtree(temp_dir, root)


def _safe_rmtree(target: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()

    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise RuntimeError("Refusing to remove a path outside the PDF processing temp root.")

    shutil.rmtree(resolved_target, ignore_errors=True)
