"use client";

import { useMemo, useState } from "react";

import {
  confirmTerminologyImport,
  previewTerminologyImport,
  readCsvHeaders,
  type CsvColumnMapping,
  type TerminologyImportPreview,
} from "../lib/terminology-import";

const EMPTY_MAPPING: CsvColumnMapping = { source_term: "", target_term: "" };

export function TerminologyImportPanel({ onImported }: { onImported: () => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<"csv" | "tbx">("csv");
  const [headers, setHeaders] = useState<string[]>([]);
  const [mapping, setMapping] = useState<CsvColumnMapping>(EMPTY_MAPPING);
  const [sourceLanguage, setSourceLanguage] = useState("en");
  const [targetLanguage, setTargetLanguage] = useState("sl");
  const [domain, setDomain] = useState("");
  const [externalSource, setExternalSource] = useState("");
  const [sourceVersion, setSourceVersion] = useState("");
  const [licenseInfo, setLicenseInfo] = useState("");
  const [preview, setPreview] = useState<TerminologyImportPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [classification, setClassification] = useState("");
  const [domainFilter, setDomainFilter] = useState("");
  const [languageFilter, setLanguageFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [query, setQuery] = useState("");
  const [message, setMessage] = useState("Datoteka se obdela lokalno in se po predogledu izbriše.");
  const [busy, setBusy] = useState(false);

  const visible = useMemo(() => (preview?.candidates ?? []).filter((candidate) =>
    (!classification || candidate.classification === classification) &&
    (!domainFilter || candidate.domain === domainFilter) &&
    (!languageFilter || `${candidate.source_language}-${candidate.target_language}` === languageFilter) &&
    (!sourceFilter || candidate.external_source === sourceFilter) &&
    (!query || `${candidate.source_term} ${candidate.target_term} ${candidate.external_record_id ?? ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())),
  ), [classification, domainFilter, languageFilter, preview, query, sourceFilter]);

  async function chooseFile(nextFile: File | null) {
    setFile(nextFile); setPreview(null); setSelected(new Set());
    if (!nextFile) { setHeaders([]); return; }
    const nextFormat = nextFile.name.toLocaleLowerCase().endsWith(".tbx") ? "tbx" : "csv";
    setFormat(nextFormat);
    if (nextFormat === "csv") {
      try {
        const nextHeaders = await readCsvHeaders(nextFile);
        setHeaders(nextHeaders);
        setMapping({ source_term: nextHeaders[0] ?? "", target_term: nextHeaders[1] ?? "" });
      } catch (error) { setMessage(error instanceof Error ? error.message : "Glave CSV ni bilo mogoče prebrati."); }
    } else setHeaders([]);
  }

  async function runPreview() {
    if (!file) return;
    setBusy(true);
    try {
      const result = await previewTerminologyImport(file, format, {
        source_language: sourceLanguage,
        target_language: targetLanguage,
        default_domain: domain,
        default_match_mode: "preferred_term",
        external_source: externalSource,
        source_version: sourceVersion || undefined,
        license_info: licenseInfo || undefined,
        csv_mapping: format === "csv" ? mapping : undefined,
      });
      setPreview(result);
      setSelected(new Set(result.candidates.filter((item) => item.classification === "new").map((item) => item.candidate_id)));
      setMessage("Predogled je pripravljen. V glosar še ni bilo nič zapisano.");
    } catch (error) { setPreview(null); setMessage(error instanceof Error ? error.message : "Predogled ni uspel."); }
    finally { setBusy(false); }
  }

  async function commit() {
    if (!preview || selected.size === 0) return;
    setBusy(true);
    try {
      const result = await confirmTerminologyImport(preview, [...selected]);
      setMessage(`Uvoženih je ${result.created} predlaganih kandidatov; preskočeno: ${result.skipped_duplicates} novih dvojnikov in ${result.skipped_conflicts} novih konfliktov.`);
      setPreview(null); setSelected(new Set()); setFile(null); setHeaders([]);
      await onImported();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Uvoz ni uspel."); }
    finally { setBusy(false); }
  }

  function toggle(candidateId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(candidateId)) next.delete(candidateId); else next.add(candidateId);
      return next;
    });
  }

  return <div className="terminology-import-panel">
    <header><h4>Zunanji terminološki kandidati</h4><p>TBX ali CSV se obdela samo lokalno. Kandidati se vedno uvozijo kot predlagani in ne vplivajo na prevajanje do ločene odobritve.</p></header>
    <div className="terminology-import-settings">
      <label>Datoteka<input type="file" accept=".csv,.tbx,text/csv,application/xml,text/xml" onChange={(event) => void chooseFile(event.target.files?.[0] ?? null)} /></label>
      <label>Oblika<select value={format} onChange={(event) => setFormat(event.target.value as "csv" | "tbx")}><option value="csv">CSV</option><option value="tbx">TBX Basic</option></select></label>
      <label>Izvorni jezik<input value={sourceLanguage} onChange={(event) => setSourceLanguage(event.target.value)} /></label>
      <label>Ciljni jezik<input value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)} /></label>
      <label>Privzeta domena<input value={domain} onChange={(event) => setDomain(event.target.value)} /></label>
      <label>Zunanji vir<input required value={externalSource} onChange={(event) => setExternalSource(event.target.value)} placeholder="npr. uporabniški IATE izvoz" /></label>
      <label>Datum ali različica vira<input value={sourceVersion} onChange={(event) => setSourceVersion(event.target.value)} /></label>
      <label>Licenca ali atribucija<input value={licenseInfo} onChange={(event) => setLicenseInfo(event.target.value)} /></label>
    </div>
    {format === "csv" && headers.length > 0 && <div className="csv-mapping">
      <ColumnSelect label="Izvorni termin" required headers={headers} value={mapping.source_term} onChange={(value) => setMapping({ ...mapping, source_term: value })} />
      <ColumnSelect label="Ciljni termin" required headers={headers} value={mapping.target_term} onChange={(value) => setMapping({ ...mapping, target_term: value })} />
      <ColumnSelect label="Domena" headers={headers} value={mapping.domain ?? ""} onChange={(value) => setMapping({ ...mapping, domain: value || undefined })} />
      <ColumnSelect label="Izvorni jezik" headers={headers} value={mapping.source_language ?? ""} onChange={(value) => setMapping({ ...mapping, source_language: value || undefined })} />
      <ColumnSelect label="Ciljni jezik" headers={headers} value={mapping.target_language ?? ""} onChange={(value) => setMapping({ ...mapping, target_language: value || undefined })} />
      <ColumnSelect label="Način ujemanja" headers={headers} value={mapping.match_mode ?? ""} onChange={(value) => setMapping({ ...mapping, match_mode: value || undefined })} />
      <ColumnSelect label="ID zapisa" headers={headers} value={mapping.external_record_id ?? ""} onChange={(value) => setMapping({ ...mapping, external_record_id: value || undefined })} />
      <ColumnSelect label="Zanesljivost vira" headers={headers} value={mapping.confidence ?? ""} onChange={(value) => setMapping({ ...mapping, confidence: value || undefined })} />
      <ColumnSelect label="Definicija" headers={headers} value={mapping.definition ?? ""} onChange={(value) => setMapping({ ...mapping, definition: value || undefined })} />
      <ColumnSelect label="Kontekst" headers={headers} value={mapping.context ?? ""} onChange={(value) => setMapping({ ...mapping, context: value || undefined })} />
      <ColumnSelect label="Opomba" headers={headers} value={mapping.note ?? ""} onChange={(value) => setMapping({ ...mapping, note: value || undefined })} />
      <ColumnSelect label="Različica vira" headers={headers} value={mapping.source_version ?? ""} onChange={(value) => setMapping({ ...mapping, source_version: value || undefined })} />
    </div>}
    <div className="compact-actions"><button className="button" disabled={busy || !file || !externalSource.trim() || (format === "csv" && (!mapping.source_term || !mapping.target_term))} onClick={() => void runPreview()}>{busy ? "Preverjam …" : "Pripravi predogled"}</button></div>
    <p className="translation-provider-message">{message}</p>
    {preview && <>
      <dl className="import-summary">
        <div><dt>Veljavni</dt><dd>{preview.valid_count}</dd></div><div><dt>Novi</dt><dd>{preview.new_count}</dd></div>
        <div><dt>Dvojniki</dt><dd>{preview.duplicate_count}</dd></div><div><dt>Konflikti</dt><dd>{preview.conflict_count}</dd></div>
        <div><dt>Neveljavni</dt><dd>{preview.invalid_count}</dd></div><div><dt>Manjkajoči jeziki/prevodi</dt><dd>{preview.missing_language_count + preview.missing_translation_count}</dd></div>
      </dl>
      <p><strong>SHA-256:</strong> <code>{preview.file_sha256}</code><br /><strong>Vir:</strong> {preview.external_source}{preview.license_info ? ` · ${preview.license_info}` : ""}</p>
      {preview.ignored_fields.length > 0 && <p>Prezrta polja: {preview.ignored_fields.join(", ")}</p>}
      <div className="glossary-toolbar">
        <input aria-label="Išči uvozne kandidate" placeholder="Išči kandidat ali ID" value={query} onChange={(event) => setQuery(event.target.value)} />
        <select aria-label="Filter konflikta" value={classification} onChange={(event) => setClassification(event.target.value)}><option value="">Vsi rezultati</option><option value="new">Novi</option><option value="conflict">Konflikti</option><option value="duplicate">Dvojniki</option></select>
        <select aria-label="Filter domene kandidata" value={domainFilter} onChange={(event) => setDomainFilter(event.target.value)}><option value="">Vse domene</option>{preview.domains.map((item) => <option key={item}>{item}</option>)}</select>
        <select aria-label="Filter jezikov kandidata" value={languageFilter} onChange={(event) => setLanguageFilter(event.target.value)}><option value="">Vsi jezikovni pari</option>{[...new Set(preview.candidates.map((item) => `${item.source_language}-${item.target_language}`))].sort().map((item) => <option key={item} value={item}>{item.toLocaleUpperCase()}</option>)}</select>
        <select aria-label="Filter vira kandidata" value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}><option value="">Vsi viri</option><option value={preview.external_source}>{preview.external_source}</option></select>
      </div>
      <div className="glossary-table-wrap"><table className="glossary-table"><thead><tr><th>Izberi</th><th>Izvor</th><th>Prevod</th><th>Domena</th><th>Stanje</th><th>Vir</th></tr></thead><tbody>
        {visible.map((candidate) => <tr key={candidate.candidate_id}>
          <td><input aria-label={`Izberi ${candidate.source_term}`} type="checkbox" disabled={candidate.classification === "duplicate" || candidate.classification === "invalid"} checked={selected.has(candidate.candidate_id)} onChange={() => toggle(candidate.candidate_id)} /></td>
          <td>{candidate.source_term}<CandidateDetails candidate={candidate} /></td><td>{candidate.target_term}</td><td>{candidate.domain || "splošno"}</td><td>{classificationLabel(candidate.classification)}</td><td>{candidate.external_source}{candidate.external_record_id ? ` · ${candidate.external_record_id}` : ""}</td>
        </tr>)}
      </tbody></table></div>
      {preview.issues.map((issue, index) => <p className="translation-warning" key={`${issue.row_number}-${issue.kind}-${index}`}>Zapis {issue.row_number}: {issue.message}</p>)}
      <div className="compact-actions"><button className="button" disabled={busy || selected.size === 0} onClick={() => void commit()}>Uvozi {selected.size} izbranih kot predlagane</button></div>
    </>}
  </div>;
}

function ColumnSelect({ label, headers, value, required = false, onChange }: { label: string; headers: string[]; value: string; required?: boolean; onChange: (value: string) => void }) {
  return <label>{label}<select required={required} value={value} onChange={(event) => onChange(event.target.value)}><option value="">Ni preslikano</option>{headers.map((header) => <option key={header}>{header}</option>)}</select></label>;
}

function CandidateDetails({ candidate }: { candidate: TerminologyImportPreview["candidates"][number] }) {
  if (!candidate.definition && !candidate.context && !candidate.source_confidence) return null;
  return <details className="candidate-details"><summary>Podrobnosti</summary>{candidate.definition && <p><strong>Definicija:</strong> {candidate.definition}</p>}{candidate.context && <p><strong>Kontekst:</strong> {candidate.context}</p>}{candidate.source_confidence && <p><strong>Zanesljivost vira:</strong> {candidate.source_confidence}</p>}</details>;
}

function classificationLabel(value: TerminologyImportPreview["candidates"][number]["classification"]) {
  return { new: "Nov", duplicate: "Dvojnik", conflict: "Konflikt", invalid: "Neveljaven" }[value];
}
