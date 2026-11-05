from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_TRANSLATION_BATCH_SIZE = 32
MAX_TRANSLATION_SEGMENT_CHARS = 5_000
MAX_TRANSLATION_BATCH_CHARS = 20_000


class TerminologyHint(BaseModel):
    source_term: str = Field(min_length=1, max_length=160)
    target_term: str = Field(min_length=1, max_length=160)


class TerminologyRetryContext(BaseModel):
    mode: Literal["preferred_terms"]
    source_segment_id: str = Field(min_length=1, max_length=200)
    domain: str = Field(default="", max_length=160)
    preferred_terms: list[TerminologyHint] = Field(min_length=1, max_length=8)


class TranslationTextRequest(BaseModel):
    segment_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_TRANSLATION_SEGMENT_CHARS)
    context: TerminologyRetryContext | None = None


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
    source_token_count: int | None = None
    output_token_count: int | None = None
    max_decoding_length: int | None = None
    reached_decoding_limit: bool = False
    has_end_token: bool | None = None
    beam_size: int | None = None
    alternative_translations: list[str] = Field(default_factory=list, max_length=4)


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
    profile_id: str
    device: str
    compute_type: str
    message: str | None = None
    load_time_ms: int | None = None
