import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.core.config import settings
from app.models import PdfAnalysis, RenderResult
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_renderer import render_pdf_pages_as_data_urls

router = APIRouter()
PDF_MAGIC = b"%PDF-"
UPLOAD_CHUNK_SIZE = 1024 * 1024


@router.post("/analyze", response_model=PdfAnalysis)
async def analyze(file: UploadFile = File(...)) -> PdfAnalysis:
    with _processing_tempdir() as temp_dir:
        path = await _save_upload(file, Path(temp_dir))
        try:
            return analyze_pdf(path, filename=file.filename)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Datoteka ni berljiv PDF.") from exc


@router.post("/render", response_model=RenderResult)
async def render(file: UploadFile = File(...), dpi: int = Form(150)) -> RenderResult:
    if dpi < 36 or dpi > 300:
        raise HTTPException(status_code=400, detail="DPI mora biti med 36 in 300.")

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
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="PDF strani ni bilo mogoce renderirati.") from exc


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
