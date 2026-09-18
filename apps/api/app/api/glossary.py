from __future__ import annotations

import csv
import io
import json
import re
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from app.core.config import settings
from app.glossary_models import (
    GlossaryEntry,
    GlossaryEntryCreate,
    GlossaryEntryUpdate,
    GlossaryHistoryRecord,
    GlossaryImportConfirm,
    GlossaryImportIssue,
    GlossaryImportPreview,
    GlossaryImportResult,
    GlossaryImportRow,
    GlossaryListResponse,
    GlossaryRevisionRequest,
    GlossaryStats,
    GlossaryStatusResponse,
    GlossaryUsageRequest,
)
from app.services.glossary_repository import (
    SCHEMA_VERSION,
    GlossaryNotFoundError,
    GlossaryReadOnlyError,
    GlossaryRepositoryError,
    GlossaryRevisionConflictError,
    SqliteGlossaryRepository,
)

router = APIRouter()
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 1_000
CSV_DATA_FIELDS = (
    "source_term", "target_term", "source_language", "target_language", "domain",
    "match_mode", "case_sensitive", "status", "origin", "note", "source_label", "document_hash",
)
CSV_FIELDS = CSV_DATA_FIELDS + ("formula_escape_version", "formula_escaped_fields")
FORMULA_ESCAPE_VERSION = "1"
_repository_lock = threading.Lock()
_repository: SqliteGlossaryRepository | None = None


def get_glossary_repository() -> SqliteGlossaryRepository:
    global _repository
    if _repository is None:
        with _repository_lock:
            if _repository is None:
                _repository = SqliteGlossaryRepository(Path(settings.glossary_database_path))
    return _repository


@router.get("/status", response_model=GlossaryStatusResponse)
def glossary_status(repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> GlossaryStatusResponse:
    try:
        return GlossaryStatusResponse(available=True, schema_version=repository.schema_version())
    except GlossaryRepositoryError:
        return GlossaryStatusResponse(available=False, message="Trajni lokalni glosar ni na voljo.")


@router.get("/entries", response_model=GlossaryListResponse)
def list_entries(
    q: str | None = Query(default=None, max_length=200),
    source_language: str | None = Query(default=None, min_length=2, max_length=12),
    target_language: str | None = Query(default=None, min_length=2, max_length=12),
    domain: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, pattern="^(proposed|approved|rejected|archived)$"),
    match_mode: str | None = Query(default=None, pattern="^(exact_segment|preferred_term|do_not_translate)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> GlossaryListResponse:
    try:
        entries, total = repository.list_entries(
            query=q, source_language=source_language, target_language=target_language,
            domain=domain, status=status, match_mode=match_mode, limit=limit, offset=offset,
        )
        return GlossaryListResponse(entries=entries, total=total, active_set_hash=repository.active_set_hash())
    except GlossaryRepositoryError as exc:
        raise _unavailable() from exc


@router.post("/entries", response_model=GlossaryEntry, status_code=201)
def create_entry(
    payload: GlossaryEntryCreate,
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> GlossaryEntry:
    try:
        return repository.create_entry(payload)
    except GlossaryRepositoryError as exc:
        raise _unavailable() from exc


@router.put("/entries/{entry_id}", response_model=GlossaryEntry)
def update_entry(
    entry_id: str,
    payload: GlossaryEntryUpdate,
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> GlossaryEntry:
    try:
        return repository.update_entry(entry_id, payload)
    except Exception as exc:
        raise _repository_http_error(exc) from exc


@router.post("/entries/{entry_id}/approve", response_model=GlossaryEntry)
def approve_entry(entry_id: str, payload: GlossaryRevisionRequest, repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> GlossaryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "approved", "approve")


@router.post("/entries/{entry_id}/reject", response_model=GlossaryEntry)
def reject_entry(entry_id: str, payload: GlossaryRevisionRequest, repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> GlossaryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "rejected", "reject")


@router.post("/entries/{entry_id}/archive", response_model=GlossaryEntry)
def archive_entry(entry_id: str, payload: GlossaryRevisionRequest, repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> GlossaryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "archived", "archive")


@router.post("/entries/{entry_id}/restore", response_model=GlossaryEntry)
def restore_entry(entry_id: str, payload: GlossaryRevisionRequest, repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> GlossaryEntry:
    return _transition(repository, entry_id, payload.expected_revision, "proposed", "restore")


@router.get("/entries/{entry_id}/history", response_model=list[GlossaryHistoryRecord])
def entry_history(entry_id: str, repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> list[GlossaryHistoryRecord]:
    try:
        return repository.history(entry_id)
    except Exception as exc:
        raise _repository_http_error(exc) from exc


@router.get("/stats", response_model=GlossaryStats)
def glossary_stats(repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> GlossaryStats:
    try:
        return repository.stats()
    except GlossaryRepositoryError as exc:
        raise _unavailable() from exc


@router.post("/usage", status_code=204)
def record_glossary_usage(
    payload: GlossaryUsageRequest,
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> Response:
    try:
        repository.record_usage(payload.entry_ids)
        return Response(status_code=204)
    except GlossaryRepositoryError as exc:
        raise _unavailable() from exc


@router.get("/export.json")
def export_json(repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> Response:
    try:
        entries, _ = repository.list_entries(limit=500, offset=0)
        payload = {"schema_version": 1, "entries": [entry.model_dump(mode="json") for entry in entries]}
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="glossary.json"'},
        )
    except GlossaryRepositoryError as exc:
        raise _unavailable() from exc


@router.get("/export.csv")
def export_csv(repository: SqliteGlossaryRepository = Depends(get_glossary_repository)) -> Response:
    try:
        entries, _ = repository.list_entries(limit=500, offset=0)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\r\n")
        writer.writeheader()
        for entry in entries:
            row = entry.model_dump(mode="json")
            csv_row: dict[str, object] = {}
            escaped_fields: list[str] = []
            for field in CSV_DATA_FIELDS:
                csv_row[field], escaped = _csv_safe(row.get(field))
                if escaped:
                    escaped_fields.append(field)
            csv_row["formula_escape_version"] = FORMULA_ESCAPE_VERSION
            csv_row["formula_escaped_fields"] = "|".join(escaped_fields)
            writer.writerow(csv_row)
        return Response(
            "\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="glossary.csv"'},
        )
    except GlossaryRepositoryError as exc:
        raise _unavailable() from exc


@router.post("/imports/csv/preview", response_model=GlossaryImportPreview)
async def preview_csv(
    file: UploadFile = File(...),
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> GlossaryImportPreview:
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="CSV je prevelik.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV mora biti kodiran kot UTF-8.") from exc
    return _preview_rows(text, repository)


@router.post("/imports/csv/confirm", response_model=GlossaryImportResult)
def confirm_csv(
    payload: GlossaryImportConfirm,
    repository: SqliteGlossaryRepository = Depends(get_glossary_repository),
) -> GlossaryImportResult:
    created = updated = skipped = 0
    for row in payload.rows:
        create = GlossaryEntryCreate.model_validate(row.model_dump(exclude={"row_number"}) | {"origin": "import"})
        matches = repository.find_same_source(create)
        duplicate = next((entry for entry in matches if _same_definition(entry, create)), None)
        if duplicate:
            skipped += 1
            continue
        if matches:
            if not payload.overwrite_conflicts:
                skipped += 1
                continue
            repository.import_overwrite(matches[0].id, matches[0].revision, create)
            updated += 1
            continue
        repository.create_entry(create, action="import")
        created += 1
    return GlossaryImportResult(created=created, updated=updated, skipped=skipped)


def _preview_rows(text: str, repository: SqliteGlossaryRepository) -> GlossaryImportPreview:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=422, detail="CSV nima glave.")
    unexpected = set(reader.fieldnames) - set(CSV_FIELDS)
    if unexpected:
        raise HTTPException(status_code=422, detail=f"CSV vsebuje nepričakovana polja: {', '.join(sorted(unexpected))}.")
    rows = list(reader)
    if len(rows) > MAX_IMPORT_ROWS:
        raise HTTPException(status_code=413, detail=f"CSV sme vsebovati največ {MAX_IMPORT_ROWS} vrstic.")
    valid: list[GlossaryImportRow] = []
    issues: list[GlossaryImportIssue] = []
    new_count = duplicate_count = conflict_count = invalid_count = override_count = 0
    for index, raw in enumerate(rows, start=2):
        try:
            escape_version = raw.get("formula_escape_version") or ""
            if escape_version not in {"", FORMULA_ESCAPE_VERSION}:
                raise ValueError("Različica zaščite CSV formul ni podprta.")
            escaped_fields = _csv_escaped_fields(raw, escape_version)
            restored = {
                field: _csv_restore(raw.get(field) or "", field, escaped_fields)
                for field in CSV_DATA_FIELDS
            }
            payload = GlossaryImportRow(
                row_number=index,
                source_term=restored["source_term"], target_term=restored["target_term"],
                source_language=restored["source_language"] or "en", target_language=restored["target_language"] or "sl",
                domain=restored["domain"], match_mode=restored["match_mode"] or "preferred_term",
                case_sensitive=_csv_bool(restored["case_sensitive"]), status=restored["status"] or "proposed",
                origin="import", note=restored["note"], source_label=restored["source_label"] or None,
                document_hash=restored["document_hash"] or None,
            )
        except (ValidationError, ValueError) as exc:
            invalid_count += 1
            issues.append(GlossaryImportIssue(row_number=index, kind="invalid", message=str(exc)))
            continue
        prior_matches = [existing for existing in valid if _same_import_source(existing, payload)]
        if any(_same_import_definition(existing, payload) for existing in prior_matches):
            duplicate_count += 1
            issues.append(GlossaryImportIssue(row_number=index, kind="duplicate", message="Popoln dvojnik je že v uvozni datoteki."))
            continue
        if prior_matches:
            conflict_count += 1
            issues.append(GlossaryImportIssue(row_number=index, kind="conflict", message="Uvozna datoteka vsebuje različni definiciji istega izraza."))
        matches = repository.find_same_source(payload)
        if any(_same_definition(entry, payload) for entry in matches):
            duplicate_count += 1
            issues.append(GlossaryImportIssue(row_number=index, kind="duplicate", message="Popoln dvojnik že obstaja.", existing_entry_id=matches[0].id))
            continue
        if matches:
            conflict_count += 1
            if any(entry.status == "approved" for entry in matches):
                override_count += 1
                kind = "override"
                message = "Vrstica bi preglasila obstoječi potrjeni vnos."
            else:
                kind = "conflict"
                message = "Za isti izvorni izraz obstaja drugačna definicija."
            issues.append(GlossaryImportIssue(row_number=index, kind=kind, message=message, existing_entry_id=matches[0].id))
        else:
            new_count += 1
        valid.append(payload)
    return GlossaryImportPreview(
        valid_rows=valid, new_count=new_count, duplicate_count=duplicate_count,
        conflict_count=conflict_count, invalid_count=invalid_count,
        override_count=override_count, issues=issues,
    )


def _transition(repository: SqliteGlossaryRepository, entry_id: str, revision: int, status: str, action: str) -> GlossaryEntry:
    try:
        return repository.transition(entry_id, revision, status, action)  # type: ignore[arg-type]
    except Exception as exc:
        raise _repository_http_error(exc) from exc


def _repository_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GlossaryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GlossaryRevisionConflictError):
        return HTTPException(status_code=409, detail={"message": str(exc), "current": exc.current.model_dump(mode="json")})
    if isinstance(exc, GlossaryReadOnlyError):
        return HTTPException(status_code=403, detail=str(exc))
    return _unavailable()


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Trajni lokalni glosar ni na voljo.")


def _csv_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "da"}:
        return True
    if normalized in {"0", "false", "no", "ne", ""}:
        return False
    raise ValueError("Neveljavna logična vrednost.")


def _csv_safe(value: object) -> tuple[object, bool]:
    if not isinstance(value, str):
        return value, False
    if _needs_formula_escape(value):
        return "'" + value, True
    return value, False


def _csv_escaped_fields(raw: dict[str, str | None], escape_version: str) -> set[str]:
    encoded = raw.get("formula_escaped_fields") or ""
    if not escape_version:
        if encoded:
            raise ValueError("CSV vsebuje oznake zaščitenih polj brez različice kodiranja.")
        return set()
    fields = set(encoded.split("|")) if encoded else set()
    if fields - set(CSV_DATA_FIELDS):
        raise ValueError("CSV vsebuje neveljavno oznako zaščitenega polja.")
    return fields


def _csv_restore(value: str, field: str, escaped_fields: set[str]) -> str:
    if field in escaped_fields:
        if not value.startswith("'") or not _needs_formula_escape(value[1:]):
            raise ValueError(f"Zaščita CSV polja {field} je poškodovana.")
        return value[1:]
    return value


def _needs_formula_escape(value: str) -> bool:
    if re.fullmatch(r"-\d+(?:[.,]\d+)?(?:\s*(?:°C|K|mm|cm|m|MPa|kPa|Pa|%|ml|L|kg|g|W|kW))?", value):
        return False
    return re.match(r"^[ \t\r\n]*[=+\-@]", value) is not None


def _same_definition(entry: GlossaryEntry, row: GlossaryEntryCreate) -> bool:
    return (
        entry.source_term == row.source_term and entry.target_term == row.target_term and
        entry.source_language == row.source_language and entry.target_language == row.target_language and
        entry.domain == row.domain and entry.match_mode == row.match_mode and
        entry.case_sensitive == row.case_sensitive and entry.status == row.status
    )


def _same_import_source(left: GlossaryImportRow, right: GlossaryImportRow) -> bool:
    case_sensitive = left.case_sensitive or right.case_sensitive
    left_term = left.source_term if case_sensitive else left.source_term.casefold()
    right_term = right.source_term if case_sensitive else right.source_term.casefold()
    return (
        left_term == right_term and left.source_language == right.source_language and
        left.target_language == right.target_language and left.domain == right.domain
    )


def _same_import_definition(left: GlossaryImportRow, right: GlossaryImportRow) -> bool:
    return _same_import_source(left, right) and (
        left.target_term == right.target_term and left.match_mode == right.match_mode and
        left.case_sensitive == right.case_sensitive and left.status == right.status
    )
