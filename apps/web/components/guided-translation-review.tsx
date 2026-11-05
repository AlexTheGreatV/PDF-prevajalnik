"use client";

import { useEffect, useRef } from "react";

import { sourceAwareQcLabel } from "../lib/ocr-review";
import type { TranslationSegment } from "../lib/translation-types";

export function GuidedTranslationReview({
  segment,
  sourceWarning,
  position,
  total,
  onEdit,
  onReview,
  onNext,
}: {
  segment: TranslationSegment;
  sourceWarning?: string | null;
  position: number;
  total: number;
  onEdit: (text: string) => void;
  onReview: () => void;
  onNext: () => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    textareaRef.current?.scrollIntoView({ block: "nearest" });
  }, [segment.segment_id]);

  return <section className="guided-review" aria-labelledby="guided-review-title">
    <header>
      <div>
        <span>Prevod {position} od {total}</span>
        <h3 id="guided-review-title">Stran {segment.page_number}</h3>
      </div>
      <a className="button secondary" href={`#pdf-preview-page-${segment.page_number}`}>Prikaži stran</a>
    </header>
    <div className="guided-review-copy">
      <div><h4>Izvorno besedilo</h4><p>{segment.source_text}</p></div>
      <label>
        <span>Predlagani prevod</span>
        <textarea ref={textareaRef} value={segment.edited_translation} onChange={(event) => onEdit(event.target.value)} />
      </label>
    </div>
    <p className="translation-origin">Izvor prevoda: {translationOriginLabel(segment)}</p>
    <div className="translation-state-grid" aria-label="Stanje pregleda in kakovosti">
      <span><strong>Uporabniški pregled</strong>{segment.status === "reviewed" ? segment.review_state?.origin === "local_user_action" ? "Uporabniško potrjeno" : "Potrditev neznanega izvora" : "Ni potrjeno"}</span>
      <span><strong>Avtomatski QC</strong>{sourceAwareQcLabel(segment, sourceWarning)}</span>
    </div>
    {segment.needs_review && <p className="translation-warning">Ta prevod zahteva ročni pregled.</p>}
    {sourceWarning && <p className="translation-warning" role="note">{sourceWarning} <a href="#ocr-review">Preglej OCR območje</a></p>}
    {segment.current_qc?.findings.map((finding, index) => <p className={finding.severity === "blocking" ? "translation-error" : "translation-warning"} key={`${finding.code}-${finding.glossary_entry_id ?? index}`}>{finding.message}</p>)}
    {segment.error && <p className="translation-error">{segment.error}</p>}
    <div className="translation-row-actions">
      <button className="button secondary" onClick={() => textareaRef.current?.focus()}>Uredi</button>
      <button className="button" disabled={!segment.edited_translation.trim()} onClick={onReview}>Potrdi</button>
      <button className="button secondary" onClick={onNext}>Naslednji</button>
    </div>
    <details>
      <summary>Podrobnosti</summary>
      <p>ID: {segment.segment_id} · {segment.block_type} · {segment.source_text_origin === "accepted_ocr" ? "potrjeni OCR" : "izvorno digitalno besedilo"}</p>
      {segment.glossary_matches.length > 0 && <p>Glosar: {segment.glossary_matches.map((match) => match.matched_text).join(", ")}</p>}
      {segment.translation_memory_entry_id && <p>Prevajalski spomin: {segment.translation_memory_entry_id} · rev. {segment.translation_memory_revision}</p>}
      {segment.model_id && <p>Model: {segment.model_id}</p>}
      {segment.chunks.length > 1 && <p>Prevajalski deli: {segment.chunks.length}</p>}
      {segment.warnings.length > 0 && <p>Zgodovinska opozorila ponudnika: {segment.warnings.join(", ")}</p>}
    </details>
  </section>;
}


function translationOriginLabel(segment: TranslationSegment): string {
  if (segment.translation_origin === "glossary") return "glosar";
  if (segment.translation_origin === "translation_memory") return "prevajalski spomin";
  if (segment.translation_origin === "provider") return "lokalni HPLT";
  if (segment.translation_origin === "mixed") return "mešano (prevajalski spomin in lokalni HPLT)";
  if (segment.translation_origin === "manual") return "ročno urejeno";
  return "še ni prevedeno";
}
