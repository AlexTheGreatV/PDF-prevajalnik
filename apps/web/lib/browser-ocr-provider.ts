import type Tesseract from "tesseract.js";

export const OCR_RUNTIME_PATHS = {
  workerPath: "/vendor/tesseract/worker.min.js",
  corePath: "/vendor/tesseract/tesseract-core.wasm.js",
  langPath: "/vendor/tesseract/lang",
  language: "eng",
} as const;

export const OCR_LIMITS = {
  maxPixels: 2_000_000,
  maxImageBytes: 8 * 1024 * 1024,
  maxWorkers: 1,
} as const;

export type OcrBBox = {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
};

export type BrowserOcrWord = {
  text: string;
  confidence: number;
  pixel_bbox: OcrBBox;
};

export type BrowserOcrSegmentationMode = "single_line" | "single_block" | "sparse_text";

const TESSERACT_PSM_BY_MODE: Record<BrowserOcrSegmentationMode, Tesseract.PSM> = {
  single_line: "7" as Tesseract.PSM,
  single_block: "6" as Tesseract.PSM,
  sparse_text: "11" as Tesseract.PSM,
};

export type BrowserOcrResult = {
  text: string;
  words: BrowserOcrWord[];
  confidence: number;
  pixel_bbox: OcrBBox;
  pdf_bbox: OcrBBox | null;
  language: "eng";
  processing_time_ms: number;
  reason: string;
  preprocessing_method: string;
  segmentation_mode: BrowserOcrSegmentationMode;
};

export type BrowserOcrInput = {
  image: Tesseract.ImageLike;
  pixel_bbox: OcrBBox;
  pdf_bbox?: OcrBBox | null;
  reason: string;
  preprocessing_method?: string;
  segmentation_mode?: BrowserOcrSegmentationMode;
};

type RuntimeAvailability = {
  ok: boolean;
  missing: string[];
};

export function getBrowserOcrRuntimePaths() {
  return OCR_RUNTIME_PATHS;
}

export function explainBrowserOcrError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (/fetch|network|failed|404|not found/i.test(message)) {
    return "OCR ni na voljo, ker lokalne runtime datoteke Tesseract.js niso dosegljive.";
  }
  return `OCR ni uspel: ${message}`;
}

export async function checkBrowserOcrRuntime(fetcher: typeof fetch = fetch): Promise<RuntimeAvailability> {
  const requiredUrls = [
    OCR_RUNTIME_PATHS.workerPath,
    OCR_RUNTIME_PATHS.corePath,
    "/vendor/tesseract/tesseract-core.wasm",
    `${OCR_RUNTIME_PATHS.langPath}/eng.traineddata.gz`,
  ];
  const results = await Promise.all(
    requiredUrls.map(async (url) => {
      try {
        const response = await fetcher(url, { method: "HEAD", cache: "no-store" });
        return { url, ok: response.ok };
      } catch {
        return { url, ok: false };
      }
    }),
  );

  const missing = results.filter((result) => !result.ok).map((result) => result.url);
  return { ok: missing.length === 0, missing };
}

export function findExternalOcrRuntimeUrls(urls: string[]): string[] {
  return urls.filter((url) => {
    try {
      const parsed = new URL(url, globalThis.location?.origin ?? "http://127.0.0.1:3000");
      return parsed.origin !== (globalThis.location?.origin ?? "http://127.0.0.1:3000");
    } catch {
      return false;
    }
  });
}

function assertImageLimits(pixel_bbox: OcrBBox) {
  const width = Math.max(0, pixel_bbox.x1 - pixel_bbox.x0);
  const height = Math.max(0, pixel_bbox.y1 - pixel_bbox.y0);
  if (width * height > OCR_LIMITS.maxPixels) {
    throw new Error("OCR izrez je prevelik za lokalno obdelavo.");
  }
}

function flattenWords(page: Tesseract.Page): BrowserOcrWord[] {
  return (page.blocks ?? []).flatMap((block) =>
    block.paragraphs.flatMap((paragraph) =>
      paragraph.lines.flatMap((line) =>
        line.words.map((word) => ({
          text: word.text,
          confidence: word.confidence,
          pixel_bbox: {
            x0: word.bbox.x0,
            y0: word.bbox.y0,
            x1: word.bbox.x1,
            y1: word.bbox.y1,
          },
        })),
      ),
    ),
  );
}

export class BrowserOcrProvider {
  private workerPromise: Promise<Tesseract.Worker> | null = null;

  async recognize(input: BrowserOcrInput): Promise<BrowserOcrResult> {
    if (typeof input.image === "object" && input.image !== null && "width" in input.image && "height" in input.image) {
      const image = input.image as { width: number; height: number; naturalWidth?: number; naturalHeight?: number };
      assertImageLimits({ x0: 0, y0: 0, x1: image.naturalWidth ?? image.width, y1: image.naturalHeight ?? image.height });
    } else {
      assertImageLimits(input.pixel_bbox);
    }
    const startedAt = performance.now();
    const worker = await this.getWorker();
    const segmentationMode = input.segmentation_mode ?? "single_block";
    await worker.setParameters({
      tessedit_pageseg_mode: TESSERACT_PSM_BY_MODE[segmentationMode],
      preserve_interword_spaces: "1",
    });
    const result = await worker.recognize(
      input.image,
      {},
      {
        blocks: true,
        text: true,
      },
    );

    return {
      text: result.data.text.trim(),
      words: flattenWords(result.data),
      confidence: result.data.confidence,
      pixel_bbox: input.pixel_bbox,
      pdf_bbox: input.pdf_bbox ?? null,
      language: OCR_RUNTIME_PATHS.language,
      processing_time_ms: Math.round(performance.now() - startedAt),
      reason: input.reason,
      preprocessing_method: input.preprocessing_method ?? "original",
      segmentation_mode: segmentationMode,
    };
  }

  async terminate(): Promise<void> {
    const worker = await this.workerPromise;
    this.workerPromise = null;
    await worker?.terminate();
  }

  private getWorker(): Promise<Tesseract.Worker> {
    this.workerPromise ??= this.createConfiguredWorker();
    return this.workerPromise;
  }

  private async createConfiguredWorker(): Promise<Tesseract.Worker> {
    const tesseract = await import("tesseract.js");
    const runtime = tesseract.default ?? tesseract;
    const worker = await runtime.createWorker(
      OCR_RUNTIME_PATHS.language,
      1 as Tesseract.OEM,
      {
        workerPath: OCR_RUNTIME_PATHS.workerPath,
        corePath: OCR_RUNTIME_PATHS.corePath,
        langPath: OCR_RUNTIME_PATHS.langPath,
        workerBlobURL: false,
        gzip: true,
        cacheMethod: "none",
      },
    );
    return worker;
  }
}
