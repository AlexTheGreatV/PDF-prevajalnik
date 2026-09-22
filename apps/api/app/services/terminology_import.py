from __future__ import annotations

import csv
import hashlib
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from app.glossary_models import (
    GlossaryEntry,
    TerminologyImportCandidate,
    TerminologyImportCommitResult,
    TerminologyImportIssue,
    TerminologyImportOptions,
    TerminologyImportPreview,
)
from app.services.glossary_repository import SqliteGlossaryRepository

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 1_000
MAX_FIELD_CHARS = 2_000
PREVIEW_TTL = timedelta(minutes=30)
MAX_ACTIVE_PREVIEWS = 16
MAX_PREVIEW_REGISTRY_BYTES = 8 * 1024 * 1024
SUPPORTED_FORMATS = {"csv", "tbx"}
SUPPORTED_TBX_TAGS = {
    "tbx", "martif", "text", "body", "termEntry", "langSet", "tig", "ntig",
    "termGrp", "term", "termNote", "descrip", "descripGrp", "admin", "adminGrp", "note",
}
csv.field_size_limit(MAX_IMPORT_BYTES)


class TerminologyImportError(ValueError):
    pass


class TerminologyImportLimitError(TerminologyImportError):
    pass


@dataclass(frozen=True)
class StoredTerminologyPreview:
    preview: TerminologyImportPreview
    candidates: tuple[TerminologyImportCandidate, ...]
    options_sha256: str
    size_bytes: int


class TerminologyPreviewRegistry:
    def __init__(self) -> None:
        self._items: dict[str, StoredTerminologyPreview] = {}
        self._lock = threading.Lock()

    def put(self, value: StoredTerminologyPreview) -> None:
        with self._lock:
            self._remove_expired()
            current_bytes = sum(item.size_bytes for item in self._items.values())
            if len(self._items) >= MAX_ACTIVE_PREVIEWS:
                raise TerminologyImportLimitError("Doseženo je največje število aktivnih predogledov. Dokončajte ali počakajte na potek obstoječega predogleda.")
            if current_bytes + value.size_bytes > MAX_PREVIEW_REGISTRY_BYTES:
                raise TerminologyImportLimitError("Aktivni terminološki predogledi presegajo dovoljeno skupno velikost.")
            self._items[value.preview.preview_id] = value

    def get(self, preview_id: str) -> StoredTerminologyPreview:
        with self._lock:
            self._remove_expired()
            value = self._items.get(preview_id)
            if value is None:
                raise TerminologyImportError("Predogled uvoza ne obstaja ali je potekel. Datoteko ponovno preglejte.")
            return value

    def remove(self, preview_id: str) -> None:
        with self._lock:
            self._items.pop(preview_id, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def usage(self) -> tuple[int, int]:
        with self._lock:
            self._remove_expired()
            return len(self._items), sum(item.size_bytes for item in self._items.values())

    def _remove_expired(self) -> None:
        now = datetime.now(UTC)
        expired = [key for key, value in self._items.items() if value.preview.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


preview_registry = TerminologyPreviewRegistry()


def build_terminology_preview(
    path: Path,
    *,
    import_format: str,
    file_sha256: str,
    options: TerminologyImportOptions,
    repository: SqliteGlossaryRepository,
) -> TerminologyImportPreview:
    if import_format not in SUPPORTED_FORMATS:
        raise TerminologyImportError("Podprti sta samo obliki CSV in TBX.")
    if import_format == "csv":
        if options.csv_mapping is None:
            raise TerminologyImportError("Za CSV je obvezna eksplicitna preslikava stolpcev.")
        candidates, issues, ignored = _parse_csv(path, options)
    else:
        candidates, issues, ignored = _parse_tbx(path, options)
    classified, classification_issues = _classify(candidates, repository)
    issues.extend(classification_issues)
    counts = {kind: sum(item.classification == kind for item in classified) for kind in ("new", "duplicate", "conflict", "invalid")}
    parsed_invalid_count = sum(item.kind in {"invalid", "ambiguous"} for item in issues)
    preview_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    preview = TerminologyImportPreview(
        preview_id=preview_id,
        import_batch_id=batch_id,
        expires_at=now + PREVIEW_TTL,
        format=import_format,
        file_sha256=file_sha256,
        candidates=classified,
        valid_count=sum(item.classification != "invalid" for item in classified),
        new_count=counts["new"], duplicate_count=counts["duplicate"],
        conflict_count=counts["conflict"], invalid_count=counts["invalid"] + parsed_invalid_count,
        missing_translation_count=sum(item.kind == "missing_translation" for item in issues),
        missing_language_count=sum(item.kind == "missing_language" for item in issues),
        issues=issues,
        domains=sorted({item.domain for item in classified if item.domain}),
        ignored_fields=sorted(ignored),
        external_source=options.external_source,
        source_version=options.source_version,
        license_info=options.license_info,
    )
    options_bytes = options.model_dump_json(exclude_none=False).encode("utf-8")
    preview_size = len(preview.model_dump_json().encode("utf-8")) + len(options_bytes)
    preview_registry.put(StoredTerminologyPreview(
        preview=preview,
        candidates=tuple(classified),
        options_sha256=hashlib.sha256(options_bytes).hexdigest(),
        size_bytes=preview_size,
    ))
    return preview


def commit_terminology_preview(
    preview_id: str,
    file_sha256: str,
    candidate_ids: list[str],
    repository: SqliteGlossaryRepository,
) -> TerminologyImportCommitResult:
    stored = preview_registry.get(preview_id)
    if stored.preview.file_sha256 != file_sha256:
        raise TerminologyImportError("SHA-256 datoteke se ne ujema s pregledanim uvozom.")
    selected_ids = set(candidate_ids)
    if len(selected_ids) != len(candidate_ids):
        raise TerminologyImportError("Izbor kandidatov vsebuje podvojene identifikatorje.")
    known = {item.candidate_id: item for item in stored.candidates}
    unknown = selected_ids - known.keys()
    if unknown:
        raise TerminologyImportError("Izbor vsebuje neznanega kandidata.")
    selected = [known[item_id] for item_id in candidate_ids]
    if any(item.classification in {"duplicate", "invalid"} for item in selected):
        raise TerminologyImportError("Dvojnikov in neveljavnih zapisov ni mogoče uvoziti.")
    created, skipped_duplicates, skipped_conflicts = repository.import_candidates(
        selected,
        import_batch_id=stored.preview.import_batch_id,
        file_sha256=stored.preview.file_sha256,
    )
    preview_registry.remove(preview_id)
    return TerminologyImportCommitResult(
        import_batch_id=stored.preview.import_batch_id,
        created=created,
        skipped_duplicates=skipped_duplicates,
        skipped_conflicts=skipped_conflicts,
        skipped_unselected=len(stored.candidates) - len(selected),
    )


def _parse_csv(
    path: Path,
    options: TerminologyImportOptions,
) -> tuple[list[TerminologyImportCandidate], list[TerminologyImportIssue], set[str]]:
    try:
        return _parse_csv_utf8(path, options)
    except UnicodeDecodeError as exc:
        raise TerminologyImportError("CSV mora biti veljavno kodiran v UTF-8.") from exc
    except csv.Error as exc:
        raise TerminologyImportError(f"CSV ni veljaven: {exc}.") from exc


def _parse_csv_utf8(
    path: Path,
    options: TerminologyImportOptions,
) -> tuple[list[TerminologyImportCandidate], list[TerminologyImportIssue], set[str]]:
    mapping = options.csv_mapping
    assert mapping is not None
    candidates: list[TerminologyImportCandidate] = []
    issues: list[TerminologyImportIssue] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise TerminologyImportError("CSV nima glave.")
        headers = set(reader.fieldnames)
        mapped = {value for value in mapping.model_dump().values() if value}
        missing = mapped - headers
        if missing:
            raise TerminologyImportError(f"CSV nima preslikanih stolpcev: {', '.join(sorted(missing))}.")
        ignored = headers - mapped
        for row_number, row in enumerate(reader, start=2):
            if row_number - 1 > MAX_IMPORT_ROWS:
                raise TerminologyImportLimitError(f"Uvoz sme vsebovati največ {MAX_IMPORT_ROWS} zapisov.")
            try:
                if any(len(value or "") > MAX_FIELD_CHARS for value in row.values()):
                    raise TerminologyImportError("CSV vsebuje polje, daljše od 2000 znakov.")
                source = _csv_value(row, mapping.source_term)
                target = _csv_value(row, mapping.target_term)
                source_language = _csv_value(row, mapping.source_language) or options.source_language
                target_language = _csv_value(row, mapping.target_language) or options.target_language
                if not source_language or not target_language:
                    issues.append(_issue(row_number, "missing_language", "Manjka izvorni ali ciljni jezik."))
                    continue
                if not target:
                    issues.append(_issue(row_number, "missing_translation", "Manjka ciljni termin."))
                    continue
                if not source:
                    issues.append(_issue(row_number, "invalid", "Manjka izvorni termin."))
                    continue
                candidate = _candidate(
                    row_number=row_number, source_term=source, target_term=target,
                    source_language=source_language, target_language=target_language,
                    domain=_csv_value(row, mapping.domain) or options.default_domain,
                    match_mode=_csv_value(row, mapping.match_mode) or options.default_match_mode,
                    external_source=options.external_source,
                    external_record_id=_csv_value(row, mapping.external_record_id) or None,
                    source_confidence=_csv_value(row, mapping.confidence) or None,
                    definition=_csv_value(row, mapping.definition) or None,
                    context=_csv_value(row, mapping.context) or None,
                    note=_csv_value(row, mapping.note),
                    source_version=_csv_value(row, mapping.source_version) or options.source_version,
                    license_info=options.license_info,
                )
                candidates.append(candidate)
            except (ValidationError, TerminologyImportError, ValueError) as exc:
                issues.append(_issue(row_number, "invalid", str(exc)))
    return candidates, issues, ignored


def _parse_tbx(
    path: Path,
    options: TerminologyImportOptions,
) -> tuple[list[TerminologyImportCandidate], list[TerminologyImportIssue], set[str]]:
    candidates: list[TerminologyImportCandidate] = []
    issues: list[TerminologyImportIssue] = []
    ignored: set[str] = set()
    entry_number = 0
    root_checked = False
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            tag = _local_name(element.tag)
            if event == "start":
                if not root_checked:
                    root_checked = True
                    if tag not in {"tbx", "martif"}:
                        raise TerminologyImportError("XML nima podprtega korenskega elementa TBX ali martif.")
                if tag not in SUPPORTED_TBX_TAGS:
                    ignored.add(tag)
                continue
            if tag != "termEntry":
                continue
            entry_number += 1
            if entry_number > MAX_IMPORT_ROWS:
                raise TerminologyImportLimitError(f"Uvoz sme vsebovati največ {MAX_IMPORT_ROWS} zapisov.")
            terms: dict[str, list[str]] = {}
            term_statuses: set[str] = set()
            for lang_set in (item for item in element.iter() if _local_name(item.tag) == "langSet"):
                language = (lang_set.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") or lang_set.attrib.get("lang") or "").strip().lower()
                if language:
                    language_terms = terms.setdefault(language, [])
                    for term_element in (item for item in lang_set.iter() if _local_name(item.tag) == "term" and _text(item)):
                        value = _text(term_element)
                        if value not in language_terms:
                            language_terms.append(value)
                for term_note in (item for item in lang_set.iter() if _local_name(item.tag) == "termNote" and _text(item)):
                    note_type = (term_note.attrib.get("type") or "").strip()
                    if note_type:
                        term_statuses.add(f"{note_type}={_text(term_note)}")
            source_terms = terms.get(options.source_language, [])
            target_terms = terms.get(options.target_language, [])
            if not source_terms or not target_terms:
                kind = "missing_language" if len(terms) < 2 else "missing_translation"
                message = "TBX zapis nima zahtevanega jezikovnega para." if kind == "missing_language" else "TBX zapis nima ciljnega termina."
                issues.append(_issue(entry_number, kind, message))
                element.clear()
                continue
            if len(source_terms) != 1 or len(target_terms) != 1:
                status_note = f" Statusi: {', '.join(sorted(term_statuses))}." if term_statuses else ""
                issues.append(_issue(
                    entry_number,
                    "ambiguous",
                    f"TBX zapis je dvoumen: {len(source_terms)} izvornih in {len(target_terms)} ciljnih terminov; prednostnega para ni mogoče dokazati.{status_note}",
                ))
                element.clear()
                continue
            source = source_terms[0]
            target = target_terms[0]
            metadata = _tbx_metadata(element)
            try:
                candidates.append(_candidate(
                    row_number=entry_number, source_term=source, target_term=target,
                    source_language=options.source_language, target_language=options.target_language,
                    domain=metadata.get("subjectfield", options.default_domain),
                    match_mode=options.default_match_mode,
                    external_source=options.external_source,
                    external_record_id=element.attrib.get("id"),
                    source_confidence=metadata.get("reliabilitycode"),
                    definition=metadata.get("definition"), context=metadata.get("context"),
                    note=metadata.get("note", ""),
                    source_version=options.source_version,
                    license_info=options.license_info,
                ))
            except (ValidationError, TerminologyImportError) as exc:
                issues.append(_issue(entry_number, "invalid", str(exc)))
            element.clear()
    except ET.ParseError as exc:
        raise TerminologyImportError(f"TBX XML ni veljaven: {exc}.") from exc
    if entry_number == 0:
        raise TerminologyImportError("TBX ne vsebuje podprtih termEntry zapisov.")
    return candidates, issues, ignored


def _classify(
    candidates: list[TerminologyImportCandidate],
    repository: SqliteGlossaryRepository,
) -> tuple[list[TerminologyImportCandidate], list[TerminologyImportIssue]]:
    existing, _ = repository.list_entries(limit=10_000)
    classified: list[TerminologyImportCandidate] = []
    issues: list[TerminologyImportIssue] = []
    seen: list[TerminologyImportCandidate] = []
    for candidate in candidates:
        matches = [entry for entry in existing if _same_source(entry, candidate)]
        upload_matches = [entry for entry in seen if _same_candidate_source(entry, candidate)]
        duplicate_entries = [entry for entry in matches if _same_definition(entry, candidate)]
        duplicate_upload = [entry for entry in upload_matches if _same_candidate_definition(entry, candidate)]
        if duplicate_entries or duplicate_upload:
            ids = [entry.id for entry in duplicate_entries]
            revisions = {entry.id: entry.revision for entry in matches}
            current = candidate.model_copy(update={"classification": "duplicate", "existing_entry_ids": ids, "existing_entry_revisions": revisions})
            issues.append(_issue(candidate.row_number, "duplicate", "Popoln dvojnik je bil preskočen.", candidate.candidate_id, ids))
        elif matches or upload_matches:
            ids = [entry.id for entry in matches]
            revisions = {entry.id: entry.revision for entry in matches}
            current = candidate.model_copy(update={"classification": "conflict", "existing_entry_ids": ids, "existing_entry_revisions": revisions})
            issues.append(_issue(candidate.row_number, "conflict", "Za isti izraz in domeno obstaja drugačen ciljni termin ali način.", candidate.candidate_id, ids))
        else:
            current = candidate
        classified.append(current)
        seen.append(current)
    return classified, issues


def _candidate(**values: object) -> TerminologyImportCandidate:
    for name, value in values.items():
        if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
            raise TerminologyImportError(f"Polje {name} presega največjo dovoljeno dolžino.")
    values["candidate_id"] = str(uuid.uuid4())
    return TerminologyImportCandidate.model_validate(values)


def _csv_value(row: dict[str, str | None], column: str | None) -> str:
    if not column:
        return ""
    return (row.get(column) or "").strip()


def _tbx_metadata(element: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in element.iter():
        tag = _local_name(item.tag)
        item_type = (item.attrib.get("type") or "").strip().lower()
        if tag == "descrip" and item_type in {"subjectfield", "definition", "context", "reliabilitycode"}:
            result.setdefault(item_type, _text(item))
        elif tag == "note" and _text(item):
            result.setdefault("note", _text(item))
    return result


def _text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _same_source(entry: GlossaryEntry, candidate: TerminologyImportCandidate) -> bool:
    return (
        entry.source_language == candidate.source_language
        and entry.target_language == candidate.target_language
        and entry.domain == candidate.domain
        and entry.source_term.casefold() == candidate.source_term.casefold()
    )


def _same_definition(entry: GlossaryEntry, candidate: TerminologyImportCandidate) -> bool:
    return entry.target_term == candidate.target_term and entry.match_mode == candidate.match_mode


def _same_candidate_source(left: TerminologyImportCandidate, right: TerminologyImportCandidate) -> bool:
    return (
        left.source_language == right.source_language
        and left.target_language == right.target_language
        and left.domain == right.domain
        and left.source_term.casefold() == right.source_term.casefold()
    )


def _same_candidate_definition(left: TerminologyImportCandidate, right: TerminologyImportCandidate) -> bool:
    return left.target_term == right.target_term and left.match_mode == right.match_mode


def _issue(
    row_number: int,
    kind: str,
    message: str,
    candidate_id: str | None = None,
    existing_ids: list[str] | None = None,
) -> TerminologyImportIssue:
    return TerminologyImportIssue(
        row_number=row_number,
        kind=kind,  # type: ignore[arg-type]
        message=message,
        candidate_id=candidate_id,
        existing_entry_ids=existing_ids or [],
    )
