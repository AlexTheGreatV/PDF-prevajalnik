import type {
  BBox,
  PageAnalysis,
  PdfAnalysis,
  PdfKind,
  StructuredBlock,
  StructuredPage,
  TextBlock,
} from "./pdf-types";

export const MAX_PDF_BYTES = 50 * 1024 * 1024;

const kindLabels: Record<PdfKind, string> = {
  digital: "digitalen",
  scanned: "skeniran",
  mixed: "mešan",
  empty: "prazen",
};

export function formatPdfKind(kind: PdfKind): string {
  return kindLabels[kind];
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function validatePdfFile(file: { name: string; size: number; type: string }): string | null {
  const hasPdfName = file.name.toLowerCase().endsWith(".pdf");
  const hasPdfType = file.type === "" || file.type === "application/pdf" || file.type === "application/x-pdf";

  if (!hasPdfName || !hasPdfType) {
    return "Izberi datoteko PDF.";
  }

  if (file.size > MAX_PDF_BYTES) {
    return `PDF je prevelik. Največja dovoljena velikost je ${formatBytes(MAX_PDF_BYTES)}.`;
  }

  return null;
}

export function countTextBlocks(analysis: PdfAnalysis): number {
  if (analysis.document) {
    return analysis.document.pages.reduce(
      (total, page) =>
        total + page.regions.reduce((pageTotal, region) => pageTotal + region.blocks.length, 0),
      0,
    );
  }
  return analysis.pages.reduce((total, page) => total + page.text_blocks.length, 0);
}

export function countImages(analysis: PdfAnalysis): number {
  return analysis.pages.reduce((total, page) => total + page.image_count, 0);
}

export function pageText(page: PageAnalysis): string {
  return page.text_blocks.map((block) => block.text).join("\n\n");
}

export function structuredPageText(page: StructuredPage, mode: "original" | "normalized"): string {
  return flattenStructuredBlocks(page)
    .map((block) => (mode === "original" ? block.original_text : block.normalized_text))
    .filter(Boolean)
    .join("\n\n");
}

export function flattenStructuredBlocks(page: StructuredPage): StructuredBlock[] {
  return page.regions.flatMap((region) => region.blocks).sort((a, b) => a.reading_order - b.reading_order);
}

export function blockOverlayStyle(block: TextBlock, page: PageAnalysis): Record<string, string> {
  const width = page.dimensions.width || 1;
  const height = page.dimensions.height || 1;

  return {
    left: `${(block.x0 / width) * 100}%`,
    top: `${(block.y0 / height) * 100}%`,
    width: `${((block.x1 - block.x0) / width) * 100}%`,
    height: `${((block.y1 - block.y0) / height) * 100}%`,
  };
}

export function structuredBlockOverlayStyle(block: { bbox: BBox }, page: StructuredPage): Record<string, string> {
  const width = page.width || 1;
  const height = page.height || 1;

  return {
    left: `${(block.bbox.x0 / width) * 100}%`,
    top: `${(block.bbox.y0 / height) * 100}%`,
    width: `${((block.bbox.x1 - block.bbox.x0) / width) * 100}%`,
    height: `${((block.bbox.y1 - block.bbox.y0) / height) * 100}%`,
  };
}
