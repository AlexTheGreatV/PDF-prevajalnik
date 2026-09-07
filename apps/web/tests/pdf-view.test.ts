import assert from "node:assert/strict";

import {
  blockOverlayStyle,
  countImages,
  countTextBlocks,
  flattenStructuredBlocks,
  formatBytes,
  formatPdfKind,
  pageText,
  structuredBlockOverlayStyle,
  structuredPageText,
  validatePdfFile,
} from "../lib/pdf-view";
import type { PdfAnalysis } from "../lib/pdf-types";

const analysis: PdfAnalysis = {
  filename: "test.pdf",
  page_count: 1,
  kind: "digital",
  extracted_text_chars: 11,
  pages: [
    {
      page_number: 1,
      kind: "digital",
      extracted_text_chars: 11,
      dimensions: { width: 200, height: 100, unit: "pt" },
      image_count: 2,
      text_blocks: [
        {
          page_number: 1,
          block_number: 0,
          text: "Pozdrav",
          x0: 20,
          y0: 10,
          x1: 120,
          y1: 30,
        },
        {
          page_number: 1,
          block_number: 1,
          text: "JUB",
          x0: 40,
          y0: 40,
          x1: 80,
          y1: 60,
        },
      ],
    },
  ],
};

function test(name: string, run: () => void) {
  run();
  console.log(`ok - ${name}`);
}

test("formats PDF kind labels in Slovene", () => {
  assert.equal(formatPdfKind("digital"), "digitalen");
  assert.equal(formatPdfKind("scanned"), "skeniran");
  assert.equal(formatPdfKind("mixed"), "mešan");
  assert.equal(formatPdfKind("empty"), "prazen");
});

test("formats file sizes", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(2 * 1024 * 1024), "2.0 MB");
});

test("validates PDF file metadata before upload", () => {
  assert.equal(validatePdfFile({ name: "doc.pdf", size: 100, type: "application/pdf" }), null);
  assert.match(
    validatePdfFile({ name: "doc.txt", size: 100, type: "text/plain" }) ?? "",
    /PDF/,
  );
  assert.match(
    validatePdfFile({ name: "doc.pdf", size: 60 * 1024 * 1024, type: "application/pdf" }) ?? "",
    /prevelik/,
  );
});

test("summarizes page content", () => {
  assert.equal(countImages(analysis), 2);
  assert.equal(countTextBlocks(analysis), 2);
  assert.equal(pageText(analysis.pages[0]), "Pozdrav\n\nJUB");
});

test("projects PDF block coordinates to percentages", () => {
  assert.deepEqual(blockOverlayStyle(analysis.pages[0].text_blocks[0], analysis.pages[0]), {
    left: "10%",
    top: "10%",
    width: "50%",
    height: "20%",
  });
});

test("summarizes structured document blocks", () => {
  const structured: PdfAnalysis = {
    ...analysis,
    document: {
      metadata: {},
      pages: [
        {
          page_number: 1,
          width: 200,
          height: 100,
          page_type: "digital",
          images: [],
          regions: [
            {
              kind: "body",
              reading_order: 1,
              bbox: { x0: 20, y0: 10, x1: 120, y1: 40 },
              blocks: [
                {
                  kind: "heading",
                  reading_order: 1,
                  bbox: { x0: 20, y0: 10, x1: 120, y1: 30 },
                  original_text: "NASLOV",
                  normalized_text: "Naslov",
                  lines: [],
                  line_boundaries: [],
                  style: {
                    font: "Helvetica",
                    font_size: 14,
                    color: 0,
                    flags: 16,
                    bold: true,
                    italic: false,
                  },
                  confidence: 1,
                  warnings: [],
                  needs_ocr_check: false,
                },
              ],
            },
          ],
        },
      ],
    },
  };

  const page = structured.document?.pages[0];
  assert.equal(countTextBlocks(structured), 1);
  assert.equal(page ? structuredPageText(page, "normalized") : "", "Naslov");
  assert.equal(page ? flattenStructuredBlocks(page)[0].kind : "", "heading");
  assert.deepEqual(page ? structuredBlockOverlayStyle(flattenStructuredBlocks(page)[0], page) : {}, {
    left: "10%",
    top: "10%",
    width: "50%",
    height: "20%",
  });
});
