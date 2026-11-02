import type { RenderResult } from "./pdf-types";

export type PdfPreviewState = {
  status: "stale" | "updating" | "ready" | "blocked";
  pages?: RenderResult["pages"];
  message?: string;
  refresh?: () => void;
};

// The generation also rejects A -> B -> A races and repeat requests for A.
export function createPdfPreviewGate() {
  let generation = 0;
  return {
    invalidate: () => { generation += 1; },
    begin: () => ++generation,
    isCurrent: (ticket: number) => ticket === generation,
  };
}
