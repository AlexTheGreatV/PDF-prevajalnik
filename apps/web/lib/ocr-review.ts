import type { BrowserOcrResult, BrowserOcrSegmentationMode, OcrBBox } from "./browser-ocr-provider";
import type { PdfAnalysis, StructuredBlock, StructuredPage } from "./pdf-types";

export type OcrReviewStatus = "pending" | "processing" | "suggested" | "accepted" | "rejected" | "failed" | "needs_review";
export type OcrTextSource = "native" | "ocr";
export type OcrReviewReason =
  | "suspicious_glyph"
  | "font_encoding_anomaly"
  | "scanned_page"
  | "low_native_text"
  | "image_text_candidate"
  | "manual_selection";

export type PageImageMetrics = {
  naturalWidth: number;
  naturalHeight: number;
  displayWidth: number;
  displayHeight: number;
};

export type PageSize = {
  width: number;
  height: number;
};

export type OcrAttempt = {
  text: string;
  confidence: number;
  preprocessing_method: string;
  segmentation_mode: BrowserOcrSegmentationMode;
  processing_time_ms: number;
  pixel_bbox: OcrBBox;
  canvas_width: number;
  canvas_height: number;
};

export type OcrCropDebug = {
  render_width: number;
  render_height: number;
  pdf_bbox: OcrBBox;
  pixel_bbox: OcrBBox;
  canvas_width: number;
  canvas_height: number;
  padding_px: number;
  rotation: number;
};

export type OcrReviewItem = {
  id: string;
  page_number: number;
  reason: OcrReviewReason;
  pdf_bbox: OcrBBox;
  pixel_bbox: OcrBBox | null;
  native_text: string;
  ocr_text: string | null;
  confidence: number | null;
  status: OcrReviewStatus;
  selected_text_source: OcrTextSource;
  effective_text: string;
  error: string | null;
  source: "block" | "page" | "image" | "manual";
  linked_block_reading_order?: number;
  warnings: string[];
  preprocessing_method?: string;
  segmentation_mode?: BrowserOcrSegmentationMode;
  attempts: OcrAttempt[];
  crop_image_url: string | null;
  crop_debug: OcrCropDebug | null;
  server_revision_id: string | null;
};

export type DisplaySelection = {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
};

const IGNORED_WARNING_MARKERS = ["ambiguous_hyphen", "hyphenation_ambiguous"] as const;

export function createOcrReviewItems(analysis: PdfAnalysis): OcrReviewItem[] {
  const documentPages = analysis.document?.pages ?? [];
  const blockItems: OcrReviewItem[] = [];
  const pageItems: OcrReviewItem[] = [];
  const imageItems: OcrReviewItem[] = [];

  for (const page of documentPages) {
    const pageTextChars = getPageNativeTextLength(page);
    const isScanned = page.page_type === "scanned";
    const isLowNativeText = pageTextChars < 20 && page.images.length > 0;

    if (isScanned || isLowNativeText) {
      pageItems.push(
        createBaseItem({
          id: `page-${page.page_number}-${isScanned ? "scanned" : "low-text"}`,
          page_number: page.page_number,
          reason: isScanned ? "scanned_page" : "low_native_text",
          pdf_bbox: { x0: 0, y0: 0, x1: page.width, y1: page.height },
          native_text: "",
          source: "page",
          warnings: [],
        }),
      );
    }

    for (const block of flattenPageBlocks(page)) {
      const reasons = actionableWarningReasons(block.warnings ?? []);
      if (!block.needs_ocr_check || reasons.length === 0) {
        continue;
      }
      for (const reason of reasons) {
        blockItems.push(
          createBaseItem({
            id: `page-${page.page_number}-block-${block.reading_order}-${reason}`,
            page_number: page.page_number,
            reason,
            pdf_bbox: block.bbox,
            native_text: block.original_text,
            source: "block",
            linked_block_reading_order: block.reading_order,
            warnings: block.warnings ?? [],
          }),
        );
      }
    }

    page.images.forEach((image, index) => {
      const shouldQueueImage = image.needs_ocr_check && image.bbox && page.page_type !== "digital";
      if (shouldQueueImage && image.bbox) {
        imageItems.push(
          createBaseItem({
            id: `page-${page.page_number}-image-${index + 1}`,
            page_number: page.page_number,
            reason: "image_text_candidate",
            pdf_bbox: image.bbox,
            native_text: "",
            source: "image",
            warnings: ["image_text_candidate"],
          }),
        );
      }
    });
  }

  return dedupeItems([...blockItems, ...pageItems, ...imageItems]);
}

export function createManualOcrItem(page: StructuredPage, pdf_bbox: OcrBBox): OcrReviewItem {
  return createBaseItem({
    id: `page-${page.page_number}-manual-${Math.round(pdf_bbox.x0)}-${Math.round(pdf_bbox.y0)}-${Date.now()}`,
    page_number: page.page_number,
    reason: "manual_selection",
    pdf_bbox,
    native_text: "",
    source: "manual",
    warnings: ["manual_selection"],
  });
}

export function actionableWarningReasons(warnings: string[]): OcrReviewReason[] {
  const normalized = warnings.map((warning) => warning.toLowerCase());
  const hasOnlyIgnored =
    normalized.length > 0 && normalized.every((warning) => IGNORED_WARNING_MARKERS.some((marker) => warning.includes(marker)));
  if (hasOnlyIgnored) {
    return [];
  }

  const reasons: OcrReviewReason[] = [];
  if (
    normalized.some(
      (warning) =>
        warning.includes("suspicious_glyph") || warning.includes("suspicious_text_low_confidence"),
    )
  ) {
    reasons.push("suspicious_glyph");
  }
  if (normalized.some((warning) => warning.includes("font_encoding_anomaly"))) {
    reasons.push("font_encoding_anomaly");
  }
  return reasons;
}

export function isAmbiguousHyphenOnly(warnings: string[]): boolean {
  return (
    warnings.length > 0 &&
    warnings.every((warning) =>
      IGNORED_WARNING_MARKERS.some((marker) => warning.toLowerCase().includes(marker)),
    )
  );
}

export function chooseOcrSegmentationMode(item: OcrReviewItem): BrowserOcrSegmentationMode {
  if (item.segmentation_mode) return item.segmentation_mode;
  if (item.reason === "manual_selection" || item.reason === "image_text_candidate") {
    return "sparse_text";
  }
  if (item.reason === "scanned_page" || item.reason === "low_native_text") {
    return "sparse_text";
  }
  if (!item.native_text.includes("\n") && item.native_text.trim().length <= 80) {
    return "single_line";
  }
  return "single_block";
}

export function browserResultToAttempt(
  result: BrowserOcrResult,
  canvas: { width: number; height: number },
): OcrAttempt {
  return {
    text: result.text,
    confidence: result.confidence,
    preprocessing_method: result.preprocessing_method,
    segmentation_mode: result.segmentation_mode,
    processing_time_ms: result.processing_time_ms,
    pixel_bbox: result.pixel_bbox,
    canvas_width: canvas.width,
    canvas_height: canvas.height,
  };
}

export function relativeOcrPadding(
  pixel_bbox: OcrBBox,
  minPaddingPx = 16,
  maxPaddingPx = 48,
): number {
  const normalized = normalizeBBox(pixel_bbox);
  const width = Math.max(1, normalized.x1 - normalized.x0);
  const height = Math.max(1, normalized.y1 - normalized.y0);
  const relativePadding = Math.max(width * 0.18, height * 1.1);
  return Math.round(clamp(relativePadding, minPaddingPx, maxPaddingPx));
}

export function applyOcrSuggestion(
  item: OcrReviewItem,
  result: BrowserOcrResult,
  attempts: OcrAttempt[] = [],
  crop_image_url: string | null = null,
  crop_debug: OcrCropDebug | null = null,
): OcrReviewItem {
  return {
    ...item,
    status: result.text.trim() ? "suggested" : "needs_review",
    ocr_text: result.text,
    confidence: result.confidence,
    pixel_bbox: result.pixel_bbox,
    pdf_bbox: result.pdf_bbox ?? item.pdf_bbox,
    error: result.text.trim() ? null : "OCR ni prepoznal besedila – potreben je ročni pregled.",
    preprocessing_method: result.preprocessing_method,
    segmentation_mode: result.segmentation_mode,
    attempts,
    crop_image_url,
    crop_debug,
    server_revision_id: null,
    selected_text_source: "native",
    effective_text: item.native_text,
  };
}

export function acceptOcrResult(item: OcrReviewItem, serverRevisionId?: string): OcrReviewItem {
  if (!item.ocr_text?.trim() || !["suggested", "rejected", "accepted"].includes(item.status)) return item;
  return {
    ...item,
    status: "accepted",
    selected_text_source: "ocr",
    effective_text: item.ocr_text ?? "",
    server_revision_id: serverRevisionId ?? null,
  };
}

export function rejectOcrResult(item: OcrReviewItem): OcrReviewItem {
  return {
    ...item,
    status: "rejected",
    selected_text_source: "native",
    effective_text: item.native_text,
    server_revision_id: null,
  };
}

export function markOcrProcessing(item: OcrReviewItem, pixel_bbox: OcrBBox): OcrReviewItem {
  return { ...item, status: "processing", pixel_bbox, error: null };
}

export function markOcrFailed(item: OcrReviewItem, error: string): OcrReviewItem {
  return { ...item, status: "failed", error, selected_text_source: "native", effective_text: item.native_text };
}

export async function processOcrItemsSequentially(
  items: OcrReviewItem[],
  recognize: (item: OcrReviewItem, index: number) => Promise<OcrReviewItem>,
  onItem: (item: OcrReviewItem, index: number) => void,
  signal?: AbortSignal,
): Promise<OcrReviewItem[]> {
  const processed: OcrReviewItem[] = [];
  for (let index = 0; index < items.length; index += 1) {
    if (signal?.aborted) {
      break;
    }
    let result: OcrReviewItem;
    try {
      result = await recognize(items[index], index);
    } catch (error) {
      if (signal?.aborted) break;
      result = markOcrFailed(items[index], error instanceof Error ? error.message : String(error));
    }
    processed.push(result);
    onItem(result, index);
  }
  return processed;
}

export function pdfBBoxToPixelBBox(
  pdf_bbox: OcrBBox,
  page: PageSize,
  image: Pick<PageImageMetrics, "naturalWidth" | "naturalHeight">,
  paddingPx = 0,
  rotation = 0,
): OcrBBox {
  const normalized = normalizeBBox(pdf_bbox);
  const rotate = normalizedRotation(rotation);
  let mapped: OcrBBox;

  if (rotate === 90) {
    mapped = {
      x0: ((page.height - normalized.y1) / page.height) * image.naturalWidth,
      y0: (normalized.x0 / page.width) * image.naturalHeight,
      x1: ((page.height - normalized.y0) / page.height) * image.naturalWidth,
      y1: (normalized.x1 / page.width) * image.naturalHeight,
    };
  } else if (rotate === 180) {
    mapped = {
      x0: ((page.width - normalized.x1) / page.width) * image.naturalWidth,
      y0: ((page.height - normalized.y1) / page.height) * image.naturalHeight,
      x1: ((page.width - normalized.x0) / page.width) * image.naturalWidth,
      y1: ((page.height - normalized.y0) / page.height) * image.naturalHeight,
    };
  } else if (rotate === 270) {
    mapped = {
      x0: (normalized.y0 / page.height) * image.naturalWidth,
      y0: ((page.width - normalized.x1) / page.width) * image.naturalHeight,
      x1: (normalized.y1 / page.height) * image.naturalWidth,
      y1: ((page.width - normalized.x0) / page.width) * image.naturalHeight,
    };
  } else {
    mapped = {
      x0: (normalized.x0 / page.width) * image.naturalWidth,
      y0: (normalized.y0 / page.height) * image.naturalHeight,
      x1: (normalized.x1 / page.width) * image.naturalWidth,
      y1: (normalized.y1 / page.height) * image.naturalHeight,
    };
  }

  return clampBBox(expandBBox(mapped, paddingPx), image.naturalWidth, image.naturalHeight);
}

export function displaySelectionToPdfBBox(
  selection: DisplaySelection,
  page: PageSize,
  image: PageImageMetrics,
  rotation = 0,
): OcrBBox {
  const displayBox = normalizeBBox(selection);
  const naturalBox = {
    x0: (displayBox.x0 / image.displayWidth) * image.naturalWidth,
    y0: (displayBox.y0 / image.displayHeight) * image.naturalHeight,
    x1: (displayBox.x1 / image.displayWidth) * image.naturalWidth,
    y1: (displayBox.y1 / image.displayHeight) * image.naturalHeight,
  };
  const rotate = normalizedRotation(rotation);

  if (rotate === 90) {
    return normalizeBBox({
      x0: (naturalBox.y0 / image.naturalHeight) * page.width,
      y0: page.height - (naturalBox.x1 / image.naturalWidth) * page.height,
      x1: (naturalBox.y1 / image.naturalHeight) * page.width,
      y1: page.height - (naturalBox.x0 / image.naturalWidth) * page.height,
    });
  }
  if (rotate === 180) {
    return normalizeBBox({
      x0: page.width - (naturalBox.x1 / image.naturalWidth) * page.width,
      y0: page.height - (naturalBox.y1 / image.naturalHeight) * page.height,
      x1: page.width - (naturalBox.x0 / image.naturalWidth) * page.width,
      y1: page.height - (naturalBox.y0 / image.naturalHeight) * page.height,
    });
  }
  if (rotate === 270) {
    return normalizeBBox({
      x0: page.width - (naturalBox.y1 / image.naturalHeight) * page.width,
      y0: (naturalBox.x0 / image.naturalWidth) * page.height,
      x1: page.width - (naturalBox.y0 / image.naturalHeight) * page.width,
      y1: (naturalBox.x1 / image.naturalWidth) * page.height,
    });
  }

  return normalizeBBox({
    x0: (naturalBox.x0 / image.naturalWidth) * page.width,
    y0: (naturalBox.y0 / image.naturalHeight) * page.height,
    x1: (naturalBox.x1 / image.naturalWidth) * page.width,
    y1: (naturalBox.y1 / image.naturalHeight) * page.height,
  });
}

export function clampBBox(bbox: OcrBBox, maxWidth: number, maxHeight: number): OcrBBox {
  const normalized = normalizeBBox(bbox);
  return {
    x0: clamp(normalized.x0, 0, maxWidth),
    y0: clamp(normalized.y0, 0, maxHeight),
    x1: clamp(normalized.x1, 0, maxWidth),
    y1: clamp(normalized.y1, 0, maxHeight),
  };
}

export function normalizeBBox<T extends OcrBBox | DisplaySelection>(bbox: T): T {
  return {
    ...bbox,
    x0: Math.min(bbox.x0, bbox.x1),
    y0: Math.min(bbox.y0, bbox.y1),
    x1: Math.max(bbox.x0, bbox.x1),
    y1: Math.max(bbox.y0, bbox.y1),
  };
}

function createBaseItem(
  input: Omit<
    OcrReviewItem,
    | "pixel_bbox"
    | "ocr_text"
    | "confidence"
    | "status"
    | "selected_text_source"
    | "effective_text"
    | "error"
    | "attempts"
    | "crop_image_url"
    | "crop_debug"
    | "server_revision_id"
  >,
): OcrReviewItem {
  return {
    ...input,
    pixel_bbox: null,
    ocr_text: null,
    confidence: null,
    status: "pending",
    selected_text_source: "native",
    effective_text: input.native_text,
    error: null,
    attempts: [],
    crop_image_url: null,
    crop_debug: null,
    server_revision_id: null,
  };
}

function flattenPageBlocks(page: StructuredPage): StructuredBlock[] {
  return page.regions.flatMap((region) => region.blocks);
}

function getPageNativeTextLength(page: StructuredPage): number {
  return flattenPageBlocks(page).reduce((total, block) => total + block.original_text.length, 0);
}

function dedupeItems(items: OcrReviewItem[]): OcrReviewItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) {
      return false;
    }
    seen.add(item.id);
    return true;
  });
}

function expandBBox(bbox: OcrBBox, padding: number): OcrBBox {
  return {
    x0: bbox.x0 - padding,
    y0: bbox.y0 - padding,
    x1: bbox.x1 + padding,
    y1: bbox.y1 + padding,
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function normalizedRotation(rotation: number): 0 | 90 | 180 | 270 {
  const value = ((rotation % 360) + 360) % 360;
  if (value === 90 || value === 180 || value === 270) {
    return value;
  }
  return 0;
}
