from typing import Literal

from pydantic import BaseModel, Field

PdfKind = Literal["digital", "scanned", "mixed", "empty"]


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


class RenderedPage(BaseModel):
    page_number: int
    path: str
    width_px: int | None = None
    height_px: int | None = None


class RenderResult(BaseModel):
    filename: str
    dpi: int
    pages: list[RenderedPage]

