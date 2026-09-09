from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_TRANSLATION_BATCH_SIZE = 32
MAX_TRANSLATION_SEGMENT_CHARS = 5_000
MAX_TRANSLATION_BATCH_CHARS = 20_000


class TranslationTextRequest(BaseModel):
    segment_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_TRANSLATION_SEGMENT_CHARS)


class TranslationBatchRequest(BaseModel):
    source_language: Literal["en"]
    target_language: Literal["sl"]
    segments: list[TranslationTextRequest] = Field(min_length=1, max_length=MAX_TRANSLATION_BATCH_SIZE)

    @model_validator(mode="after")
    def validate_batch(self) -> "TranslationBatchRequest":
        if len({segment.segment_id for segment in self.segments}) != len(self.segments):
            raise ValueError("segment_id mora biti znotraj paketa enoličen.")
        if sum(len(segment.text) for segment in self.segments) > MAX_TRANSLATION_BATCH_CHARS:
            raise ValueError(f"Paket sme vsebovati največ {MAX_TRANSLATION_BATCH_CHARS} znakov.")
        return self


class TranslationError(BaseModel):
    code: str
    message: str
    retryable: bool


class TranslationTextResult(BaseModel):
    segment_id: str
    translated_text: str | None = None
    confidence: float | None = None
    error: TranslationError | None = None
    processing_time_ms: int


class TranslationBatchResponse(BaseModel):
    provider_id: str
    model_id: str
    source_language: Literal["en"] = "en"
    target_language: Literal["sl"] = "sl"
    processing_time_ms: int
    results: list[TranslationTextResult]


class LocalTranslationStatus(BaseModel):
    available: bool
    loaded: bool
    provider_id: str
    model_id: str
    device: str
    compute_type: str
    message: str | None = None
    load_time_ms: int | None = None
