"use client";

import { useState } from "react";
import type { TranslationJob } from "../lib/translation-types";
import type { TerminologySourceRecord, TranslationChecklistStatus, TranslationDiagnosticKind } from "../lib/translation-profile";

const DIAGNOSTIC_LABELS = {
  blocking_error: "Blokirna napaka",
  warning: "Opozorilo",
  external_source_unchecked: "Zunanji vir ni preverjen",
  informational: "Informacija",
} as const;

const CHECK_LABELS = {
  passed: "Preverjeno",
  failed: "Neuspešno",
  needs_review: "Potreben pregled",
  not_checkable: "Ni samodejno preverljivo",
  not_applicable: "Ni relevantno",
} as const;

export function TranslationProfileSummary({
  job,
  onTerminologySourcesChange,
}: {
  job: TranslationJob;
  onTerminologySourcesChange: (sources: TerminologySourceRecord[]) => void;
}) {
  const [diagnosticKind, setDiagnosticKind] = useState<"all" | TranslationDiagnosticKind>("all");
  const [diagnosticSegment, setDiagnosticSegment] = useState("all");
  const [ruleStatus, setRuleStatus] = useState<"all" | TranslationChecklistStatus>("all");
  const profile = job.translation_profile;
  if (!profile) return <section className="translation-profile-summary legacy">
    <h3>Prevajalski profil</h3>
    <p>Uvoženo opravilo nima zapisa o uporabljenem prevajalskem profilu. Pravila novega profila mu niso pripisana.</p>
  </section>;

  const diagnostics = profile.diagnostics.filter((item) =>
    (diagnosticKind === "all" || item.kind === diagnosticKind) &&
    (diagnosticSegment === "all" || (diagnosticSegment === "document" ? item.segment_id === null : item.segment_id === diagnosticSegment)),
  );
  const diagnosticSegments = [...new Set(profile.diagnostics.flatMap((item) => item.segment_id ? [item.segment_id] : []))];
  const statusCounts = Object.fromEntries(Object.keys(CHECK_LABELS).map((status) => [status, profile.rule_evaluations.filter((item) => item.status === status).length])) as Record<TranslationChecklistStatus, number>;
  const visibleRules = profile.rule_evaluations.filter((item) => ruleStatus === "all" || item.status === ruleStatus);

  return <section className="translation-profile-summary" aria-labelledby="translation-profile-title">
    <div className="translation-subhead">
      <div>
        <h3 id="translation-profile-title">Prevajalski profil</h3>
        <p>{profile.profile_id} · različica {profile.profile_version}</p>
      </div>
      <span className="profile-rule-count">Uporabljena samodejna pravila: {profile.applied_rule_ids.length}</span>
    </div>

    {profile.evaluation_status !== "current" ? <p className="profile-diagnostic warning">
      <strong>Preverjanje profila ni aktualno</strong>
      <span>{profile.evaluation_status === "stale" ? "Hash profila se ne ujema; stari rezultati preverjanja so razveljavljeni." : "Opravilo uporablja neznan profil; njegovi podatki so ohranjeni brez ponovne razlage."}</span>
    </p> : null}

    <div className="profile-status-summary" aria-label="Runtime rezultati pravil">
      {(Object.keys(CHECK_LABELS) as TranslationChecklistStatus[]).map((status) => <span key={status} className={`rule-status ${status}`}>
        {CHECK_LABELS[status]}: <strong>{statusCounts[status]}</strong>
      </span>)}
    </div>

    <div className="profile-filters" aria-label="Filtri opozoril">
      <label>Pomembnost
        <select value={diagnosticKind} onChange={(event) => setDiagnosticKind(event.target.value as typeof diagnosticKind)}>
          <option value="all">Vse</option>
          {Object.entries(DIAGNOSTIC_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>Segment
        <select value={diagnosticSegment} onChange={(event) => setDiagnosticSegment(event.target.value)}>
          <option value="all">Vsi</option><option value="document">Dokumentna opozorila</option>
          {diagnosticSegments.map((segmentId) => <option key={segmentId} value={segmentId}>{segmentId}</option>)}
        </select>
      </label>
    </div>

    <div className="profile-diagnostics">
      {diagnostics.map((diagnostic, index) => <p key={`${diagnostic.kind}-${diagnostic.rule_id ?? index}-${diagnostic.segment_id ?? "document"}`} className={`profile-diagnostic ${diagnostic.kind}`}>
        <strong>{DIAGNOSTIC_LABELS[diagnostic.kind]}</strong>
        <span>{diagnostic.message}{diagnostic.rule_id ? ` (${diagnostic.rule_id})` : ""}</span>
        {diagnostic.segment_id ? <small>Segment: {diagnostic.segment_id}</small> : <small>Raven dokumenta</small>}
        {diagnostic.source_text ? <small>Izvor: {diagnostic.source_text}</small> : null}
        {diagnostic.required_source_type ? <small>Zahtevani vir: {diagnostic.required_source_type} · stanje: {diagnostic.source_status}</small> : null}
      </p>)}
      {diagnostics.length === 0 ? <p className="profile-empty">Za izbrana filtra ni opozoril.</p> : null}
      {profile.conflicts.map((conflict) => <p key={`${conflict.higher_priority}-${conflict.lower_priority}`} className="profile-diagnostic informational">
        <strong>Razrešen konflikt pravil</strong><span>{conflict.reason}</span>
      </p>)}
    </div>

    <details className="translation-rule-results">
      <summary>Uporabnost in rezultat vseh pravil</summary>
      <label>Rezultat
        <select value={ruleStatus} onChange={(event) => setRuleStatus(event.target.value as typeof ruleStatus)}>
          <option value="all">Vsi rezultati</option>
          {Object.entries(CHECK_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <div className="rule-result-list">
        {visibleRules.map((result) => <p key={result.rule_id} className={`rule-result ${result.status}`}>
          <strong>{result.rule_id}</strong>
          <span>{CHECK_LABELS[result.status]} · {result.applicability === "applicable" ? "uporabno" : result.applicability === "not_applicable" ? "ni relevantno" : "uporabnost ni določljiva"} · {result.scope === "document" ? "dokument" : `${result.segment_ids.length} segmentov`}</span>
          <small>{result.message}</small>
        </p>)}
      </div>
    </details>

    <details className="translation-quality-checklist">
      <summary>Končni nadzor kakovosti</summary>
      <div>
        {job.quality_checklist.map((check) => <article key={check.check_id} className={`quality-check ${check.status}`}>
          <span>{CHECK_LABELS[check.status]}</span>
          <strong>{check.label}</strong>
          <p>{check.message}</p>
        </article>)}
      </div>
    </details>

    <details className="terminology-sources">
      <summary>Terminološki viri</summary>
      <p>Viri se ne preverjajo samodejno. Potrditev pomeni le uporabnikovo potrditev zapisa.</p>
      {profile.terminology_sources.map((source, index) => <div className="terminology-source-row" key={`term-source-${index}`}>
        <input aria-label="Izbrani termin" placeholder="Izbrani termin" value={source.selected_term} onChange={(event) => updateTerminologySource(profile.terminology_sources, index, { selected_term: event.target.value }, onTerminologySourcesChange)} />
        <input aria-label="Terminološko področje" placeholder="Področje" value={source.domain} onChange={(event) => updateTerminologySource(profile.terminology_sources, index, { domain: event.target.value }, onTerminologySourcesChange)} />
        <input aria-label="Vir ali citat" placeholder="Vir ali citat" value={source.source_or_citation ?? ""} onChange={(event) => updateTerminologySource(profile.terminology_sources, index, { source_or_citation: event.target.value || null }, onTerminologySourcesChange)} />
        <select aria-label="Zanesljivost vira" value={source.confidence ?? ""} onChange={(event) => updateTerminologySource(profile.terminology_sources, index, { confidence: event.target.value ? event.target.value as TerminologySourceRecord["confidence"] : null }, onTerminologySourcesChange)}>
          <option value="">Ni določena</option><option value="low">Nizka</option><option value="medium">Srednja</option><option value="high">Visoka</option>
        </select>
        <label><input type="checkbox" checked={source.user_confirmed} onChange={(event) => updateTerminologySource(profile.terminology_sources, index, { user_confirmed: event.target.checked }, onTerminologySourcesChange)} /> Uporabniško potrjeno</label>
        <button className="button secondary" onClick={() => onTerminologySourcesChange(profile.terminology_sources.filter((_, itemIndex) => itemIndex !== index))}>Odstrani</button>
      </div>)}
      <button className="button secondary" onClick={() => onTerminologySourcesChange([...profile.terminology_sources, emptyTerminologySource()])}>Dodaj vir</button>
    </details>

    <details className="translation-profile-details">
      <summary>Sledljivost pravil</summary>
      <dl>
        <div><dt>Vir</dt><dd>{profile.source_sha256}</dd></div>
        <div><dt>Kanonični profil</dt><dd>{profile.canonical_sha256}</dd></div>
        <div><dt>Konkreten ročni pregled</dt><dd>{profile.manual_review_rule_ids.length} pravil</dd></div>
        <div><dt>Uporabnost ali rezultat ni preverljiv</dt><dd>{profile.unverifiable_rule_ids.length} pravil</dd></div>
      </dl>
    </details>
  </section>;
}

function emptyTerminologySource(): TerminologySourceRecord {
  return { selected_term: "", domain: "", source_or_citation: null, confidence: null, user_confirmed: false };
}

function updateTerminologySource(
  sources: TerminologySourceRecord[],
  index: number,
  update: Partial<TerminologySourceRecord>,
  onChange: (sources: TerminologySourceRecord[]) => void,
) {
  onChange(sources.map((source, itemIndex) => itemIndex === index ? { ...source, ...update } : source));
}
