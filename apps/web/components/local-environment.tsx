"use client";

import { useEffect, useState } from "react";
import { apiBaseUrl } from "../lib/pdf-client";

type LocalEnvironment = {
  profile: string; data_scope: "production" | "isolated" | "mixed";
  glossary_database_path: string; translation_memory_database_path: string; translation_profile: string; ocr_database_path: string;
};

export function LocalEnvironmentSummary() {
  const [environment, setEnvironment] = useState<LocalEnvironment | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void fetch(`${apiBaseUrl}/api/environment`, { signal: controller.signal, cache: "no-store" })
      .then(async response => {
        if (!response.ok) throw new Error("environment unavailable");
        const value = await response.json() as LocalEnvironment;
        if (!["production", "isolated", "mixed"].includes(value.data_scope)
            || ![value.profile, value.glossary_database_path, value.translation_memory_database_path, value.translation_profile, value.ocr_database_path]
              .every(field => typeof field === "string" && field.length > 0)) throw new Error("invalid environment");
        if (!controller.signal.aborted) setEnvironment(value);
      }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, []);
  return <section className="panel" aria-label="Aktivno lokalno okolje">
    <h2>Lokalno okolje</h2>
    {!environment && <p role="status">{failed ? "Okolja ni mogoče preveriti. Poti baz niso potrjene." : "Preverjam aktivno okolje …"}</p>}
    {environment && <>
      <p><strong>{environment.profile}</strong> · {environment.data_scope === "isolated" ? "Testne / izolirane baze" : environment.data_scope === "production" ? "Produkcijski bazi" : "Mešano okolje: ena produkcijska baza"}</p>
      <dl>
        <dt>API</dt><dd><a href={`${apiBaseUrl}/health`} target="_blank" rel="noreferrer">{apiBaseUrl}</a></dd>
        <dt>Glosar</dt><dd style={{ overflowWrap: "anywhere" }}>{environment.glossary_database_path}</dd>
        <dt>Prevajalski spomin</dt><dd style={{ overflowWrap: "anywhere" }}>{environment.translation_memory_database_path}</dd>
        <dt>Trajna OCR-evidenca</dt><dd style={{ overflowWrap: "anywhere" }}>{environment.ocr_database_path}</dd>
        <dt>Modelni profil</dt><dd>{environment.translation_profile}</dd>
      </dl>
    </>}
  </section>;
}
