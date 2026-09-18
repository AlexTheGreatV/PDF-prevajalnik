"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { createDefaultGlossary } from "../lib/default-glossary";
import {
  EMPTY_GLOSSARY_DRAFT,
  confirmGlossaryCsv,
  createLatestGlossaryRequestCoordinator,
  createGlossaryEntry,
  getGlossaryHistory,
  getGlossaryStatus,
  glossaryExportUrl,
  listGlossaryEntries,
  persistentToRuntimeEntry,
  previewGlossaryCsv,
  transitionGlossaryEntry,
  updateGlossaryEntry,
  type GlossaryEntryDraft,
  type GlossaryFilters,
  type GlossaryHistoryRecord,
  type GlossaryImportPreview,
  type PersistentGlossaryEntry,
  type PersistentGlossaryLoadState,
} from "../lib/persistent-glossary";
export type GlossaryProposal = {
  key: string;
  source_term: string;
  target_term: string;
  source_language: string;
  target_language: string;
  domain: string;
};

export function GlossaryManager({ proposal, onProposalConsumed, onApprovedEntriesChange }: {
  proposal: GlossaryProposal | null;
  onProposalConsumed: () => void;
  onApprovedEntriesChange: (state: PersistentGlossaryLoadState) => void;
}) {
  const importRef = useRef<HTMLInputElement | null>(null);
  const loadCoordinator = useRef(createLatestGlossaryRequestCoordinator());
  const [tab, setTab] = useState<"overview" | "import">("overview");
  const [entries, setEntries] = useState<PersistentGlossaryEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState<GlossaryFilters>({});
  const [draft, setDraft] = useState<GlossaryEntryDraft | null>(null);
  const [editing, setEditing] = useState<PersistentGlossaryEntry | null>(null);
  const [history, setHistory] = useState<{ id: string; rows: GlossaryHistoryRecord[] } | null>(null);
  const [preview, setPreview] = useState<GlossaryImportPreview | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [message, setMessage] = useState("Nalagam lokalni glosar …");

  const builtIns = useMemo(createDefaultGlossary, []);
  const visibleBuiltIns = useMemo(() => builtIns.filter((entry) => {
    const query = filters.q?.toLocaleLowerCase().trim();
    return (!query || entry.source_term.toLocaleLowerCase().includes(query) || entry.target_term.toLocaleLowerCase().includes(query)) &&
      (!filters.source_language || filters.source_language === "en") &&
      (!filters.target_language || filters.target_language === "sl") &&
      (!filters.domain || filters.domain === "organizacijski") &&
      (!filters.status || filters.status === "approved") &&
      (!filters.match_mode || filters.match_mode === "exact_segment");
  }), [builtIns, filters]);
  const domains = useMemo(() => [...new Set(entries.map((entry) => entry.domain).filter(Boolean))].sort(), [entries]);
  const conflictIds = useMemo(() => {
    const definitions = [
      ...entries.map((entry) => ({ id: entry.id, source: entry.source_term, target: entry.target_term, mode: entry.match_mode })),
      ...builtIns.map((entry) => ({ id: entry.id, source: entry.source_term, target: entry.target_term, mode: entry.mode })),
    ];
    return new Set(definitions.filter((entry, index) => definitions.some((candidate, candidateIndex) =>
      index !== candidateIndex && candidate.source.toLocaleLowerCase() === entry.source.toLocaleLowerCase() &&
      (candidate.target !== entry.target || candidate.mode !== entry.mode),
    )).map((entry) => entry.id));
  }, [builtIns, entries]);

  useEffect(() => {
    const coordinator = loadCoordinator.current;
    void refresh();
    return () => coordinator.invalidate();
  }, [filters]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!proposal) return;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setTab("overview");
      setEditing(null);
      setDraft({ ...EMPTY_GLOSSARY_DRAFT, ...proposal, origin: "reviewed_correction", status: "proposed" });
    });
    return () => { active = false; };
  }, [proposal]);

  async function refresh() {
    await loadCoordinator.current.run(async () => {
      const status = await getGlossaryStatus();
      if (!status.available) throw new Error(status.message ?? "Trajni lokalni glosar ni na voljo.");
      const [visible, approved] = await Promise.all([
        listGlossaryEntries(filters),
        listGlossaryEntries({ source_language: "en", target_language: "sl", status: "approved" }),
      ]);
      return { visible, approved };
    }, {
      onLoading: () => {
        setAvailable(null);
        setMessage("Nalaganje trajnega glosarja …");
        onApprovedEntriesChange({ status: "loading" });
      },
      onReady: ({ visible, approved }) => {
        setAvailable(true);
        setEntries(visible.entries);
        setTotal(visible.total);
        onApprovedEntriesChange({
          status: "ready",
          entries: approved.entries.map(persistentToRuntimeEntry),
          active_set_hash: approved.active_set_hash,
        });
        setMessage(visible.total ? "" : "Ni lokalnih vnosov za izbrane filtre.");
      },
      onError: (error) => {
        const errorMessage = error instanceof Error ? error.message : "Trajni lokalni glosar ni na voljo.";
        setAvailable(false);
        setMessage(errorMessage);
        onApprovedEntriesChange({ status: "error", message: errorMessage });
      },
    });
  }

  function newEntry() {
    setEditing(null);
    setDraft({ ...EMPTY_GLOSSARY_DRAFT });
  }

  function edit(entry: PersistentGlossaryEntry) {
    setEditing(entry);
    setDraft({
      source_term: entry.source_term, target_term: entry.target_term,
      source_language: entry.source_language, target_language: entry.target_language,
      domain: entry.domain, match_mode: entry.match_mode, case_sensitive: entry.case_sensitive,
      status: entry.status, origin: entry.origin, note: entry.note,
      source_label: entry.source_label, document_hash: entry.document_hash,
    });
  }

  async function save() {
    if (!draft) return;
    try {
      if (editing) await updateGlossaryEntry(editing, draft);
      else await createGlossaryEntry(draft);
      setDraft(null);
      setEditing(null);
      onProposalConsumed();
      setMessage("Vnos je shranjen. Za vpliv na prevod ga je treba posebej odobriti.");
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Shranjevanje ni uspelo."); }
  }

  async function transition(entry: PersistentGlossaryEntry, action: "approve" | "reject" | "archive" | "restore") {
    try {
      await transitionGlossaryEntry(entry, action);
      setMessage(action === "approve" ? "Vnos je odobren za prihodnje prevode." : "Stanje vnosa je posodobljeno.");
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Sprememba stanja ni uspela."); }
  }

  async function showHistory(entry: PersistentGlossaryEntry) {
    try { setHistory({ id: entry.id, rows: await getGlossaryHistory(entry.id) }); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Zgodovine ni bilo mogoče prebrati."); }
  }

  async function previewImport(file: File | null) {
    if (!file) return;
    try { setPreview(await previewGlossaryCsv(file)); setMessage("Predogled je pripravljen; podatki še niso zapisani."); }
    catch (error) { setPreview(null); setMessage(error instanceof Error ? error.message : "Predogled uvoza ni uspel."); }
    finally { if (importRef.current) importRef.current.value = ""; }
  }

  async function confirmImport() {
    if (!preview) return;
    try {
      const result = await confirmGlossaryCsv(preview, overwrite);
      setMessage(`Uvoz: ${result.created} novih, ${result.updated} posodobljenih, ${result.skipped} preskočenih.`);
      setPreview(null);
      setOverwrite(false);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Uvoz ni uspel."); }
  }

  return <section className="persistent-glossary" aria-label="Trajni lokalni glosar">
    <div className="translation-subhead">
      <div><h3>Trajni lokalni glosar</h3><p>Shranjeni so samo termini in sledljivost, ne dokumenti ali odstavki.</p></div>
      <strong>{total + visibleBuiltIns.length} vnosov</strong>
    </div>
    <div className="segmented glossary-tabs" role="tablist">
      <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}>Pregled</button>
      <button className={tab === "import" ? "active" : ""} onClick={() => setTab("import")}>Uvoz in izvoz</button>
    </div>
    {message && <p className={available === false ? "translation-error" : "translation-provider-message"}>{message}</p>}
    {available === false && <button className="button secondary" onClick={() => void refresh()}>Poskusi znova</button>}

    {tab === "overview" && <>
      <div className="glossary-toolbar">
        <input aria-label="Išči po izrazih" placeholder="Išči izvorni ali ciljni izraz" value={filters.q ?? ""} onChange={(event) => setFilters({ ...filters, q: event.target.value })} />
        <select aria-label="Status glosarja" value={filters.status ?? ""} onChange={(event) => setFilters({ ...filters, status: event.target.value as GlossaryFilters["status"] })}>
          <option value="">Vsi statusi</option><option value="proposed">Predlagano</option><option value="approved">Odobreno</option><option value="rejected">Zavrnjeno</option><option value="archived">Arhivirano</option>
        </select>
        <select aria-label="Način ujemanja glosarja" value={filters.match_mode ?? ""} onChange={(event) => setFilters({ ...filters, match_mode: event.target.value as GlossaryFilters["match_mode"] })}>
          <option value="">Vsi načini</option><option value="exact_segment">Celoten segment</option><option value="preferred_term">Priporočeni termin</option><option value="do_not_translate">Ne prevajaj</option>
        </select>
        <select aria-label="Področje glosarja" value={filters.domain ?? ""} onChange={(event) => setFilters({ ...filters, domain: event.target.value })}>
          <option value="">Vsa področja</option>{domains.map((domain) => <option key={domain}>{domain}</option>)}
        </select>
        <select aria-label="Izvorni jezik glosarja" value={filters.source_language ?? ""} onChange={(event) => setFilters({ ...filters, source_language: event.target.value })}>
          <option value="">Vsi izvorni jeziki</option><option value="en">EN</option><option value="de">DE</option><option value="sl">SL</option>
        </select>
        <select aria-label="Ciljni jezik glosarja" value={filters.target_language ?? ""} onChange={(event) => setFilters({ ...filters, target_language: event.target.value })}>
          <option value="">Vsi ciljni jeziki</option><option value="sl">SL</option><option value="en">EN</option><option value="de">DE</option>
        </select>
        <button className="button secondary" disabled={available === false} onClick={newEntry}>Dodaj nov termin</button>
      </div>

      {draft && <GlossaryForm draft={draft} onChange={setDraft} onSave={() => void save()} onCancel={() => { setDraft(null); setEditing(null); onProposalConsumed(); }} />}

      <div className="glossary-table-wrap">
        <table className="glossary-table"><thead><tr><th>Izvor</th><th>Prevod</th><th>Področje</th><th>Način</th><th>Status</th><th>Vir</th><th>Dejanja</th></tr></thead>
          <tbody>
            {visibleBuiltIns.map((entry) => <tr key={entry.id}><td>{entry.source_term}</td><td>{entry.target_term}</td><td>organizacijski</td><td>Celoten segment</td><td>Odobreno</td><td><span className="glossary-badge built_in">BUILT-IN</span></td><td>Ni mogoče urejati</td></tr>)}
            {entries.map((entry) => <tr key={entry.id}>
              <td>{entry.source_term} {conflictIds.has(entry.id) && <span className="glossary-badge conflict">KONFLIKT</span>}</td><td>{entry.match_mode === "do_not_translate" ? "Ne prevajaj" : entry.target_term}</td><td>{entry.domain || "splošno"}</td><td>{modeLabel(entry.match_mode)}</td><td>{statusLabel(entry.status)}</td>
              <td><span className={`glossary-badge ${entry.origin}`}>{originLabel(entry.origin)}</span>{entry.source_label && <small>{entry.source_label}</small>}</td>
              <td><div className="compact-actions"><button onClick={() => edit(entry)}>Uredi</button><button onClick={() => void showHistory(entry)}>Zgodovina</button>
                {entry.status === "proposed" && <><button onClick={() => void transition(entry, "approve")}>Odobri</button><button onClick={() => void transition(entry, "reject")}>Zavrni</button></>}
                {entry.status === "archived" ? <button onClick={() => void transition(entry, "restore")}>Obnovi</button> : <button onClick={() => void transition(entry, "archive")}>Arhiviraj</button>}
              </div></td>
            </tr>)}
          </tbody></table>
      </div>
      {history && <details open className="glossary-history"><summary>Zgodovina vnosa</summary>{history.rows.map((row) => <p key={row.history_id}><strong>r{row.revision} · {row.action}</strong> <span>{new Date(row.created_at).toLocaleString("sl-SI")}</span> · {row.snapshot.source_term} → {row.snapshot.target_term}</p>)}</details>}
    </>}

    {tab === "import" && <div className="glossary-import-export">
      <div className="compact-actions"><a className="button secondary" href={glossaryExportUrl("csv")}>Izvozi CSV</a><a className="button secondary" href={glossaryExportUrl("json")}>Izvozi JSON</a><button className="button" onClick={() => importRef.current?.click()}>Predogled CSV uvoza</button></div>
      <input ref={importRef} className="visually-hidden" type="file" accept="text/csv,.csv" onChange={(event) => void previewImport(event.target.files?.[0] ?? null)} />
      {preview && <div className="import-preview"><dl><div><dt>Novi</dt><dd>{preview.new_count}</dd></div><div><dt>Dvojniki</dt><dd>{preview.duplicate_count}</dd></div><div><dt>Konflikti</dt><dd>{preview.conflict_count}</dd></div><div><dt>Neveljavni</dt><dd>{preview.invalid_count}</dd></div><div><dt>Preglasitve</dt><dd>{preview.override_count}</dd></div></dl>
        {preview.valid_rows.length > 0 && <div className="glossary-table-wrap">
          <h4>Vrednosti po varni obnovi</h4>
          <table className="glossary-table"><thead><tr><th>Vrstica</th><th>Izvor</th><th>Prevod</th><th>Status</th></tr></thead>
            <tbody>{preview.valid_rows.map((row) => <tr key={row.row_number}><td>{row.row_number}</td><td>{row.source_term}</td><td>{row.match_mode === "do_not_translate" ? "Ne prevajaj" : row.target_term}</td><td>{statusLabel(row.status)}</td></tr>)}</tbody>
          </table>
        </div>}
        {preview.issues.map((issue) => <p key={`${issue.row_number}-${issue.kind}`}>Vrstica {issue.row_number}: {issue.message}</p>)}
        {preview.conflict_count > 0 && <label><input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} /> Izrecno prepiši konflikte</label>}
        <button className="button" disabled={preview.valid_rows.length === 0} onClick={() => void confirmImport()}>Potrdi uvoz</button>
      </div>}
    </div>}
  </section>;
}

function GlossaryForm({ draft, onChange, onSave, onCancel }: { draft: GlossaryEntryDraft; onChange: (draft: GlossaryEntryDraft) => void; onSave: () => void; onCancel: () => void }) {
  const long = draft.source_term.length > 120 || /[.!?]\s/.test(draft.source_term);
  return <div className="glossary-form">
    {long && <p className="translation-warning">Daljša poved je primernejša za prihodnji prevajalski pomnilnik kot za glosar. Vnosa ne bomo samodejno razrezali.</p>}
    <label>Izvorni izraz<input value={draft.source_term} onChange={(event) => onChange({ ...draft, source_term: event.target.value })} /></label>
    <label>Ciljni izraz<input disabled={draft.match_mode === "do_not_translate"} value={draft.target_term} onChange={(event) => onChange({ ...draft, target_term: event.target.value })} /></label>
    <label>Področje<input value={draft.domain} onChange={(event) => onChange({ ...draft, domain: event.target.value })} /></label>
    <label>Izvorni jezik<select value={draft.source_language} onChange={(event) => onChange({ ...draft, source_language: event.target.value })}><option value="en">EN</option><option value="de">DE</option><option value="sl">SL</option></select></label>
    <label>Ciljni jezik<select value={draft.target_language} onChange={(event) => onChange({ ...draft, target_language: event.target.value })}><option value="sl">SL</option><option value="en">EN</option><option value="de">DE</option></select></label>
    <label>Način<select value={draft.match_mode} onChange={(event) => onChange({ ...draft, match_mode: event.target.value as GlossaryEntryDraft["match_mode"] })}><option value="exact_segment">Celoten segment</option><option value="preferred_term">Priporočeni termin</option><option value="do_not_translate">Ne prevajaj</option></select></label>
    <label>Izvorna oznaka<input value={draft.source_label ?? ""} onChange={(event) => onChange({ ...draft, source_label: event.target.value || null })} placeholder="Neobvezna nevtralna oznaka" /></label>
    <label>Opomba<textarea value={draft.note} onChange={(event) => onChange({ ...draft, note: event.target.value })} /></label>
    <label><input type="checkbox" checked={draft.case_sensitive} onChange={(event) => onChange({ ...draft, case_sensitive: event.target.checked })} /> Razlikuj velike in male črke</label>
    <div className="compact-actions"><button className="button" disabled={!draft.source_term.trim() || (draft.match_mode !== "do_not_translate" && !draft.target_term.trim())} onClick={onSave}>Shrani kot {draft.status === "approved" ? "odobreno" : "predlagano"}</button><button className="button secondary" onClick={onCancel}>Prekliči</button></div>
  </div>;
}

function modeLabel(mode: PersistentGlossaryEntry["match_mode"]) { return { exact_segment: "Celoten segment", preferred_term: "Priporočeni termin", do_not_translate: "Ne prevajaj" }[mode]; }
function statusLabel(status: PersistentGlossaryEntry["status"]) { return { proposed: "Predlagano", approved: "Odobreno", rejected: "Zavrnjeno", archived: "Arhivirano" }[status]; }
function originLabel(origin: PersistentGlossaryEntry["origin"]) { return { manual: "MANUAL", reviewed_correction: "AUTO/PROPOSED", import: "IMPORT", built_in: "BUILT-IN" }[origin]; }
