import json
import math
import shutil
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from collections.abc import AsyncIterator, Iterator
from typing import cast
from urllib.parse import quote

import pymupdf

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.models import OcrResponse, PdfAnalysis, RenderResult
from app.pdf_export_models import (
    ExportMode, OcrReviewAcceptance, OcrReviewRegistration,
    OcrReviewRegistrationResult, PdfExportJob, PdfExportPlan,
    OcrReviewVerifications,
)
from app.services.ocr import run_selective_ocr
from app.services.pdf_analyzer import analyze_pdf
from app.services.pdf_renderer import render_pdf_pages_as_data_urls
from app.services.pdf_exporter import ExportArtifact, PdfExportValidationError, export_translated_pdf, plan_pdf_export
from app.services.pdf_document_typography import export_document_typography_pilot
from app.services.ocr_review_registry import OCR_REVIEW_TTL_SECONDS, document_sha256, ocr_review_registry
from app.services.ocr_acceptance_repository import OcrAcceptanceStorageError
from app.services.pdf_table_reconstruction import TableReconstructionError
from app.services.pdf_reconstruction_export import (
    ReconstructionRequest, prepare_reconstruction_export, download_reconstruction_export,
)

router = APIRouter()
PDF_MAGIC = b"%PDF-"
UPLOAD_CHUNK_SIZE = 1024 * 1024


def _ocr_storage_unavailable(exc: OcrAcceptanceStorageError) -> HTTPException:
    return HTTPException(status_code=503, detail={"code": "ocr_storage_unavailable", "message": str(exc)})


@router.post("/export-translated/reconstruction/preview")
async def reconstruction_preview(request: Request):
    return await _reconstruction_request(request, download=False)


@router.post("/export-translated/reconstruction/download")
async def reconstruction_download(request: Request):
    return await _reconstruction_request(request, download=True)


async def _reconstruction_request(request: Request, *, download: bool):
    try:
        async with request.form(max_files=1, max_fields=2, max_part_size=settings.max_translation_job_bytes) as form:
            file = form.get("file")
            raw_job, raw_selection = form.get("translation_job"), form.get("selection")
            if not isinstance(file, StarletteUploadFile) or not isinstance(raw_job, str) or not isinstance(raw_selection, str):
                raise HTTPException(status_code=422, detail="Manjka PDF, opravilo ali izrecni izbor tabele.")
            job = _parse_export_job(raw_job)
            selection = ReconstructionRequest.model_validate_json(raw_selection)
            with _processing_tempdir() as directory:
                work = Path(directory)
                source = await _save_upload(file, work)
                if not download:
                    return prepare_reconstruction_export(source, job, selection, work)
                artifact, counts, _ = download_reconstruction_export(source, job, selection, work)
                return Response(content=artifact.path.read_bytes(), media_type="application/pdf", headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(Path(job.source_filename).stem + "-sl-delni-rekonstrukcija.pdf"),
                    "X-PDF-Export-Mode": "replacement_preview",
                    "X-PDF-Reconstruction-Token": selection.token,
                    "X-PDF-Reconstruction-Counts": json.dumps(counts, separators=(",", ":")),
                })
    except OcrAcceptanceStorageError as exc:
        raise _ocr_storage_unavailable(exc) from exc
    except (TableReconstructionError, PdfExportValidationError) as exc:
        raise HTTPException(status_code=400, detail={"code": getattr(exc, "code", "table_reconstruction"),
            "message": str(exc), **getattr(exc, "details", {})}) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="Izbor tabele ni veljaven; območje določi strežnik.") from exc


@router.post("/ocr-review/register", response_model=OcrReviewRegistrationResult)
async def register_ocr_review_source(file: UploadFile = File(...), source: str = Form(...)) -> OcrReviewRegistrationResult:
    try:
        candidate = OcrReviewRegistration.model_validate_json(source)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="OCR vir ni veljaven.") from exc
    with _processing_tempdir() as temp_dir:
        path = await _save_upload(file, Path(temp_dir))
        try:
            with pymupdf.open(path) as document:
                if candidate.page_number > document.page_count:
                    raise HTTPException(status_code=400, detail="OCR območje kaže na neobstoječo stran.")
                page = document[candidate.page_number - 1]
                rect = pymupdf.Rect(candidate.bbox.x0, candidate.bbox.y0, candidate.bbox.x1, candidate.bbox.y1)
                if not all(math.isfinite(value) for value in candidate.bbox.model_dump().values()) or rect.is_empty or rect.is_infinite or not page.rect.contains(rect):
                    raise HTTPException(status_code=400, detail="OCR območje ni znotraj preverjene strani PDF-ja.")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail="PDF-ja za OCR revizijo ni bilo mogoče preveriti.") from exc
        revision = ocr_review_registry.register(
            document_sha256=document_sha256(path.read_bytes()),
            review_id=candidate.review_id,
            page_number=candidate.page_number,
            bbox=candidate.bbox,
            text=candidate.text,
            source=candidate.source,
            reason=candidate.reason,
        )
    return OcrReviewRegistrationResult(
        revision_id=revision.revision_id, accepted=False, expires_in_seconds=OCR_REVIEW_TTL_SECONDS
    )


@router.post("/ocr-review/accept", response_model=OcrReviewRegistrationResult)
def accept_ocr_review_source(request: OcrReviewAcceptance) -> OcrReviewRegistrationResult:
    try:
        revision = ocr_review_registry.accept(request.revision_id)
    except OcrAcceptanceStorageError as exc:
        raise _ocr_storage_unavailable(exc) from exc
    if revision is None:
        raise HTTPException(status_code=409, detail="OCR revizija ne obstaja več ali ni aktualna.")
    return OcrReviewRegistrationResult(
        revision_id=revision.revision_id, accepted=True,
        expires_in_seconds=max(0, int(revision.expires_at - time.monotonic())),
    )


@router.post("/ocr-review/verify")
async def verify_ocr_review_sources(file: UploadFile = File(...), sources: str = Form(...)) -> dict:
    """Read existing acceptance only; never register, renew or approve client claims."""
    try:
        candidates = OcrReviewVerifications.model_validate_json(sources)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="OCR povezave niso veljavne.") from exc
    with _processing_tempdir() as directory:
        path = await _save_upload(file, Path(directory))
        sha = document_sha256(path.read_bytes())
        results = []
        for candidate in candidates.sources:
            try:
                reason = ocr_review_registry.validate(document_sha256=sha,
                    **candidate.model_dump(exclude={"bbox"}), bbox=candidate.bbox)
            except OcrAcceptanceStorageError as exc:
                raise _ocr_storage_unavailable(exc) from exc
            results.append({"revision_id": candidate.revision_id, "valid": reason is None, "reason": reason})
        return {"sources": results}


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
    request: Request,
) -> PdfExportPlan:
    async with _export_form(request) as (file, translation_job, mode):
        job = _parse_export_job(translation_job)
        with _processing_tempdir() as temp_dir:
            temp_path = Path(temp_dir)
            path = await _save_upload(file, temp_path)
            try:
                if job.typography_profile != "original":
                    if mode != "replacement_preview":
                        raise PdfExportValidationError("typography_mode", "Poenotena tipografija zahteva končni način izvoza.")
                    return export_document_typography_pilot(path, job, temp_path, plan_only=True).translation_plan
                return plan_pdf_export(path, job, mode, temp_path)
            except OcrAcceptanceStorageError as exc:
                raise _ocr_storage_unavailable(exc) from exc
            except PdfExportValidationError as exc:
                raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc), **exc.details}) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Načrta izvoza PDF ni bilo mogoče pripraviti.") from exc


@router.post("/export-translated")
async def translated_export(
    request: Request,
) -> Response:
    async with _export_form(request) as (file, translation_job, mode):
        job = _parse_export_job(translation_job)
        with _processing_tempdir() as temp_dir:
            temp_path = Path(temp_dir)
            path = await _save_upload(file, temp_path)
            try:
                if job.typography_profile != "original":
                    if mode != "replacement_preview":
                        raise PdfExportValidationError("typography_mode", "Poenotena tipografija zahteva končni način izvoza.")
                    pilot = export_document_typography_pilot(path, job, temp_path)
                    artifact = ExportArtifact(pilot.path, pilot.translation_plan, pilot.translated_written)
                else:
                    artifact = export_translated_pdf(path, job, mode, temp_path)
                content = artifact.path.read_bytes()
            except OcrAcceptanceStorageError as exc:
                raise _ocr_storage_unavailable(exc) from exc
            except PdfExportValidationError as exc:
                raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc), **exc.details}) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Izvoza PDF ni bilo mogoče izdelati.") from exc
    encoded_name = quote(artifact.plan.filename)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "X-PDF-Export-Mode": artifact.plan.mode,
            "X-PDF-Replacements-Applied": str(artifact.applied_replacements),
            "X-PDF-Typography-Profile": job.typography_profile,
            "X-PDF-Typography-Revision": artifact.plan.typography_revision or "original",
            "X-PDF-Source-Formatted": str(artifact.plan.typography_coverage.source_formatted if artifact.plan.typography_coverage else 0),
        },
    )


def _parse_export_job(value: str) -> PdfExportJob:
    if len(value.encode("utf-8")) > settings.max_translation_job_bytes:
        raise HTTPException(status_code=413, detail="Prevajalsko opravilo je preveliko.")
    try:
        return PdfExportJob.model_validate_json(value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="Prevajalsko opravilo ni veljavno za izvoz PDF.") from exc


@asynccontextmanager
async def _export_form(request: Request) -> AsyncIterator[tuple[StarletteUploadFile, str, ExportMode]]:
    try:
        form = await request.form(
            max_files=1,
            max_fields=2,
            max_part_size=settings.max_translation_job_bytes,
        )
    except StarletteHTTPException as exc:
        if exc.status_code == 400 and "exceeded maximum size" in str(exc.detail):
            raise HTTPException(status_code=413, detail="Prevajalsko opravilo je preveliko.") from exc
        raise HTTPException(status_code=400, detail="Multipart zahtevek za PDF izvoz ni veljaven.") from exc

    try:
        file = form.get("file")
        translation_job = form.get("translation_job")
        mode = form.get("mode")
        if not isinstance(file, StarletteUploadFile):
            raise HTTPException(status_code=422, detail="PDF datoteka manjka.")
        if not isinstance(translation_job, str):
            raise HTTPException(status_code=422, detail="Prevajalsko opravilo manjka.")
        if mode not in {"diagnostic", "replacement_preview"}:
            raise HTTPException(status_code=422, detail="Način PDF izvoza ni veljaven.")
        yield file, translation_job, cast(ExportMode, mode)
    finally:
        await cast(FormData, form).close()


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
