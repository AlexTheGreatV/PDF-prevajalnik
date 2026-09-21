from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TranslationMemoryStatus = Literal["proposed", "approved", "rejected", "archived"]
TranslationMemoryOrigin = Literal["manual_review"]
TranslationMemorySourceKind = Literal["native", "accepted_ocr"]
TranslationMemoryAction = Literal["create", "approve", "reject", "update", "archive", "restore"]

MAX_SEGMENT_CHARS = 20_000
MAX_NOTE_CHARS = 2_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TranslationMemoryEntryCreate(StrictModel):
    source_text: str = Field(min_length=1, max_length=MAX_SEGMENT_CHARS)
    target_text: str = Field(min_length=1, max_length=MAX_SEGMENT_CHARS)
    source_language: str = Field(default="en", min_length=2, max_length=12)
    target_language: str = Field(default="sl", min_length=2, max_length=12)
    domain: str = Field(default="", max_length=100)
    status: Literal["proposed"] = "proposed"
    origin: TranslationMemoryOrigin = "manual_review"
    source_document_sha256: str | None = None
    source_segment_id: str | None = Field(default=None, max_length=200)
    source_kind: TranslationMemorySourceKind = "native"
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)

    @field_validator("source_text", "target_text")
    @classmethod
    def validate_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Izvorno in ciljno besedilo ne smeta biti prazna.")
        return value

    @field_validator("source_language", "target_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.replace("-", "").isalpha():
            raise ValueError("Jezikovna oznaka ni veljavna.")
        return normalized

    @field_validator("domain", "note")
    @classmethod
    def strip_metadata(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_segment_id")
    @classmethod
    def normalize_segment_id(cls, value: str | None) -> str | None:
        normalized = value.strip() if value else ""
        return normalized or None

    @field_validator("source_document_sha256")
    @classmethod
    def validate_document_hash(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("Hash dokumenta mora biti SHA-256.")
        return normalized


class TranslationMemoryEntryUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    source_text: str | None = Field(default=None, min_length=1, max_length=MAX_SEGMENT_CHARS)
    target_text: str | None = Field(default=None, min_length=1, max_length=MAX_SEGMENT_CHARS)
    source_language: str | None = Field(default=None, min_length=2, max_length=12)
    target_language: str | None = Field(default=None, min_length=2, max_length=12)
    domain: str | None = Field(default=None, max_length=100)
    source_document_sha256: str | None = None
    source_segment_id: str | None = Field(default=None, max_length=200)
    source_kind: TranslationMemorySourceKind | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)

    @field_validator("source_text", "target_text")
    @classmethod
    def validate_optional_nonblank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Izvorno in ciljno besedilo ne smeta biti prazna.")
        return value

    @field_validator("source_language", "target_language")
    @classmethod
    def validate_optional_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized.replace("-", "").isalpha():
            raise ValueError("Jezikovna oznaka ni veljavna.")
        return normalized

    @field_validator("domain", "note")
    @classmethod
    def strip_optional_metadata(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("source_document_sha256")
    @classmethod
    def validate_optional_document_hash(cls, value: str | None) -> str | None:
        return TranslationMemoryEntryCreate.validate_document_hash(value)


class TranslationMemoryRevisionRequest(StrictModel):
    expected_revision: int = Field(ge=1)


class TranslationMemoryUsageRequest(StrictModel):
    entry_ids: list[str] = Field(min_length=1, max_length=500)


class TranslationMemoryExactLookupRequest(StrictModel):
    source_text: str = Field(min_length=1, max_length=MAX_SEGMENT_CHARS)
    source_language: str = Field(min_length=2, max_length=12)
    target_language: str = Field(min_length=2, max_length=12)
    domain: str = Field(default="", max_length=100)

    @field_validator("source_language", "target_language")
    @classmethod
    def normalize_lookup_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.replace("-", "").isalpha():
            raise ValueError("Jezikovna oznaka ni veljavna.")
        return normalized

    @field_validator("domain")
    @classmethod
    def strip_lookup_domain(cls, value: str) -> str:
        return value.strip()


class TranslationMemoryEntry(StrictModel):
    id: str
    source_text: str
    target_text: str
    normalized_source: str
    normalized_source_sha256: str
    source_language: str
    target_language: str
    domain: str
    status: TranslationMemoryStatus
    origin: TranslationMemoryOrigin
    source_document_sha256: str | None
    source_segment_id: str | None
    source_kind: TranslationMemorySourceKind
    note: str
    revision: int
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    usage_count: int
    last_used_at: datetime | None


class TranslationMemoryHistoryRecord(StrictModel):
    history_id: str
    entry_id: str
    revision: int
    action: TranslationMemoryAction
    snapshot: TranslationMemoryEntry
    created_at: datetime


class TranslationMemoryListResponse(StrictModel):
    entries: list[TranslationMemoryEntry]
    total: int
    active_set_hash: str


class TranslationMemoryLookupResponse(StrictModel):
    status: Literal["none", "match", "conflict"]
    match: TranslationMemoryEntry | None = None
    candidates: list[TranslationMemoryEntry] = Field(default_factory=list)
    normalized_source_sha256: str


class TranslationMemoryStats(StrictModel):
    total: int
    by_status: dict[str, int]
    active_set_hash: str


class TranslationMemoryStatusResponse(StrictModel):
    available: bool
    message: str | None = None
    schema_version: int | None = None
