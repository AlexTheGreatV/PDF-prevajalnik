export type PdfKind = "digital" | "scanned" | "mixed" | "empty";
export type RegionKind = "header" | "footer" | "column" | "table" | "body" | "unknown";
export type StructuredBlockKind =
  | "heading"
  | "paragraph"
  | "list"
  | "list_item"
  | "table"
  | "caption"
  | "unknown";

export type TextBlock = {
  page_number: number;
  block_number: number;
  text: string;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
};

export type PageAnalysis = {
  page_number: number;
  kind: PdfKind;
  extracted_text_chars: number;
  dimensions: { width: number; height: number; unit: string };
  text_blocks: TextBlock[];
  image_count: number;
};

export type PdfAnalysis = {
  filename: string;
  page_count: number;
  kind: PdfKind;
  extracted_text_chars: number;
  pages: PageAnalysis[];
  document?: DocumentModel | null;
};

export type RenderResult = {
  filename: string;
  dpi: number;
  pages: Array<{
    page_number: number;
    path: string;
    width_px: number | null;
    height_px: number | null;
  }>;
};

export type BBox = {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
};

export type StructuredSpan = {
  text: string;
  bbox: BBox;
  font: string | null;
  font_size: number | null;
  color: number | string | null;
  flags: number | null;
  bold: boolean;
  italic: boolean;
};

export type StructuredLine = {
  bbox: BBox;
  original_text: string;
  spans: StructuredSpan[];
};

export type LineBoundary = {
  before_line_index: number;
  after_line_index: number;
  kind: "space" | "soft_hyphen" | "visible_hyphen" | "none";
  warning: string | null;
};

export type OcrReason =
  | "scanned_page"
  | "low_native_text_coverage"
  | "suspicious_glyph"
  | "image_text_candidate"
  | "font_encoding_anomaly"
  | "manual_request"
  | "ambiguous_hyphen";

export type OcrSource = "full_page" | "image_region" | "suspicious_block";

export type OcrWord = {
  text: string;
  bbox: BBox;
  confidence: number;
};

export type OcrResult = {
  text: string;
  words: OcrWord[];
  confidence: number;
  bbox: BBox;
  language: string;
  provider: string;
  processing_time_ms: number;
  source: OcrSource;
};

export type OcrCandidate = {
  page_number: number;
  source: OcrSource;
  reasons: OcrReason[];
  bbox: BBox;
  native_text: string | null;
  ocr_text: string | null;
  ocr_confidence: number | null;
  selected_text_source: "native" | "ocr" | "manual_review";
  ocr_applied: boolean;
  requires_review: boolean;
  result: OcrResult | null;
  linked_block_reading_order: number | null;
  linked_heading: string | null;
  dpi: number | null;
  scale: number | null;
  status: "planned" | "processed" | "skipped" | "unavailable" | "failed";
  message: string | null;
};

export type OcrStatus = {
  available: boolean;
  provider: string;
  executable: string | null;
  version: string | null;
  languages: string[];
  tessdata_prefix: string | null;
  pymupdf_ocr_available: boolean;
  message: string | null;
};

export type OcrResponse = {
  filename: string;
  status: OcrStatus;
  candidates: OcrCandidate[];
};

export type StructuredStyle = {
  font: string | null;
  font_size: number | null;
  color: number | string | null;
  flags: number | null;
  bold: boolean;
  italic: boolean;
};

export type StructuredBlock = {
  kind: StructuredBlockKind;
  bbox: BBox;
  original_text: string;
  normalized_text: string;
  lines: StructuredLine[];
  line_boundaries: LineBoundary[];
  style: StructuredStyle;
  reading_order: number;
  confidence: number;
  warnings: string[];
  needs_ocr_check: boolean;
  table_cells?: string[][] | null;
};

export type StructuredRegion = {
  kind: RegionKind;
  bbox: BBox;
  reading_order: number;
  blocks: StructuredBlock[];
};

export type StructuredImage = {
  bbox: BBox | null;
  width: number | null;
  height: number | null;
  xref: number | null;
  needs_ocr_check: boolean;
  ocr_text: string | null;
  ocr_confidence: number | null;
};

export type StructuredPage = {
  page_number: number;
  width: number;
  height: number;
  page_type: PdfKind;
  regions: StructuredRegion[];
  images: StructuredImage[];
};

export type DocumentModel = {
  pages: StructuredPage[];
  metadata: Record<string, unknown>;
};
