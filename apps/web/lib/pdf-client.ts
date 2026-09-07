import type { PdfAnalysis, RenderResult } from "./pdf-types";

export const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function analyzePdf(file: File): Promise<PdfAnalysis> {
  const form = new FormData();
  form.append("file", file);

  const response = await fetchApi(`${apiBaseUrl}/api/pdf/analyze`, form);

  return parseApiResponse<PdfAnalysis>(response);
}

export async function renderPdf(file: File): Promise<RenderResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("dpi", "110");

  const response = await fetchApi(`${apiBaseUrl}/api/pdf/render`, form);

  return parseApiResponse<RenderResult>(response);
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
