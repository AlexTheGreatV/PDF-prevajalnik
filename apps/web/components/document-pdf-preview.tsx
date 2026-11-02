"use client";

import { useState } from "react";
import type { RenderResult } from "../lib/pdf-types";
import type { PdfPreviewState } from "../lib/pdf-preview";

export function DocumentPdfPreview({ original, current }: { original: RenderResult | null; current: PdfPreviewState | null }) {
  const [view, setView] = useState<"current" | "original">("current");
  const pages = view === "original" ? original?.pages : current?.status === "ready" ? current.pages : null;
  return <aside className="document-side-preview" aria-label="Predogled PDF dokumenta">
    <h2>Predogled PDF</h2>
    <div className="segmented" aria-label="Pogled PDF">
      <button aria-pressed={view === "current"} onClick={() => setView("current")}>Trenutni izvoz</button>
      <button aria-pressed={view === "original"} onClick={() => setView("original")}>Izvirnik</button>
    </div>
    {view === "original" ? <p>Izvirni PDF, brez prevodnih zamenjav.</p> : <>
      <p role="status">{current?.status === "updating" ? "Posodabljanje predogleda" : current?.status === "ready"
        ? "Aktualni delni PDF. Prenos uporabi isto datoteko." : current?.status === "blocked"
          ? "Predogled je blokiran." : "Predogled je zastarel oziroma še ni pripravljen."}</p>
      {current?.message && <p className="translation-warning">{current.message}</p>}
      {current?.refresh && <button className="button secondary" disabled={current.status === "updating"} onClick={current.refresh}>Posodobi predogled</button>}
      {!current && <p>Pripravite opravilo in odprite končni PDF izvoz.</p>}
    </>}
    {pages?.map(page => <figure id={`pdf-preview-page-${page.page_number}`} key={page.page_number}>
      <figcaption>{view === "original" ? "Izvirnik" : "Trenutni izvoz"} · Stran {page.page_number}</figcaption>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={page.path} alt={`${view === "original" ? "Izvirnik" : "Trenutni izvoz"} strani ${page.page_number}`} width={page.width_px ?? 850} height={page.height_px ?? 1100} />
    </figure>)}
  </aside>;
}
