from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import BBox, PdfKind, SpanStyle

ExportMode = Literal["diagnostic", "replacement_preview"]
FitStatus = Literal["not_checked", "fits", "overflow", "skipped"]
SkipReason = Literal[
    "not_reviewed",
    "empty_translation",
    "missing_bbox",
    "invalid_page",
    "bbox_outside_page",
    "non_digital_source",
    "ocr_only_source",
    "image_overlap",
    "complex_table",
    "rotated_page",
    "source_text_mismatch",
    "overlap_conflict",
    "missing_glyph",
    "overflow",
]


class PdfPageIdentity(BaseModel):
    page_number: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: int


class PdfDocumentIdentity(BaseModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_count: int = Field(ge=1)
    document_model_version: int = Field(ge=1)
    pages: list[PdfPageIdentity]


class PdfExportSegment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    segment_id: str = Field(min_length=1, max_length=200)
    page_number: int = Field(ge=1, le=100_000)
    block_id: str = Field(min_length=1, max_length=200)
    block_type: Literal["heading", "paragraph", "list", "list_item", "table", "table_cell", "caption", "unknown"]
    source_text: str = Field(max_length=200_000)
    source_text_origin: Literal["native", "accepted_ocr"]
    source_bbox: BBox | None = None
    source_page_type: PdfKind | None = None
    source_rotation: int | None = None
    source_style: SpanStyle | None = None
    edited_translation: str = Field(max_length=200_000)
    status: Literal["pending", "translating", "translated", "reviewed", "failed", "skipped"]
    translation_origin: Literal["glossary", "provider", "manual"] | None = None


class PdfExportJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[4]
    source_filename: str
    document_identity: PdfDocumentIdentity
    segments: list[PdfExportSegment] = Field(max_length=10_000)

    @model_validator(mode="after")
    def unique_segment_ids(self) -> "PdfExportJob":
        identifiers = [segment.segment_id for segment in self.segments]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Segment identifiers must be unique.")
        return self


class PdfReplacement(BaseModel):
    segment_id: str
    page_number: int
    source_bbox: BBox | None
    source_text: str
    translated_text: str
    translation_origin: str | None
    reviewed: bool
    block_type: str
    proposed_font: str
    proposed_font_size: float
    text_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    fitted_font_size: float | None = None
    fit_status: FitStatus
    eligible: bool = False
    warnings: list[str] = Field(default_factory=list)
    skip_reason: SkipReason | None = None


class PdfExportSummary(BaseModel):
    total_segments: int
    reviewed: int
    eligible: int
    skipped: int
    overflow: int
    conflicts: int


class PdfExportPlan(BaseModel):
    filename: str
    mode: ExportMode
    document_sha256: str
    summary: PdfExportSummary
    replacements: list[PdfReplacement]
