import assert from "node:assert/strict";

import {
  OCR_RUNTIME_PATHS,
  checkBrowserOcrRuntime,
  explainBrowserOcrError,
  findExternalOcrRuntimeUrls,
  getBrowserOcrRuntimePaths,
} from "../lib/browser-ocr-provider";
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

test("uses local browser OCR runtime paths", () => {
  const paths = getBrowserOcrRuntimePaths();
  assert.equal(paths.workerPath, "/vendor/tesseract/worker.min.js");
  assert.equal(paths.corePath, "/vendor/tesseract/tesseract-core.wasm.js");
  assert.equal(paths.langPath, "/vendor/tesseract/lang");
  assert.equal(paths.language, "eng");
  assert.ok(!Object.values(paths).some((value) => /^https?:\/\//i.test(value)));
});

test("checks local OCR runtime availability", async () => {
  const requestedUrls: string[] = [];
  const fetcher = async (input: string | URL | Request) => {
    const url = input instanceof Request ? input.url : String(input);
    requestedUrls.push(url);
    return new Response(null, { status: 200 });
  };

  const result = await checkBrowserOcrRuntime(fetcher);
  assert.equal(result.ok, true);
  assert.deepEqual(requestedUrls, [
    OCR_RUNTIME_PATHS.workerPath,
    OCR_RUNTIME_PATHS.corePath,
    "/vendor/tesseract/tesseract-core.wasm",
    `${OCR_RUNTIME_PATHS.langPath}/eng.traineddata.gz`,
  ]);
});

test("reports missing local OCR runtime files clearly", async () => {
  const fetcher = async (input: string | URL | Request) => {
    const url = input instanceof Request ? input.url : String(input);
    return new Response(null, { status: url.includes("eng.traineddata.gz") ? 404 : 200 });
  };

  const result = await checkBrowserOcrRuntime(fetcher);
  assert.equal(result.ok, false);
  assert.deepEqual(result.missing, ["/vendor/tesseract/lang/eng.traineddata.gz"]);
  assert.match(explainBrowserOcrError(new Error("Failed to fetch")), /lokalne runtime datoteke/);
});

test("detects external OCR runtime requests", () => {
  assert.deepEqual(
    findExternalOcrRuntimeUrls([
      "http://127.0.0.1:3000/vendor/tesseract/worker.min.js",
      "https://cdn.jsdelivr.net/npm/tesseract.js/worker.min.js",
    ]),
    ["https://cdn.jsdelivr.net/npm/tesseract.js/worker.min.js"],
  );
});

void Promise.all(pendingTests).catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
