from typing import Any, Literal

from pydantic import BaseModel, Field

PdfKind = Literal["digital", "scanned", "mixed", "empty"]
RegionKind = Literal["header", "footer", "column", "table", "body", "unknown"]
BlockKind = Literal["heading", "paragraph", "list", "list_item", "table", "caption", "unknown"]


class PageDimensions(BaseModel):
    width: float
    height: float
    unit: str = "pt"


class TextBlock(BaseModel):
    page_number: int
    block_number: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


class PageAnalysis(BaseModel):
    page_number: int
    kind: PdfKind
    extracted_text_chars: int
    dimensions: PageDimensions
    text_blocks: list[TextBlock] = Field(default_factory=list)
    image_count: int = 0


class PdfAnalysis(BaseModel):
    filename: str
    page_count: int
    kind: PdfKind
    extracted_text_chars: int
    pages: list[PageAnalysis]
    document: "DocumentModel | None" = None


class RenderedPage(BaseModel):
    page_number: int
    path: str
    width_px: int | None = None
    height_px: int | None = None


class RenderResult(BaseModel):
    filename: str
    dpi: int
    pages: list[RenderedPage]


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class SpanStyle(BaseModel):
    font: str | None = None
    font_size: float | None = None
    color: int | str | None = None
    flags: int | None = None
    bold: bool = False
    italic: bool = False


class DocumentSpan(BaseModel):
    text: str
    bbox: BBox
    font: str | None = None
    font_size: float | None = None
    color: int | str | None = None
    flags: int | None = None
    bold: bool = False
    italic: bool = False


class DocumentLine(BaseModel):
    bbox: BBox
    original_text: str
    spans: list[DocumentSpan] = Field(default_factory=list)


class LineBoundary(BaseModel):
    before_line_index: int
    after_line_index: int
    kind: Literal["space", "soft_hyphen", "visible_hyphen", "none"] = "space"
    warning: str | None = None


class DocumentBlock(BaseModel):
    kind: BlockKind
    bbox: BBox
    original_text: str
    normalized_text: str
    lines: list[DocumentLine] = Field(default_factory=list)
    line_boundaries: list[LineBoundary] = Field(default_factory=list)
    style: SpanStyle = Field(default_factory=SpanStyle)
    reading_order: int
    confidence: float = 1.0
    warnings: list[str] = Field(default_factory=list)
    needs_ocr_check: bool = False
    table_cells: list[list[str]] | None = None


class DocumentRegion(BaseModel):
    kind: RegionKind
    bbox: BBox
    reading_order: int
    blocks: list[DocumentBlock] = Field(default_factory=list)


class DocumentImage(BaseModel):
    bbox: BBox | None = None
    width: int | None = None
    height: int | None = None
    xref: int | None = None
    needs_ocr_check: bool = True
    ocr_text: str | None = None
    ocr_confidence: float | None = None


class DocumentPage(BaseModel):
    page_number: int
    width: float
    height: float
    page_type: PdfKind
    regions: list[DocumentRegion] = Field(default_factory=list)
    images: list[DocumentImage] = Field(default_factory=list)


class DocumentModel(BaseModel):
    pages: list[DocumentPage] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
