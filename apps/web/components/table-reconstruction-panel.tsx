"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { TranslationJob } from "../lib/translation-types";
import type { PdfTypographyProfile, PdfExportPlan } from "../lib/pdf-export-client";
import { prepareReconstruction, downloadReconstruction, type ReconstructionPreview, type ReconstructionSelection } from "../lib/table-reconstruction";
import { renderPdf } from "../lib/pdf-client";
import { createPdfPreviewGate, type PdfPreviewState } from "../lib/pdf-preview";

export function TableReconstructionPanel({ file, job, profile, selection, revision, onStateChange, onPreviewChange }: {
  file: File; job: TranslationJob; profile: PdfTypographyProfile; selection: ReconstructionSelection; revision: string;
  onStateChange: (state: { revisionKey: string; value: { plan: PdfExportPlan | null; downloadCompleted: boolean; appliedReplacements: number } }) => void;
  onPreviewChange: (state: PdfPreviewState | null) => void;
}) {
  const [preview, setPreview] = useState<ReconstructionPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloaded, setDownloaded] = useState(false);
  const live = useRef(revision);
  const running = useRef(false);
  const gate = useRef(createPdfPreviewGate());
  const artifact = useRef<{ filename: string; blob: Blob } | null>(null);
  useLayoutEffect(() => {
    const currentGate = gate.current;
    live.current = revision;
    currentGate.invalidate();
    artifact.current = null;
    onPreviewChange({ status: "stale", refresh: () => void run(false) });
    return () => { live.current = ""; currentGate.invalidate(); onPreviewChange(null); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision, onPreviewChange]);
  useEffect(() => {
    const timer = setTimeout(() => { void run(false); }, 600);
    return () => clearTimeout(timer);
    // A new instance is keyed by the complete export revision.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision]);
  useEffect(() => {
    onStateChange({ revisionKey: revision, value: { plan: preview?.ordinary_plan ?? null,
      downloadCompleted: downloaded, appliedReplacements: downloaded ? preview?.counts?.translated_units ?? 0 : 0 } });
  }, [downloaded, onStateChange, preview, revision]);

  async function run(download: boolean) {
    if (running.current) return;
    running.current = true;
    setBusy(true); setError(null); setDownloaded(false);
    const current = revision;
    const ticket = gate.current.begin();
    try {
      if (download && preview?.ready) {
        const result = artifact.current;
        if (!result) throw new Error("Najprej posodobite predogled rekonstrukcije.");
        if (live.current !== current) return;
        const url = URL.createObjectURL(result.blob);
        const anchor = document.createElement("a");
        anchor.href = url; anchor.download = result.filename; anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        setDownloaded(true);
      } else {
        artifact.current = null;
        onPreviewChange({ status: "updating" });
        setPreview(null);
        const value = await prepareReconstruction(file, job, profile, selection);
        if (live.current !== current || !gate.current.isCurrent(ticket)) return;
        setPreview(value);
        if (!value.ready) {
          onPreviewChange({ status: "blocked", message: value.issues.join(" "), refresh: () => void run(false) });
          return;
        }
        const result = await downloadReconstruction(file, job, profile, selection, value);
        const rendered = await renderPdf(new File([result.blob], result.filename, { type: "application/pdf" }));
        if (live.current !== current || !gate.current.isCurrent(ticket)) return;
        artifact.current = result;
        onPreviewChange({ status: "ready", pages: rendered.pages, refresh: () => void run(false) });
      }
    } catch (caught) {
      if (live.current === current && gate.current.isCurrent(ticket)) {
        onPreviewChange({ status: "blocked", message: caught instanceof Error ? caught.message : "Rekonstrukcija ni uspela.", refresh: () => void run(false) });
        setError(caught instanceof Error ? caught.message : "Rekonstrukcija ni uspela.");
        // A download consumes its token even on failure. No stale success or retry with it.
        if (download) setPreview(null);
      }
    } finally { running.current = false; setBusy(false); }
  }

  const counts = preview?.counts;
  return <section aria-label="Rekonstrukcija izbrane tabele" className="table-reconstruction">
    <p className="translation-warning">Rekonstrukcija zamenja oblikovanje in grafiko samo izbrane tabele. Izbira ne potrdi prevodov. Manjkajoči ali nepotrjeni prevodi preprečijo rekonstrukcijo.</p>
    <button className="button secondary" disabled={busy} onClick={() => void run(false)}>{busy ? "Preverjam tabelo …" : "Pripravi predogled rekonstrukcije"}</button>
    {error && <p role="alert" className="translation-error">{error}</p>}
    {preview && <>
      <p>{preview.row_count} vrstic · {preview.cell_count} besedilnih celic · {preview.ready ? "prevodi in zaščiteni podatki preverjeni" : "rekonstrukcija blokirana"}</p>
      <div className="reconstruction-previews">
        <figure><figcaption>Izvorni izsek</figcaption>{/* Local server-rendered bounded crops, not external images. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={preview.source_preview} alt="Izvorna izbrana tabela" /></figure>
        {preview.table_preview && <figure><figcaption>Predogled nove tabele</figcaption>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={preview.table_preview} alt="Preverjeni izris rekonstruirane tabele" /></figure>}
      </div>
      {preview.issues.map(issue => <p key={issue} role="alert">{issue}</p>)}
      {counts && <dl className="reconstruction-counts">
        <dt>Običajne prevodne zamenjave zunaj tabele</dt><dd>{counts.ordinary_replacements}</dd>
        <dt>Rekonstruirane celice</dt><dd>{counts.reconstructed_cells} ({counts.reconstructed_translations} prevodov, {counts.reconstructed_identical} identičnih, {counts.reconstructed_technical} tehničnih)</dd>
        <dt>Prevedene enote skupaj, brez dvojnega štetja</dt><dd>{counts.translated_units}</dd>
        <dt>Identične / tehnično ohranjene enote skupaj</dt><dd>{counts.preserved_identical} / {counts.not_applicable}</dd>
        <dt>Preostale blokade</dt><dd>{counts.blocked}</dd>
        <dt>Skupaj razvrščene enote</dt><dd>{counts.total_units}</dd>
      </dl>}
      {preview.ordinary_plan?.typography_coverage && <p>Samo oblikovani izvori zunaj tabele: {preview.ordinary_plan.typography_coverage.source_formatted}. To niso dodatni prevodi.</p>}
      {counts && counts.blocked > 0 && <details><summary>Razlogi preostalih blokad</summary>
        {[...(preview.ordinary_plan?.replacements ?? []), ...(preview.ordinary_plan?.ocr_replacements ?? [])]
          .filter(item => item.resolution.startsWith("blocked_")).map(item => <div key={item.segment_id}>
            <a href={`#translation-${item.segment_id}`}>{item.segment_id}</a>: {item.skip_reason}
            {item.warnings.map(warning => <p key={warning}>{warning}</p>)}
          </div>)}
      </details>}
      {preview.ready && <button className="button" disabled={busy || downloaded || !artifact.current} onClick={() => void run(true)}>{downloaded ? "Delni PDF je prenesen" : "Prenesi prevedeni PDF z rekonstruirano tabelo"}</button>}
      {downloaded && <p role="status">Preverjeni delni PDF je prenesen. Števci veljajo za izvedeni izvoz.</p>}
    </>}
    <p>To je delni dokument. Grafična glava, slikovni napisi in pravna noga niso samodejno prevedeni. Stari rastrski podatki lahko ostanejo v ohranjenih slikovnih resursih; to ni varnostna redakcija.</p>
  </section>;
}
