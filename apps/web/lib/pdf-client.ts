import type { OcrResponse, OcrStatus, PdfAnalysis, RenderResult } from "./pdf-types";

export const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function analyzePdf(file: File): Promise<PdfAnalysis> {
  const form = new FormData();
  form.append("file", file);

  const response = await fetchApi(`${apiBaseUrl}/api/pdf/analyze`, form);

  return parseApiResponse<PdfAnalysis>(response);
}

export async function renderPdf(file: File, options: { dpi?: number; page_number?: number } = {}): Promise<RenderResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("dpi", String(options.dpi ?? 110));
  if (options.page_number !== undefined) form.append("page_number", String(options.page_number));

  const response = await fetchApi(`${apiBaseUrl}/api/pdf/render`, form);

  return parseApiResponse<RenderResult>(response);
}

export async function getOcrStatus(): Promise<OcrStatus> {
  const response = await fetch(`${apiBaseUrl}/api/ocr/status`);
  return parseApiResponse<OcrStatus>(response);
}

export async function ocrPdf(file: File): Promise<OcrResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("language", "eng");
  form.append("dpi", "180");

  const response = await fetchApi(`${apiBaseUrl}/api/pdf/ocr`, form);

  return parseApiResponse<OcrResponse>(response);
}

async function fetchApi(url: string, body: FormData): Promise<Response> {
  try {
    return await fetch(url, {
      method: "POST",
      body,
    });
  } catch (error) {
    throw new Error(
      "Povezava s strežnikom za analizo ni uspela. Preverite, ali je API zagnan.",
      { cause: error },
    );
  }
}

async function parseApiResponse<T>(response: Response): Promise<T> {
  if (response.ok) {
    return (await response.json()) as T;
  }

  let detail = "Zahteva ni uspela.";
  try {
    const payload = (await response.json()) as { detail?: string };
    detail = payload.detail ?? detail;
  } catch {
    detail = response.statusText || detail;
  }

  throw new Error(detail);
}
