import { BrowserOcrProvider, OCR_LIMITS, type BrowserOcrResult, type OcrBBox } from "./browser-ocr-provider";
import { browserResultToAttempt, chooseOcrSegmentationMode, normalizeBBox, type OcrReviewItem } from "./ocr-review";
import type { StructuredPage } from "./pdf-types";

type OcrPreprocessMethod =
  | "original"
  | "scale-2"
  | "scale-3"
  | "grayscale-2"
  | "contrast-2"
  | "threshold-2";

const PREPROCESS_METHODS: OcrPreprocessMethod[] = [
  "original",
  "scale-2",
  "scale-3",
  "grayscale-2",
  "contrast-2",
  "threshold-2",
];

export async function recognizeBestVariant(
  provider: BrowserOcrProvider,
  image: HTMLImageElement,
  item: OcrReviewItem,
  pixelBBox: OcrBBox,
  page: StructuredPage,
  paddingPx: number,
  signal?: AbortSignal,
): Promise<{
  best: BrowserOcrResult;
  attempts: ReturnType<typeof browserResultToAttempt>[];
  cropImageUrl: string;
  cropDebug: NonNullable<OcrReviewItem["crop_debug"]>;
}> {
  let best: BrowserOcrResult | null = null;
  let bestCanvas: HTMLCanvasElement | null = null;
  const attempts: ReturnType<typeof browserResultToAttempt>[] = [];
  const segmentationMode = chooseOcrSegmentationMode(item);
  for (const method of PREPROCESS_METHODS) {
    signal?.throwIfAborted();
    const canvas = cropImageToCanvas(image, pixelBBox, method);
    const result = await provider.recognize({
      image: canvas,
      pixel_bbox: pixelBBox,
      pdf_bbox: item.pdf_bbox,
      reason: item.reason,
      preprocessing_method: method,
      segmentation_mode: segmentationMode,
    });
    attempts.push(browserResultToAttempt(result, canvas));
    if (!best || scoreOcrResult(result) > scoreOcrResult(best)) {
      best = result;
      bestCanvas = canvas;
    }
    if (result.confidence >= 92 && result.text.trim().length > 0) {
      break;
    }
  }
  if (!best) {
    throw new Error("OCR ni vrnil rezultata.");
  }
  const cropDebug = {
    render_width: image.naturalWidth,
    render_height: image.naturalHeight,
    pdf_bbox: item.pdf_bbox,
    pixel_bbox: pixelBBox,
    canvas_width: bestCanvas?.width ?? 0,
    canvas_height: bestCanvas?.height ?? 0,
    padding_px: paddingPx,
    rotation: 0,
  };
  return {
    best,
    attempts,
    cropImageUrl: bestCanvas?.toDataURL("image/png") ?? "",
    cropDebug,
  };
}

export function cropImageToCanvas(
  image: HTMLImageElement,
  pixelBBox: OcrBBox,
  method: OcrPreprocessMethod,
): HTMLCanvasElement {
  const source = normalizeBBox(pixelBBox);
  const scale = method === "scale-3" ? 3 : method === "original" ? 1 : 2;
  const width = Math.max(1, Math.ceil(source.x1 - source.x0));
  const height = Math.max(1, Math.ceil(source.y1 - source.y0));
  const canvas = document.createElement("canvas");
  const boundedScale = Math.min(scale, Math.sqrt(OCR_LIMITS.maxPixels / (width * height)));
  canvas.width = Math.max(1, Math.floor(width * boundedScale));
  canvas.height = Math.max(1, Math.floor(height * boundedScale));
  const context = canvas.getContext("2d", {
    willReadFrequently: method !== "original" && method !== "scale-2" && method !== "scale-3",
  });
  if (!context) throw new Error("Canvas za OCR ni na voljo.");
  context.imageSmoothingEnabled = true;
  context.fillStyle = "white";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, source.x0, source.y0, source.x1 - source.x0, source.y1 - source.y0, 0, 0, canvas.width, canvas.height);

  if (method === "grayscale-2" || method === "contrast-2" || method === "threshold-2") {
    const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
    const data = imageData.data;
    for (let index = 0; index < data.length; index += 4) {
      let value = Math.round(data[index] * 0.299 + data[index + 1] * 0.587 + data[index + 2] * 0.114);
      if (method === "contrast-2") {
        value = Math.max(0, Math.min(255, (value - 128) * 1.6 + 128));
      }
      if (method === "threshold-2") {
        value = value > 150 ? 255 : 0;
      }
      data[index] = value;
      data[index + 1] = value;
      data[index + 2] = value;
    }
    context.putImageData(imageData, 0, 0);
  }

  return canvas;
}

function scoreOcrResult(result: BrowserOcrResult): number {
  return result.confidence + Math.min(result.text.trim().length, 40) / 10;
}

export function ensureImageReady(image: HTMLImageElement): Promise<void> {
  if (image.complete && image.naturalWidth > 0) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    image.addEventListener("load", () => resolve(), { once: true });
    image.addEventListener("error", () => reject(new Error("PNG predogleda ni bilo mogoče naložiti.")), {
      once: true,
    });
  });
}
