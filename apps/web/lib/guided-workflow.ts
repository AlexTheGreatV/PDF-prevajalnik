import type { OcrReviewItem } from "./ocr-review";
import type { PdfExportPlan } from "./pdf-export-client";
import type { TranslationJob } from "./translation-types";
import { isTranslationUnitSegment } from "./translation-units";

export type WorkspaceMode = "simple" | "advanced";

export const WORKSPACE_MODE_STORAGE_KEY = "pdf-translator-workspace-mode";

export type WorkspaceModeStorage = Pick<Storage, "getItem" | "setItem">;

export function readWorkspaceMode(storage: WorkspaceModeStorage | null | undefined): WorkspaceMode {
  try {
    const saved = storage?.getItem(WORKSPACE_MODE_STORAGE_KEY);
    return saved === "advanced" || saved === "simple" ? saved : "simple";
  } catch {
    return "simple";
  }
}

export function writeWorkspaceMode(storage: WorkspaceModeStorage | null | undefined, mode: WorkspaceMode): boolean {
  try {
    storage?.setItem(WORKSPACE_MODE_STORAGE_KEY, mode);
    return Boolean(storage);
  } catch {
    return false;
  }
}

export function readWorkspaceModeFromWindow(target: { readonly localStorage: WorkspaceModeStorage }): WorkspaceMode {
  try {
    return readWorkspaceMode(target.localStorage);
  } catch {
    return "simple";
  }
}

export function writeWorkspaceModeToWindow(
  target: { readonly localStorage: WorkspaceModeStorage },
  mode: WorkspaceMode,
): boolean {
  try {
    return writeWorkspaceMode(target.localStorage, mode);
  } catch {
    return false;
  }
}

export function createExclusiveLatestAction() {
  let generation = 0;
  let running = false;
  return {
    begin(): number | null {
      if (running) return null;
      running = true;
      generation += 1;
      return generation;
    },
    isCurrent(token: number): boolean {
      return running && token === generation;
    },
    finish(token: number): boolean {
      if (token !== generation) return false;
      running = false;
      return true;
    },
    invalidate(): void {
      generation += 1;
      running = false;
    },
  };
}

export function retainedNativeTextWarning(item: Pick<OcrReviewItem, "status" | "reason" | "native_text">): string | null {
  if (item.status !== "rejected" || !item.native_text.trim()) return null;
  if (!["suspicious_glyph", "font_encoding_anomaly"].includes(item.reason)) return null;
  return "Ohranjen je sumljiv izvorni zapis. Prevod je lahko napačen; OCR pregled lahko pozneje znova odprete.";
}

export function hasRetainedNativeTextWarnings(items: OcrReviewItem[]): boolean {
  return items.some((item) => retainedNativeTextWarning(item) !== null);
}

export type GuidedStep =
  | "select_pdf"
  | "analyze_document"
  | "review_text_issues"
  | "prepare_translation"
  | "translate"
  | "review_translations"
  | "prepare_pdf"
  | "download_pdf"
  | "complete";

export type RequiredLocalResourceStatus = "loading" | "ready" | "error" | "explicitly_skipped";

export type GuidedWorkflowState = {
  step: GuidedStep;
  label: string;
  explanation: string;
  blocked: boolean;
};

export function determineGuidedStep(input: {
  hasFile: boolean;
  analysisReady: boolean;
  missingTextPages: number;
  ocrItems: OcrReviewItem[];
  glossaryStatus: RequiredLocalResourceStatus;
  translationMemoryStatus: RequiredLocalResourceStatus;
  translatorAvailable: boolean;
  translationRunning: boolean;
  job: TranslationJob | null;
  exportPlan: PdfExportPlan | null;
  exportDownloaded: boolean;
}): GuidedWorkflowState {
  if (!input.hasFile) return state("select_pdf", "Izberi PDF", "Izberite dokument, ki ga želite prevesti.");
  if (!input.analysisReady) return state("analyze_document", "Analiziraj dokument", "Najprej varno analizirajte vse strani dokumenta.");

  const unresolvedOcr = countUnresolvedOcr(input.ocrItems);
  if (unresolvedOcr > 0 || input.missingTextPages > 0) {
    const gaps = [
      unresolvedOcr > 0 ? `${unresolvedOcr} nerešenih OCR območij` : "",
      input.missingTextPages > 0 ? `${input.missingTextPages} strani brez pridobljenega besedila` : "",
    ].filter(Boolean).join(" in ");
    return state(
      "review_text_issues",
      "Preglej težave pri pridobivanju besedila",
      `${gaps}. Brez pregleda dokument ne bo prikazan kot popolnoma obdelan.`,
    );
  }

  const resources = [input.glossaryStatus, input.translationMemoryStatus];
  if (resources.includes("loading")) {
    return state("prepare_translation", "Pripravi in prevedi", "Nalaganje trajnega glosarja oziroma prevajalskega spomina …", true);
  }
  if (resources.includes("error")) {
    return state("prepare_translation", "Pripravi in prevedi", "Lokalni vir ni dosegljiv. Poskusite znova ali izrecno nadaljujte brez njega.", true);
  }
  if (!input.job) return state("prepare_translation", "Pripravi in prevedi", "Uporabljen bo determinističen posnetek dokumentnega in trajnega glosarja ter prevajalskega spomina.");

  const units = input.job.segments.filter(isTranslationUnitSegment).filter((segment) => !segment.skip_reason);
  const waiting = units.filter((segment) => ["pending", "failed", "translating"].includes(segment.status));
  if (waiting.length > 0) {
    if (input.translationRunning) return state("translate", "Prevajanje poteka …", "Lokalni prevajalnik obdeluje čakajoče segmente.", true);
    if (!input.translatorAvailable) return state("translate", "Prevedi čakajoče", "Lokalni prevajalnik ni pripravljen. Preverite lokalni model.", true);
    return state("translate", "Prevedi čakajoče", `${waiting.length} segmentov čaka na lokalni prevod.`);
  }

  const translated = units.filter((segment) => ["translated", "reviewed"].includes(segment.status) && segment.edited_translation.trim());
  const reviewed = translated.filter((segment) => segment.status === "reviewed");
  if (reviewed.length < translated.length) {
    return state("review_translations", "Preglej prevode", `${translated.length - reviewed.length} prevedenih segmentov še ni ročno potrjenih.`);
  }

  if (!input.exportPlan) return state("prepare_pdf", "Pripravi PDF", "Preverjeni bodo prileganje, geometrija in varnost vsake zamenjave.");
  if (!input.exportDownloaded) return state("download_pdf", "Prenesi prevedeni PDF", "Prenos lahko ostane delen; vsi preskoki ostanejo jasno navedeni.");
  return state("complete", "Prevod je pripravljen", "PDF je bil ustvarjen za vsa trenutno varno upravičena območja.", true);
}

export function countUnresolvedOcr(items: OcrReviewItem[]): number {
  return items.filter(isUnresolvedOcr).length;
}

function isUnresolvedOcr(item: OcrReviewItem): boolean {
  if (item.status === "accepted") return !item.server_revision_id?.trim();
  if (item.status === "rejected" && item.native_text.trim()) return false;
  return item.status !== "rejected" || !item.native_text.trim();
}

function state(step: GuidedStep, label: string, explanation: string, blocked = false): GuidedWorkflowState {
  return { step, label, explanation, blocked };
}
