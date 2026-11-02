import assert from "node:assert/strict";

import type { BrowserOcrResult, OcrBBox } from "../lib/browser-ocr-provider";
import type { PdfAnalysis, StructuredBlock, StructuredPage } from "../lib/pdf-types";
import {
  acceptOcrResult,
  nativeSourceReviewWarning,
  actionableWarningReasons,
  applyOcrSuggestion,
  browserResultToAttempt,
  chooseOcrSegmentationMode,
  createManualOcrItem,
  createOcrReviewItems,
  displaySelectionToPdfBBox,
  isAmbiguousHyphenOnly,
  markOcrFailed,
  pdfBBoxToPixelBBox,
  processOcrItemsSequentially,
  relativeOcrPadding,
  rejectOcrResult,
} from "../lib/ocr-review";

const pendingTests: Promise<void>[] = [];

function test(name: string, run: () => void | Promise<void>) {
  pendingTests.push(
    Promise.resolve()
      .then(run)
      .then(() => {
        console.log(`ok - ${name}`);
      }),
  );
}

function block(overrides: Partial<StructuredBlock>): StructuredBlock {
  return {
    kind: "paragraph",
    bbox: { x0: 10, y0: 20, x1: 110, y1: 40 },
    original_text: "qPPLICATIONS",
    normalized_text: "qPPLICATIONS",
    lines: [],
    line_boundaries: [],
    style: { font: "Helvetica", font_size: 10, color: 0, flags: 0, bold: false, italic: false },
    reading_order: 1,
    confidence: 0.35,
    warnings: ["suspicious_glyph:qPPLICATIONS"],
    needs_ocr_check: true,
    ...overrides,
  };
}

function page(overrides: Partial<StructuredPage> = {}): StructuredPage {
  return {
    page_number: 1,
    width: 200,
    height: 100,
    rotation: 0,
    page_type: "digital",
    images: [],
    regions: [
      {
        kind: "body",
        bbox: { x0: 0, y0: 0, x1: 200, y1: 100 },
        reading_order: 1,
        blocks: [block({})],
      },
    ],
    ...overrides,
  };
}

function analysis(structuredPage = page()): PdfAnalysis {
  return {
    filename: "test.pdf",
    sha256: "a".repeat(64),
    page_count: 1,
    document_model_version: 1,
    kind: structuredPage.page_type,
    extracted_text_chars: 12,
    pages: [
      {
        page_number: structuredPage.page_number,
        kind: structuredPage.page_type,
        extracted_text_chars: 12,
        dimensions: { width: structuredPage.width, height: structuredPage.height, unit: "pt" },
        rotation: structuredPage.rotation,
        text_blocks: [],
        image_count: structuredPage.images.length,
      },
    ],
    document: { metadata: {}, pages: [structuredPage] },
  };
}

function ocrResult(text: string, pixel_bbox: OcrBBox): BrowserOcrResult {
  return {
    text,
    words: [{ text, confidence: 96, pixel_bbox }],
    confidence: 96,
    pixel_bbox,
    pdf_bbox: { x0: 10, y0: 20, x1: 110, y1: 40 },
    language: "eng",
    processing_time_ms: 42,
    reason: "suspicious_glyph",
    preprocessing_method: "original",
    segmentation_mode: "single_line",
  };
}

test("creates OCR candidates only for actionable warnings", () => {
  const items = createOcrReviewItems(analysis());
  assert.equal(items.length, 1);
  assert.equal(items[0].reason, "suspicious_glyph");
  assert.equal(items[0].native_text, "qPPLICATIONS");
});

test("maps backend low-confidence suspicious text warning to OCR review", () => {
  const items = createOcrReviewItems(
    analysis(
      page({
        regions: [
          {
            kind: "body",
            bbox: { x0: 0, y0: 0, x1: 200, y1: 100 },
            reading_order: 1,
            blocks: [block({ warnings: ["suspicious_text_low_confidence"], needs_ocr_check: true })],
          },
        ],
      }),
    ),
  );

  assert.equal(items.some((item) => item.reason === "image_text_candidate"), false);
  assert.equal(items[0].reason, "suspicious_glyph");
});

test("does not automatically queue all images on digital pages", () => {
  const items = createOcrReviewItems(
    analysis(
      page({
        images: [
          {
            bbox: { x0: 0, y0: 0, x1: 50, y1: 50 },
            width: 100,
            height: 100,
            xref: 1,
            needs_ocr_check: true,
            ocr_text: null,
            ocr_confidence: null,
          },
        ],
      }),
    ),
  );

  assert.equal(items.some((item) => item.reason === "image_text_candidate"), false);
  assert.equal(items[0].reason, "suspicious_glyph");
});

test("queues image OCR candidates on scanned or low-text pages", () => {
  const items = createOcrReviewItems(
    analysis(
      page({
        page_type: "scanned",
        regions: [],
        images: [
          {
            bbox: { x0: 0, y0: 0, x1: 50, y1: 50 },
            width: 100,
            height: 100,
            xref: 1,
            needs_ocr_check: true,
            ocr_text: null,
            ocr_confidence: null,
          },
        ],
      }),
    ),
  );

  assert.ok(items.some((item) => item.reason === "scanned_page"));
  assert.ok(items.some((item) => item.reason === "image_text_candidate"));
});

test("does not trigger OCR for ambiguous hyphen alone", () => {
  const items = createOcrReviewItems(
    analysis(
      page({
        regions: [
          {
            kind: "body",
            bbox: { x0: 0, y0: 0, x1: 200, y1: 100 },
            reading_order: 1,
            blocks: [
              block({
                warnings: ["ambiguous_hyphen", "hyphenation_ambiguous"],
                needs_ocr_check: true,
                original_text: "moisture-curing",
              }),
            ],
          },
        ],
      }),
    ),
  );

  assert.equal(isAmbiguousHyphenOnly(["ambiguous_hyphen"]), true);
  assert.deepEqual(actionableWarningReasons(["hyphenation_ambiguous"]), []);
  assert.equal(items.length, 0);
});

test("keeps native text immutable when OCR is suggested, accepted, or rejected", () => {
  const item = createOcrReviewItems(analysis())[0];
  const suggested = applyOcrSuggestion(item, ocrResult("APPLICATIONS", { x0: 8, y0: 18, x1: 112, y1: 42 }));
  assert.equal(suggested.native_text, "qPPLICATIONS");
  assert.equal(suggested.effective_text, "qPPLICATIONS");

  const accepted = acceptOcrResult(suggested);
  assert.equal(accepted.native_text, "qPPLICATIONS");
  assert.equal(accepted.selected_text_source, "ocr");
  assert.equal(accepted.effective_text, "APPLICATIONS");

  const rejected = rejectOcrResult(suggested);
  assert.equal(rejected.native_text, "qPPLICATIONS");
  assert.equal(rejected.selected_text_source, "native");
  assert.equal(rejected.effective_text, "qPPLICATIONS");
});

test("low confidence stays a suggestion until explicitly accepted", () => {
  const item = createOcrReviewItems(analysis())[0];
  const suggested = applyOcrSuggestion(item, { ...ocrResult("APPLICATIONS", item.pdf_bbox), confidence: 49 });
  assert.equal(suggested.status, "suggested");
  assert.equal(suggested.effective_text, item.native_text);
  assert.equal(acceptOcrResult(suggested).effective_text, "APPLICATIONS");
  assert.equal(acceptOcrResult(suggested).native_text, item.native_text);
});

test("an accepted OCR result can be re-registered after server expiry without changing native text", () => {
  const item = createOcrReviewItems(analysis())[0];
  const suggested = applyOcrSuggestion(item, { ...ocrResult("APPLICATIONS", item.pdf_bbox), confidence: 49 });
  const accepted = acceptOcrResult(suggested, "old-revision");
  const renewed = acceptOcrResult(accepted, "new-revision");
  assert.equal(renewed.server_revision_id, "new-revision");
  assert.equal(renewed.native_text, suggested.native_text);
  assert.equal(renewed.effective_text, "APPLICATIONS");
});

test("empty OCR preserves its crop and requires manual review", () => {
  const item = createOcrReviewItems(analysis())[0];
  const result = applyOcrSuggestion(item, ocrResult("", item.pdf_bbox), [], "data:image/png;base64,test");
  assert.equal(result.status, "needs_review");
  assert.match(result.error ?? "", /ročni pregled/);
  assert.equal(result.crop_image_url, "data:image/png;base64,test");
  assert.equal(acceptOcrResult(result).effective_text, item.native_text);
});

test("OCR text is preserved without synthesizing spatial meaning", () => {
  const item = createOcrReviewItems(analysis())[0];
  const text = "Upper\n\nLower";
  const suggested = applyOcrSuggestion(item, ocrResult(text, item.pdf_bbox));
  assert.equal(suggested.ocr_text, text);
  assert.equal(acceptOcrResult(suggested).effective_text, text);
});

test("maps PDF bbox to natural pixels with padding and clamps at page edges", () => {
  assert.deepEqual(
    pdfBBoxToPixelBBox({ x0: 10, y0: 10, x1: 50, y1: 30 }, { width: 100, height: 50 }, { naturalWidth: 1000, naturalHeight: 500 }, 5),
    { x0: 95, y0: 95, x1: 505, y1: 305 },
  );
  assert.deepEqual(
    pdfBBoxToPixelBBox({ x0: -10, y0: -10, x1: 5, y1: 5 }, { width: 100, height: 50 }, { naturalWidth: 1000, naturalHeight: 500 }, 20),
    { x0: 0, y0: 0, x1: 70, y1: 70 },
  );
});

test("computes relative OCR padding without exceeding safe bounds", () => {
  assert.equal(relativeOcrPadding({ x0: 10, y0: 20, x1: 50, y1: 30 }), 16);
  assert.equal(relativeOcrPadding({ x0: 10, y0: 20, x1: 210, y1: 50 }), 36);
  assert.equal(relativeOcrPadding({ x0: 10, y0: 20, x1: 810, y1: 80 }), 48);
});

test("maps scaled manual preview selections back to PDF coordinates", () => {
  const pdfBox = displaySelectionToPdfBBox(
    { x0: 20, y0: 10, x1: 120, y1: 60 },
    { width: 200, height: 100 },
    { naturalWidth: 1000, naturalHeight: 500, displayWidth: 500, displayHeight: 250 },
  );
  assert.deepEqual(pdfBox, { x0: 8, y0: 4, x1: 48, y1: 24 });
});

test("supports rotated page coordinate mapping", () => {
  assert.deepEqual(
    pdfBBoxToPixelBBox({ x0: 10, y0: 10, x1: 30, y1: 20 }, { width: 100, height: 50 }, { naturalWidth: 500, naturalHeight: 1000 }, 0, 90),
    { x0: 300, y0: 100, x1: 400, y1: 300 },
  );
});

test("chooses segmentation mode by OCR area type", () => {
  const suspiciousHeading = createOcrReviewItems(analysis())[0];
  assert.equal(chooseOcrSegmentationMode(suspiciousHeading), "single_line");

  const manual = createManualOcrItem(page(), { x0: 5, y0: 6, x1: 60, y1: 80 });
  assert.equal(chooseOcrSegmentationMode(manual), "sparse_text");

  const paragraph = {
    ...suspiciousHeading,
    native_text: "Line one\nLine two",
  };
  assert.equal(chooseOcrSegmentationMode(paragraph), "single_block");
});

test("creates manual OCR items without changing the document model", () => {
  const sourcePage = page();
  const item = createManualOcrItem(sourcePage, { x0: 5, y0: 6, x1: 60, y1: 20 });
  assert.equal(item.reason, "manual_selection");
  assert.equal(item.source, "manual");
  assert.equal(item.native_text, "");
  assert.deepEqual(sourcePage.regions[0].blocks[0].original_text, "qPPLICATIONS");
});

test("records OCR errors while preserving native text", () => {
  const item = createOcrReviewItems(analysis())[0];
  const failed = markOcrFailed(item, "Manjka lokalni worker.");
  assert.equal(failed.status, "failed");
  assert.equal(failed.error, "Manjka lokalni worker.");
  assert.equal(failed.effective_text, "qPPLICATIONS");
});

test("keeps OCR attempts as alternatives without accepting OCR automatically", () => {
  const item = createOcrReviewItems(analysis())[0];
  const first = {
    ...ocrResult("qPPLICATIONS", { x0: 8, y0: 18, x1: 112, y1: 42 }),
    confidence: 84,
    preprocessing_method: "original",
  };
  const second = {
    ...ocrResult("APPLICATIONS", { x0: 8, y0: 18, x1: 112, y1: 42 }),
    confidence: 91,
    preprocessing_method: "contrast-2",
  };
  const attempts = [
    browserResultToAttempt(first, { width: 120, height: 48 }),
    browserResultToAttempt(second, { width: 240, height: 96 }),
  ];
  const suggested = applyOcrSuggestion(item, second, attempts, "data:image/png;base64,test", {
    render_width: 1000,
    render_height: 500,
    pdf_bbox: item.pdf_bbox,
    pixel_bbox: second.pixel_bbox,
    canvas_width: 240,
    canvas_height: 96,
    padding_px: 16,
    rotation: 0,
  });

  assert.equal(suggested.ocr_text, "APPLICATIONS");
  assert.equal(suggested.effective_text, "qPPLICATIONS");
  assert.equal(suggested.selected_text_source, "native");
  assert.equal(suggested.attempts.length, 2);
  assert.equal(suggested.attempts[1].preprocessing_method, "contrast-2");
  assert.equal(suggested.crop_debug?.canvas_width, 240);
});

test("processes OCR work sequentially and stops on cancel", async () => {
  const items = [
    createOcrReviewItems(analysis())[0],
    createOcrReviewItems(analysis())[0],
    createOcrReviewItems(analysis())[0],
  ];
  const controller = new AbortController();
  const order: number[] = [];
  const processed = await processOcrItemsSequentially(
    items,
    async (item, index) => {
      order.push(index);
      if (index === 0) {
        controller.abort();
      }
      return applyOcrSuggestion(item, ocrResult(`APPLICATIONS ${index}`, { x0: 0, y0: 0, x1: 10, y1: 10 }));
    },
    () => undefined,
    controller.signal,
  );

  assert.deepEqual(order, [0]);
  assert.equal(processed.length, 1);
});

test("continues the queue after a failed area", async () => {
  const item = createOcrReviewItems(analysis())[0];
  const results = await processOcrItemsSequentially([item, item], async (candidate, index) => {
    if (index === 0) throw new Error("Area failed");
    return applyOcrSuggestion(candidate, ocrResult("Other", candidate.pdf_bbox));
  }, () => undefined);
  assert.deepEqual(results.map((result) => result.status), ["failed", "suggested"]);
  assert.ok(results.every((result) => result.native_text === item.native_text));
});

test("normalizing a manual selection preserves its page identity", async () => {
  const { normalizeBBox } = await import("../lib/ocr-review");
  const selection = normalizeBBox({ page_number: 2, x0: 60, y0: 30, x1: 10, y1: 5 });
  assert.equal(selection.page_number, 2);
  assert.equal(selection.x0, 10);
  const repeated = normalizeBBox({ ...selection, x0: 12, x1: 40 });
  assert.equal(repeated.page_number, 2);
  assert.equal(repeated.x0, 12);
});

test("manual single-line selection can override sparse segmentation", () => {
  const item = createManualOcrItem(page(), { x0: 5, y0: 5, x1: 60, y1: 15 });
  assert.equal(chooseOcrSegmentationMode({ ...item, segmentation_mode: "single_line" }), "single_line");
});

test("forbids external OCR runtime URLs", async () => {
  const { findExternalOcrRuntimeUrls } = await import("../lib/browser-ocr-provider");
  const external = findExternalOcrRuntimeUrls([
    "/vendor/tesseract/worker.min.js",
    "http://127.0.0.1:3000/vendor/tesseract/lang/eng.traineddata.gz",
    "https://unpkg.com/tesseract.js/dist/worker.min.js",
  ]);
  assert.deepEqual(external, ["https://unpkg.com/tesseract.js/dist/worker.min.js"]);
});

test("native source warnings use the current analysis candidate, not target wording or provider QC", () => {
  const item = createOcrReviewItems(analysis())[0];
  const source = { source_text_origin: "native" as const, source_bbox: item.pdf_bbox,
    source_text: item.native_text, page_number: item.page_number, block_id: `p${item.page_number}-r1-b${item.linked_block_reading_order}` };
  const before = JSON.stringify(source);
  assert.match(nativeSourceReviewWarning(source, [item])!, /Sumljiv izvor/);
  assert.equal(JSON.stringify(source), before);
  assert.equal(nativeSourceReviewWarning({ ...source, source_text_origin: "accepted_ocr" }, [item]), null);
  assert.equal(nativeSourceReviewWarning(source, []), null);
  assert.equal(nativeSourceReviewWarning({ ...source, page_number: source.page_number + 1 }, [item]), null);
  assert.equal(nativeSourceReviewWarning({ ...source, source_bbox: { ...item.pdf_bbox, x0: item.pdf_bbox.x0 + 1 } }, [item]), null);
  assert.equal(nativeSourceReviewWarning({ ...source, source_text: "Different source" }, [item]), null);
});

void Promise.all(pendingTests).catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
