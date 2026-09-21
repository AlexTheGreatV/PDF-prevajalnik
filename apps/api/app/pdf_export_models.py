from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import BBox, PdfKind, SpanStyle

ExportMode = Literal["diagnostic", "replacement_preview"]
FitStatus = Literal["not_checked", "fits", "overflow", "skipped"]
FitStrategy = Literal["not_checked", "exact", "expanded_bbox", "scaled", "overflow", "unsupported_style", "missing_glyph", "skipped"]
TextAlignment = Literal["left", "center", "right", "justify"]
ImageOverlapKind = Literal["none", "simple_background", "complex_background", "image_only_text", "uncertain"]
SkipReason = Literal[
    "not_reviewed",
    "empty_translation",
    "missing_bbox",
    "invalid_page",
    "bbox_outside_page",
    "non_digital_source",
    "ocr_only_source",
    "ocr_not_accepted",
    "ocr_provenance_mismatch",
    "ocr_registry_missing",
    "ocr_registry_unaccepted",
    "ocr_registry_stale",
    "ocr_registry_mismatch",
    "ocr_region_too_small",
    "ocr_background_unsafe",
    "image_overlap",
    "complex_table",
    "technical_value",
    "empty_cell",
    "duplicate_table_content",
    "overlapping_table_replacement",
    "ambiguous_cell_membership",
    "merged_cell_ambiguous",
    "missing_cell_bbox",
    "duplicate_cell_geometry",
    "unsupported_table_structure",
    "cell_overflow",
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


class AcceptedOcrSource(BaseModel):
    revision_id: str | None = Field(default=None, min_length=1, max_length=100)
    review_id: str = Field(min_length=1, max_length=300)
    accepted_text: str = Field(min_length=1, max_length=200_000)
    bbox: BBox
    source: Literal["block", "page", "image", "manual"]
    reason: str = Field(min_length=1, max_length=100)


class OcrReviewRegistration(BaseModel):
    review_id: str = Field(min_length=1, max_length=300)
    page_number: int = Field(ge=1, le=100_000)
    bbox: BBox
    text: str = Field(min_length=1, max_length=200_000)
    source: Literal["block", "page", "image", "manual"]
    reason: str = Field(min_length=1, max_length=100)


class OcrReviewRegistrationResult(BaseModel):
    revision_id: str
    accepted: bool
    expires_in_seconds: int


class OcrReviewAcceptance(BaseModel):
    revision_id: str = Field(min_length=1, max_length=100)


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
    accepted_ocr_source: AcceptedOcrSource | None = None
    edited_translation: str = Field(max_length=200_000)
    status: Literal["pending", "translating", "translated", "reviewed", "failed", "skipped"]
    translation_origin: Literal["glossary", "translation_memory", "provider", "mixed", "manual"] | None = None


class PdfExportJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[4, 5, 6, 7, 8]
    source_filename: str
    document_identity: PdfDocumentIdentity
    segments: list[PdfExportSegment] = Field(max_length=10_000)

    @model_validator(mode="after")
    def unique_segment_ids(self) -> "PdfExportJob":
        identifiers = [segment.segment_id for segment in self.segments]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Segment identifiers must be unique.")
        return self


class PdfTextStyle(BaseModel):
    source_font_name: str | None = None
    source_font_xref: int | None = None
    font_size: float
    bold: bool = False
    italic: bool = False
    color: str = "#000000"
    line_height: float = 1.2
    alignment: TextAlignment = "left"
    writing_direction: tuple[float, float] = (1.0, 0.0)
    rotation: int = 0
    dominant_style_confidence: float = 1.0


class PdfBackgroundMetrics(BaseModel):
    dpi: int = 300
    sample_count: int = 0
    color_variance: float | None = None
    edge_density: float | None = None
    texture_score: float | None = None
    side_color_delta: float | None = None
    near_threshold: bool = False


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
    requested_font_size: float
    text_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    color: str = "#000000"
    font_family: str
    font_weight: Literal["regular", "bold"] = "regular"
    font_style: Literal["normal", "italic"] = "normal"
    alignment: TextAlignment = "left"
    line_height: float = 1.2
    original_bbox: BBox | None = None
    fitted_bbox: BBox | None = None
    original_line_count: int = 0
    fitted_line_count: int | None = None
    scale: float | None = None
    fit_strategy: FitStrategy = "not_checked"
    typography: PdfTextStyle | None = None
    typography_warnings: list[str] = Field(default_factory=list)
    image_overlap_kind: ImageOverlapKind = "none"
    overlapping_image_xrefs: list[int] = Field(default_factory=list)
    overlap_ratio: float = 0.0
    background_patch_used: bool = False
    background_color: str | None = None
    patch_rects: list[BBox] = Field(default_factory=list)
    redaction_rects: list[BBox] = Field(default_factory=list)
    overlap_confidence: float = 1.0
    background_metrics: PdfBackgroundMetrics | None = None
    image_overlap_warnings: list[str] = Field(default_factory=list)
    fitted_font_size: float | None = None
    fit_status: FitStatus
    eligible: bool = False
    warnings: list[str] = Field(default_factory=list)
    skip_reason: SkipReason | None = None


class PdfTableCellReplacement(PdfReplacement):
    table_id: str
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    row_span: int = Field(ge=1)
    column_span: int = Field(ge=1)
    cell_bbox: BBox | None = None
    content_bbox: BBox | None = None
    source_match_status: Literal["matched", "mismatch", "not_checked"] = "not_checked"
    background_fill: str | None = None
    border_clearance: float = 0.0


class PdfTableExportPlan(BaseModel):
    table_id: str
    page_number: int
    bbox: BBox | None = None
    row_count: int = 0
    column_count: int = 0
    reliable: bool = False
    partial_export: bool = False
    warnings: list[str] = Field(default_factory=list)
    cells: list[PdfTableCellReplacement] = Field(default_factory=list)


class PdfTableExportSummary(BaseModel):
    detected_tables: int = 0
    reviewed_cells: int = 0
    eligible_cells: int = 0
    exported_cells: int = 0
    technical_cells: int = 0
    overflow_cells: int = 0
    ambiguous_cells: int = 0
    image_only_cells: int = 0
    conflicting_cells: int = 0
    partial_tables: int = 0


class PdfOcrExportSummary(BaseModel):
    reviewed_regions: int = 0
    eligible_regions: int = 0
    exported_regions: int = 0
    unsafe_regions: int = 0
    stale_regions: int = 0


class PdfOcrReplacement(PdfReplacement):
    ocr_review_id: str | None = None
    ocr_source: Literal["block", "page", "image", "manual"] | None = None
    ocr_reason: str | None = None


class PdfExportSummary(BaseModel):
    total_segments: int
    reviewed: int
    eligible: int
    skipped: int
    overflow: int
    conflicts: int
    exact: int = 0
    expanded: int = 0
    scaled: int = 0
    background_patches: int = 0
    image_overlap_skipped: int = 0


class PdfExportPlan(BaseModel):
    filename: str
    mode: ExportMode
    document_sha256: str
    summary: PdfExportSummary
    replacements: list[PdfReplacement]
    table_summary: PdfTableExportSummary = Field(default_factory=PdfTableExportSummary)
    tables: list[PdfTableExportPlan] = Field(default_factory=list)
    ocr_summary: PdfOcrExportSummary = Field(default_factory=PdfOcrExportSummary)
    ocr_replacements: list[PdfOcrReplacement] = Field(default_factory=list)
