"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createTranslationMemoryEntry,
  createLatestTranslationMemoryRequestCoordinator,
  getTranslationMemoryHistory,
  listTranslationMemoryEntries,
  transitionTranslationMemoryEntry,
  type TranslationMemoryDraft,
  type TranslationMemoryEntry,
  type TranslationMemoryLoadState,
  type TranslationMemoryStatus,
} from "../lib/translation-memory";

export type TranslationMemoryProposal = TranslationMemoryDraft & { key: string };

export function TranslationMemoryManager({
  proposal,
  reloadRequest,
  onProposalConsumed,
  onApprovedEntriesChange,
}: {
  proposal: TranslationMemoryProposal | null;
  reloadRequest: number;
  onProposalConsumed: () => void;
  onApprovedEntriesChange: (state: TranslationMemoryLoadState) => void;
}) {
  const [entries, setEntries] = useState<TranslationMemoryEntry[]>([]);
  const loadCoordinator = useRef(createLatestTranslationMemoryRequestCoordinator());
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<TranslationMemoryStatus | "">("");
  const [sourceLanguage, setSourceLanguage] = useState("");
  const [targetLanguage, setTargetLanguage] = useState("");
  const [domain, setDomain] = useState("");
  const [message, setMessage] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const submittedProposal = useRef<string | null>(null);

  const load = useCallback(async () => {
    await loadCoordinator.current.run(async () => {
      const [all, approved] = await Promise.all([
        listTranslationMemoryEntries(), listTranslationMemoryEntries("approved"),
      ]);
      return { all, approved };
    }, {
      onLoading: () => onApprovedEntriesChange({ status: "loading" }),
      onReady: ({ all, approved }) => {
        setEntries(all.entries);
        onApprovedEntriesChange({ status: "ready", entries: approved.entries, active_set_hash: approved.active_set_hash });
        setMessage("");
      },
      onError: (error) => {
        const text = error instanceof Error ? error.message : "Prevajalskega spomina ni mogoče naložiti.";
        setMessage(text);
        onApprovedEntriesChange({ status: "error", message: text });
      },
    });
  }, [onApprovedEntriesChange]);

  useEffect(() => {
    let active = true;
    const coordinator = loadCoordinator.current;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; coordinator.invalidate(); };
  }, [load, reloadRequest]);

  useEffect(() => {
    if (!proposal || submittedProposal.current === proposal.key) return;
    submittedProposal.current = proposal.key;
    const draft: TranslationMemoryDraft = {
      source_text: proposal.source_text, target_text: proposal.target_text,
      source_language: proposal.source_language, target_language: proposal.target_language,
      domain: proposal.domain, status: proposal.status, origin: proposal.origin,
      source_document_sha256: proposal.source_document_sha256,
      source_segment_id: proposal.source_segment_id, source_kind: proposal.source_kind, note: proposal.note,
    };
    void createTranslationMemoryEntry(draft)
      .then(() => { setMessage("Predlog je shranjen. Pred uporabo ga morate posebej odobriti."); return load(); })
      .catch((error) => setMessage(error instanceof Error ? error.message : "Predloga ni bilo mogoče shraniti."))
      .finally(onProposalConsumed);
  }, [proposal, load, onProposalConsumed]);

  async function transition(entry: TranslationMemoryEntry, action: "approve" | "reject" | "archive" | "restore") {
    try {
      await transitionTranslationMemoryEntry(entry, action);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Sprememba stanja ni uspela.");
    }
  }

  async function showHistory(entry: TranslationMemoryEntry) {
    try {
      const records = await getTranslationMemoryHistory(entry.id);
      setHistory(records.map((record) => `${record.revision}: ${record.action}`));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Zgodovine ni mogoče prikazati.");
    }
  }

  const visible = entries.filter((entry) => (!status || entry.status === status) &&
    (!sourceLanguage || entry.source_language === sourceLanguage) &&
    (!targetLanguage || entry.target_language === targetLanguage) &&
    (!domain || entry.domain === domain) &&
    (!query || `${entry.source_text}\n${entry.target_text}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  const sourceLanguages = [...new Set(entries.map((entry) => entry.source_language))].sort();
  const targetLanguages = [...new Set(entries.map((entry) => entry.target_language))].sort();
  const domains = [...new Set(entries.map((entry) => entry.domain).filter(Boolean))].sort();

  return <details className="glossary-manager">
    <summary>Prevajalski spomin <span>{entries.length}</span></summary>
    <div className="glossary-toolbar">
      <input aria-label="Iskanje po prevajalskem spominu" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Išči izvor ali prevod" />
      <select aria-label="Stanje prevajalskega spomina" value={status} onChange={(event) => setStatus(event.target.value as TranslationMemoryStatus | "")}>
        <option value="">Vsa stanja</option><option value="proposed">Predlagani</option><option value="approved">Odobreni</option><option value="rejected">Zavrnjeni</option><option value="archived">Arhivirani</option>
      </select>
      <select aria-label="Izvorni jezik prevajalskega spomina" value={sourceLanguage} onChange={(event) => setSourceLanguage(event.target.value)}><option value="">Vsi izvorni jeziki</option>{sourceLanguages.map((value) => <option key={value}>{value}</option>)}</select>
      <select aria-label="Ciljni jezik prevajalskega spomina" value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)}><option value="">Vsi ciljni jeziki</option>{targetLanguages.map((value) => <option key={value}>{value}</option>)}</select>
      <select aria-label="Domena prevajalskega spomina" value={domain} onChange={(event) => setDomain(event.target.value)}><option value="">Vse domene</option>{domains.map((value) => <option key={value}>{value}</option>)}</select>
      <button className="button secondary" onClick={() => void load()}>Osveži</button>
    </div>
    {message && <p className="translation-provider-message">{message}</p>}
    <div className="glossary-entry-list">
      {visible.map((entry) => <article key={entry.id} className="glossary-entry-row">
        <div><strong>{entry.source_text}</strong><span>{entry.target_text}</span><small>{entry.status} · rev. {entry.revision} · uporaba {entry.usage_count}{entry.domain ? ` · ${entry.domain}` : " · splošno"}</small></div>
        <div className="translation-row-actions">
          {entry.status === "proposed" && <><button className="button secondary" onClick={() => void transition(entry, "approve")}>Odobri</button><button className="button secondary" onClick={() => void transition(entry, "reject")}>Zavrni</button></>}
          {entry.status !== "archived" && <button className="button secondary" onClick={() => void transition(entry, "archive")}>Arhiviraj</button>}
          {entry.status === "archived" && <button className="button secondary" onClick={() => void transition(entry, "restore")}>Obnovi kot predlog</button>}
          <button className="button secondary" onClick={() => void showHistory(entry)}>Zgodovina</button>
        </div>
        {entry.source_text.length > 1_500 && <p className="translation-warning">Zelo dolg segment preverite pred odobritvijo.</p>}
      </article>)}
    </div>
    {history.length > 0 && <p className="translation-origin">Zgodovina: {history.join(" · ")}</p>}
    <p className="translation-origin">Celotne povedi se shranijo samo lokalno in samo po izrecnem predlogu uporabnika. Modelni rezultati se ne shranjujejo samodejno; PDF in slike se ne shranijo. Hash dokumenta je samo sledljivost izvora.</p>
    <p className="translation-origin">JSON izvoz je namenjen varni kopiji; uvoz v mejniku 8D1 še ni podprt.</p>
  </details>;
}
