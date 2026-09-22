from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GlossaryMatchMode = Literal["exact_segment", "preferred_term", "do_not_translate"]
GlossaryStatus = Literal["proposed", "approved", "rejected", "archived"]
GlossaryOrigin = Literal["manual", "reviewed_correction", "import", "built_in"]
GlossaryAction = Literal["create", "update", "approve", "reject", "archive", "restore", "import"]

MAX_TERM_CHARS = 500
MAX_NOTE_CHARS = 2_000
MAX_SOURCE_LABEL_CHARS = 200


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GlossaryEntryCreate(StrictModel):
    source_term: str = Field(min_length=1, max_length=MAX_TERM_CHARS)
    target_term: str = Field(default="", max_length=MAX_TERM_CHARS)
    source_language: str = Field(default="en", min_length=2, max_length=12)
    target_language: str = Field(default="sl", min_length=2, max_length=12)
    domain: str = Field(default="", max_length=100)
    match_mode: GlossaryMatchMode = "preferred_term"
    case_sensitive: bool = False
    status: GlossaryStatus = "proposed"
    origin: GlossaryOrigin = "manual"
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)
    source_label: str | None = Field(default=None, max_length=MAX_SOURCE_LABEL_CHARS)
    document_hash: str | None = None

    @field_validator("source_term", "target_term", "domain", "note")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_language", "target_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.replace("-", "").isalpha():
            raise ValueError("Jezikovna oznaka ni veljavna.")
        return normalized

    @field_validator("document_hash")
    @classmethod
    def validate_document_hash(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("Hash dokumenta mora biti SHA-256.")
        return normalized

    @model_validator(mode="after")
    def validate_target(self) -> "GlossaryEntryCreate":
        if self.match_mode != "do_not_translate" and not self.target_term:
            raise ValueError("Ciljni izraz je obvezen za izbrani način ujemanja.")
        if self.origin == "built_in":
            raise ValueError("Vgrajenih vnosov ni mogoče ustvariti prek uporabniškega API-ja.")
        return self


class GlossaryEntryUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    source_term: str | None = Field(default=None, min_length=1, max_length=MAX_TERM_CHARS)
    target_term: str | None = Field(default=None, max_length=MAX_TERM_CHARS)
    source_language: str | None = Field(default=None, min_length=2, max_length=12)
    target_language: str | None = Field(default=None, min_length=2, max_length=12)
    domain: str | None = Field(default=None, max_length=100)
    match_mode: GlossaryMatchMode | None = None
    case_sensitive: bool | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    source_label: str | None = Field(default=None, max_length=MAX_SOURCE_LABEL_CHARS)
    document_hash: str | None = None


class GlossaryRevisionRequest(StrictModel):
    expected_revision: int = Field(ge=1)


class GlossaryUsageRequest(StrictModel):
    entry_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("entry_ids")
    @classmethod
    def validate_entry_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 100 for value in normalized):
            raise ValueError("Identifikator glosarskega vnosa ni veljaven.")
        return list(dict.fromkeys(normalized))


class GlossaryEntry(StrictModel):
    id: str
    source_term: str
    target_term: str
    source_language: str
    target_language: str
    domain: str
    match_mode: GlossaryMatchMode
    case_sensitive: bool
    status: GlossaryStatus
    origin: GlossaryOrigin
    note: str
    source_label: str | None
    document_hash: str | None
    revision: int
    created_at: datetime
    updated_at: datetime
    usage_count: int
    last_used_at: datetime | None
    external_source: str | None = None
    external_record_id: str | None = None
    source_confidence: str | None = None
    definition: str | None = None
    context: str | None = None
    source_version: str | None = None
    license_info: str | None = None
    import_file_sha256: str | None = None
    import_batch_id: str | None = None
    imported_at: datetime | None = None


class GlossaryHistoryRecord(StrictModel):
    history_id: str
    entry_id: str
    revision: int
    action: GlossaryAction
    snapshot: GlossaryEntry
    created_at: datetime


class GlossaryListResponse(StrictModel):
    entries: list[GlossaryEntry]
    total: int
    active_set_hash: str


class GlossaryStats(StrictModel):
    total: int
    by_status: dict[str, int]
    by_mode: dict[str, int]
    active_set_hash: str


class GlossaryStatusResponse(StrictModel):
    available: bool
    message: str | None = None
    schema_version: int | None = None


class GlossaryImportRow(GlossaryEntryCreate):
    row_number: int = Field(ge=1)


class GlossaryImportIssue(StrictModel):
    row_number: int
    kind: Literal["duplicate", "conflict", "invalid", "override"]
    message: str
    existing_entry_id: str | None = None


class GlossaryImportPreview(StrictModel):
    valid_rows: list[GlossaryImportRow]
    new_count: int
    duplicate_count: int
    conflict_count: int
    invalid_count: int
    override_count: int
    issues: list[GlossaryImportIssue]


class GlossaryImportConfirm(StrictModel):
    rows: list[GlossaryImportRow] = Field(max_length=1_000)
    overwrite_conflicts: bool = False


class GlossaryImportResult(StrictModel):
    created: int
    updated: int
    skipped: int


class TerminologyCsvMapping(StrictModel):
    source_term: str = Field(min_length=1, max_length=100)
    target_term: str = Field(min_length=1, max_length=100)
    source_language: str | None = Field(default=None, max_length=100)
    target_language: str | None = Field(default=None, max_length=100)
    domain: str | None = Field(default=None, max_length=100)
    match_mode: str | None = Field(default=None, max_length=100)
    external_record_id: str | None = Field(default=None, max_length=100)
    confidence: str | None = Field(default=None, max_length=100)
    definition: str | None = Field(default=None, max_length=100)
    context: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=100)
    source_version: str | None = Field(default=None, max_length=100)


class TerminologyImportOptions(StrictModel):
    source_language: str = Field(default="en", min_length=2, max_length=12)
    target_language: str = Field(default="sl", min_length=2, max_length=12)
    default_domain: str = Field(default="", max_length=100)
    default_match_mode: GlossaryMatchMode = "preferred_term"
    external_source: str = Field(min_length=1, max_length=MAX_SOURCE_LABEL_CHARS)
    source_version: str | None = Field(default=None, max_length=200)
    license_info: str | None = Field(default=None, max_length=500)
    csv_mapping: TerminologyCsvMapping | None = None

    @field_validator("source_language", "target_language")
    @classmethod
    def validate_import_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.replace("-", "").isalpha():
            raise ValueError("Jezikovna oznaka ni veljavna.")
        return normalized


class TerminologyImportCandidate(StrictModel):
    candidate_id: str
    row_number: int = Field(ge=1)
    source_term: str = Field(min_length=1, max_length=MAX_TERM_CHARS)
    target_term: str = Field(min_length=1, max_length=MAX_TERM_CHARS)
    source_language: str = Field(min_length=2, max_length=12)
    target_language: str = Field(min_length=2, max_length=12)
    domain: str = Field(default="", max_length=100)
    match_mode: GlossaryMatchMode = "preferred_term"
    external_source: str = Field(min_length=1, max_length=MAX_SOURCE_LABEL_CHARS)
    external_record_id: str | None = Field(default=None, max_length=200)
    source_confidence: str | None = Field(default=None, max_length=100)
    definition: str | None = Field(default=None, max_length=2_000)
    context: str | None = Field(default=None, max_length=2_000)
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)
    source_version: str | None = Field(default=None, max_length=200)
    license_info: str | None = Field(default=None, max_length=500)
    classification: Literal["new", "duplicate", "conflict", "invalid"] = "new"
    existing_entry_ids: list[str] = Field(default_factory=list, max_length=50)
    existing_entry_revisions: dict[str, int] = Field(default_factory=dict)

    @field_validator("source_language", "target_language")
    @classmethod
    def validate_candidate_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.replace("-", "").isalpha():
            raise ValueError("Jezikovna oznaka ni veljavna.")
        return normalized


class TerminologyImportIssue(StrictModel):
    row_number: int
    kind: Literal["duplicate", "conflict", "invalid", "ambiguous", "missing_language", "missing_translation"]
    message: str
    candidate_id: str | None = None
    existing_entry_ids: list[str] = Field(default_factory=list)


class TerminologyImportPreview(StrictModel):
    preview_id: str
    import_batch_id: str
    expires_at: datetime
    format: Literal["csv", "tbx"]
    file_sha256: str
    candidates: list[TerminologyImportCandidate]
    valid_count: int
    new_count: int
    duplicate_count: int
    conflict_count: int
    invalid_count: int
    missing_translation_count: int
    missing_language_count: int
    issues: list[TerminologyImportIssue]
    domains: list[str]
    ignored_fields: list[str]
    external_source: str
    source_version: str | None
    license_info: str | None


class TerminologyImportConfirm(StrictModel):
    preview_id: str
    file_sha256: str = Field(min_length=64, max_length=64)
    candidate_ids: list[str] = Field(min_length=1, max_length=1_000)

    @field_validator("file_sha256")
    @classmethod
    def validate_import_hash(cls, value: str) -> str:
        normalized = value.lower()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("SHA-256 uvozne datoteke ni veljaven.")
        return normalized


class TerminologyImportCommitResult(StrictModel):
    import_batch_id: str
    created: int
    skipped_duplicates: int
    skipped_conflicts: int
    skipped_unselected: int
