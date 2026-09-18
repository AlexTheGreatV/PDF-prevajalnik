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
