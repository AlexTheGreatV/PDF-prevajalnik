export type PdfKind = "digital" | "scanned" | "mixed" | "empty";

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

