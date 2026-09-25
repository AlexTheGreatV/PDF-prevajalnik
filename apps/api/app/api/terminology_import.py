from __future__ import annotations

import codecs
import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from app.api.glossary import get_glossary_repository
from app.core.config import settings
from app.glossary_models import (
    TerminologyImportCommitResult,
    TerminologyImportConfirm,
    TerminologyImportOptions,
    TerminologyImportPreview,
)
from app.services.glossary_repository import GlossaryRepositoryError, SqliteGlossaryRepository
from app.services.terminology_import import (
    MAX_IMPORT_BYTES,
    TerminologyImportError,
    TerminologyImportLimitError,
    build_terminology_preview,
    commit_terminology_preview,
)

router = APIRouter()
CHUNK_SIZE = 64 * 1024


@router.post("/preview", response_model=TerminologyImportPreview)
async def preview_terminology_import(
    file: UploadFile = File(...),
    import_format: str = Form(..., pattern="^(csv|tbx|iate_csv)$"),
    options_json: str = Form(...),
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> TerminologyImportPreview:
    try:
        options = TerminologyImportOptions.model_validate_json(options_json)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Nastavitve uvoza niso veljavne: {exc}") from exc

    temp_root = Path(settings.processing_tmp_dir) / "terminology-import"
    temp_root.mkdir(parents=True, exist_ok=True)
    suffix = ".tbx" if import_format == "tbx" else ".csv"
    try:
        with tempfile.TemporaryDirectory(prefix="import-", dir=temp_root) as directory:
            path = Path(directory) / f"upload{suffix}"
            file_hash = await _copy_upload(file, path, reject_xml_declarations=import_format == "tbx")
            return build_terminology_preview(
                path,
                import_format=import_format,
                file_sha256=file_hash,
                options=options,
                repository=repository,
            )
    except TerminologyImportLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except TerminologyImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GlossaryRepositoryError as exc:
        raise HTTPException(status_code=503, detail="Trajni lokalni glosar ni na voljo.") from exc
    finally:
        await file.close()


@router.post("/confirm", response_model=TerminologyImportCommitResult)
def confirm_terminology_import(
    payload: TerminologyImportConfirm,
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> TerminologyImportCommitResult:
    try:
        return commit_terminology_preview(
            payload.preview_id,
            payload.file_sha256,
            payload.candidate_ids,
            repository,
        )
    except TerminologyImportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GlossaryRepositoryError as exc:
        raise HTTPException(status_code=503, detail="Uvoz ni uspel; v glosar ni bilo nič zapisano.") from exc


async def _copy_upload(file: UploadFile, destination: Path, *, reject_xml_declarations: bool) -> str:
    digest = hashlib.sha256()
    utf8_decoder = codecs.getincrementaldecoder("utf-8")()
    total = 0
    scan_tail = b""
    with destination.open("wb") as output:
        while chunk := await file.read(CHUNK_SIZE):
            total += len(chunk)
            if total > MAX_IMPORT_BYTES:
                raise TerminologyImportLimitError(f"Datoteka presega omejitev {MAX_IMPORT_BYTES // (1024 * 1024)} MB.")
            try:
                utf8_decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                raise TerminologyImportError("Uvozna datoteka mora biti veljavno kodirana v UTF-8.") from exc
            if reject_xml_declarations:
                scan = (scan_tail + chunk).upper()
                if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
                    raise TerminologyImportError("TBX z DTD ali ENTITY deklaracijo ni dovoljen.")
                scan_tail = scan[-32:]
            digest.update(chunk)
            output.write(chunk)
    if total == 0:
        raise TerminologyImportError("Uvozna datoteka je prazna.")
    try:
        utf8_decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise TerminologyImportError("Uvozna datoteka mora biti veljavno kodirana v UTF-8.") from exc
    return digest.hexdigest()
