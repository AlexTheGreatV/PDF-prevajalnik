from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.core.config import settings
from app.services.translation_memory_repository import (
    SCHEMA_VERSION,
    SqliteTranslationMemoryRepository,
    TranslationMemoryNotFoundError,
    TranslationMemoryRepositoryError,
    TranslationMemoryRevisionConflictError,
)
from app.translation_memory_models import (
    TranslationMemoryEntry,
    TranslationMemoryEntryCreate,
    TranslationMemoryEntryUpdate,
    TranslationMemoryExactLookupRequest,
    TranslationMemoryHistoryRecord,
    TranslationMemoryListResponse,
    TranslationMemoryLookupResponse,
    TranslationMemoryRevisionRequest,
    TranslationMemoryStats,
    TranslationMemoryStatusResponse,
    TranslationMemoryUsageRequest,
)

router = APIRouter()
_repository: SqliteTranslationMemoryRepository | None = None
_repository_lock = threading.Lock()


def get_translation_memory_repository() -> SqliteTranslationMemoryRepository:
    global _repository
    if _repository is None:
        with _repository_lock:
            if _repository is None:
                _repository = SqliteTranslationMemoryRepository(Path(settings.translation_memory_database_path))
    return _repository


@router.get("/status", response_model=TranslationMemoryStatusResponse)
def status(repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryStatusResponse:
    try:
        return TranslationMemoryStatusResponse(available=True, schema_version=repository.schema_version())
    except TranslationMemoryRepositoryError:
        return TranslationMemoryStatusResponse(available=False, message="Lokalni prevajalski spomin ni na voljo.")


@router.get("/entries", response_model=TranslationMemoryListResponse)
def list_entries(
    q: str | None = Query(default=None, max_length=200),
    source_language: str | None = Query(default=None, min_length=2, max_length=12),
    target_language: str | None = Query(default=None, min_length=2, max_length=12),
    domain: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, pattern="^(proposed|approved|rejected|archived)$"),
    limit: int = Query(default=200, ge=1, le=500), offset: int = Query(default=0, ge=0),
    repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository),
) -> TranslationMemoryListResponse:
    try:
        entries, total, active_hash = repository.list_entries_snapshot(
            query=q, source_language=source_language, target_language=target_language,
            domain=domain, status=status, limit=limit, offset=offset,
        )
        return TranslationMemoryListResponse(entries=entries, total=total, active_set_hash=active_hash)
    except TranslationMemoryRepositoryError as exc:
        raise _unavailable() from exc


@router.post("/entries", response_model=TranslationMemoryEntry, status_code=201)
def create_entry(payload: TranslationMemoryEntryCreate, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryEntry:
    try:
        return repository.create_entry(payload)
    except TranslationMemoryRepositoryError as exc:
        raise _unavailable() from exc


@router.put("/entries/{entry_id}", response_model=TranslationMemoryEntry)
def update_entry(entry_id: str, payload: TranslationMemoryEntryUpdate, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryEntry:
    try:
        return repository.update_entry(entry_id, payload)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/entries/{entry_id}/approve", response_model=TranslationMemoryEntry)
def approve(entry_id: str, payload: TranslationMemoryRevisionRequest, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "approved", "approve")


@router.post("/entries/{entry_id}/reject", response_model=TranslationMemoryEntry)
def reject(entry_id: str, payload: TranslationMemoryRevisionRequest, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "rejected", "reject")


@router.post("/entries/{entry_id}/archive", response_model=TranslationMemoryEntry)
def archive(entry_id: str, payload: TranslationMemoryRevisionRequest, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "archived", "archive")


@router.post("/entries/{entry_id}/restore", response_model=TranslationMemoryEntry)
def restore(entry_id: str, payload: TranslationMemoryRevisionRequest, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "proposed", "restore")


@router.get("/entries/{entry_id}/history", response_model=list[TranslationMemoryHistoryRecord])
def history(entry_id: str, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> list[TranslationMemoryHistoryRecord]:
    try:
        return repository.history(entry_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/lookup/exact", response_model=TranslationMemoryLookupResponse)
def exact_lookup(payload: TranslationMemoryExactLookupRequest, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryLookupResponse:
    try:
        return repository.exact_lookup(payload.source_text, payload.source_language, payload.target_language, payload.domain)
    except TranslationMemoryRepositoryError as exc:
        raise _unavailable() from exc


@router.post("/usage", status_code=204)
def record_usage(payload: TranslationMemoryUsageRequest, repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> Response:
    try:
        repository.record_usage(payload.entry_ids)
        return Response(status_code=204)
    except TranslationMemoryRepositoryError as exc:
        raise _unavailable() from exc


@router.get("/stats", response_model=TranslationMemoryStats)
def stats(repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> TranslationMemoryStats:
    try:
        return repository.stats()
    except TranslationMemoryRepositoryError as exc:
        raise _unavailable() from exc


@router.get("/export.json")
def export_json(repository: SqliteTranslationMemoryRepository = Depends(get_translation_memory_repository)) -> Response:
    try:
        return Response(
            json.dumps(repository.export_snapshot(), ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="translation-memory.json"'},
        )
    except TranslationMemoryRepositoryError as exc:
        raise _unavailable() from exc


def _transition(repository: SqliteTranslationMemoryRepository, entry_id: str, revision: int, status: str, action: str) -> TranslationMemoryEntry:
    try:
        return repository.transition(entry_id, revision, status, action)
    except Exception as exc:
        raise _http_error(exc) from exc


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TranslationMemoryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, TranslationMemoryRevisionConflictError):
        return HTTPException(status_code=409, detail={"message": str(exc), "current": exc.current.model_dump(mode="json")})
    return _unavailable()


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Lokalni prevajalski spomin ni na voljo.")
