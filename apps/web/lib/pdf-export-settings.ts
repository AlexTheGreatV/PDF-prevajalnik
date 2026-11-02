import type { PdfAnalysis } from "./pdf-types";
import type { PdfTypographyProfile } from "./pdf-export-client";
import type { ReconstructionSelection } from "./table-reconstruction";

export type PdfExportSettings = {
  version: 1;
  typography_profile: PdfTypographyProfile;
  reconstruction: (ReconstructionSelection & { geometry_binding: string }) | null;
};

function geometryBinding(analysis: PdfAnalysis, selection: ReconstructionSelection): string | null {
  const page = analysis.document?.pages.find(page => page.page_number === selection.page_number);
  const matches = page?.regions.flatMap(region => region.blocks).filter(block => block.table_provenance?.table_id === selection.table_id) ?? [];
  if (matches.length !== 1) return null;
  const block = matches[0];
  return JSON.stringify([analysis.sha256, analysis.document_model_version, page?.page_number,
    block.bbox, block.table_cells, block.table_provenance]);
}

export function createPdfExportSettings(analysis: PdfAnalysis, profile: PdfTypographyProfile, selection: ReconstructionSelection | null): PdfExportSettings {
  const binding = selection ? geometryBinding(analysis, selection) : null;
  if (selection && !binding) throw new Error("Izbrana tabela nima enolične povezave s trenutno analizo.");
  return { version: 1, typography_profile: profile,
    reconstruction: selection && binding ? { page_number: selection.page_number, table_id: selection.table_id, geometry_binding: binding } : null };
}

export function restorePdfExportSettings(analysis: PdfAnalysis, saved: unknown): {
  profile: PdfTypographyProfile; selection: ReconstructionSelection | null; error: string | null;
} {
  const empty = { profile: "original" as const, selection: null, error: null };
  if (saved === undefined) return empty;
  if (!saved || typeof saved !== "object") return { ...empty, error: "Shranjene izvozne nastavitve niso veljavne. Izberite jih znova." };
  const value = saved as PdfExportSettings;
  if (value.version !== 1 || !["original", "document-role-typography-pilot-v1"].includes(value.typography_profile)) {
    return { ...empty, error: "Različica shranjenega izvoznega profila ni podprta. Izberite profil znova." };
  }
  if (value.reconstruction === null) return { ...empty, profile: value.typography_profile };
  const selection = value.reconstruction;
  if (!selection || !Number.isSafeInteger(selection.page_number) || selection.page_number < 1
    || typeof selection.table_id !== "string" || typeof selection.geometry_binding !== "string"
    || geometryBinding(analysis, selection) !== selection.geometry_binding) {
    return { ...empty, profile: value.typography_profile,
      error: "Izbor rekonstrukcije ne ustreza trenutnemu PDF-ju, modelu ali geometriji tabele. Ponovno izberite in preglejte tabelo; prevodi so ohranjeni." };
  }
  return { profile: value.typography_profile, selection, error: null };
}
