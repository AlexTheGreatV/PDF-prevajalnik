"use client";

import { useMemo, useRef, useState } from "react";
import Image from "next/image";

import { analyzePdf, renderPdf } from "../lib/pdf-client";
import type { PdfAnalysis, RenderResult, StructuredBlock } from "../lib/pdf-types";
import {
  blockOverlayStyle,
  countImages,
  countTextBlocks,
  flattenStructuredBlocks,
  formatBytes,
  formatPdfKind,
  pageText,
  structuredBlockOverlayStyle,
  structuredPageText,
  validatePdfFile,
} from "../lib/pdf-view";

type ProcessingState = "idle" | "ready" | "analyzing" | "done" | "error";

export function PdfAnalyzer() {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [analysis, setAnalysis] = useState<PdfAnalysis | null>(null);
  const [renderResult, setRenderResult] = useState<RenderResult | null>(null);
  const [state, setState] = useState<ProcessingState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [visibleBlocks, setVisibleBlocks] = useState<Record<number, boolean>>({});
  const [textMode, setTextMode] = useState<"original" | "normalized">("normalized");
  const [selectedBlockKey, setSelectedBlockKey] = useState<string | null>(null);

  const selectedFileError = file ? validatePdfFile(file) : null;
  const totalBlocks = analysis ? countTextBlocks(analysis) : 0;
  const totalImages = analysis ? countImages(analysis) : 0;

  const renderPagesByNumber = useMemo(() => {
    return new Map(renderResult?.pages.map((page) => [page.page_number, page]) ?? []);
  }, [renderResult]);

  function chooseFile(nextFile: File | null) {
    setFile(nextFile);
    setAnalysis(null);
    setRenderResult(null);
    setVisibleBlocks({});
    setSelectedBlockKey(null);
    setError(nextFile ? validatePdfFile(nextFile) : null);
    setState(nextFile ? "ready" : "idle");
  }

  async function runAnalysis() {
    if (!file) return;

    const validationError = validatePdfFile(file);
    if (validationError) {
      setError(validationError);
      setState("error");
      return;
    }

    setState("analyzing");
    setError(null);
    setAnalysis(null);
    setRenderResult(null);
    setSelectedBlockKey(null);

    try {
      const [analysisPayload, renderPayload] = await Promise.all([
        analyzePdf(file),
        renderPdf(file),
      ]);
      setAnalysis(analysisPayload);
      setRenderResult(renderPayload);
      setState("done");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Analiza ni uspela.");
      setState("error");
    }
  }

  return (
    <div className="pipeline">
      <section
        className={`dropzone${isDragging ? " is-dragging" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setIsDragging(false);
          chooseFile(event.dataTransfer.files?.[0] ?? null);
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept="application/pdf"
          className="visually-hidden"
          onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
        />
        <div>
          <h2>Povleci PDF sem</h2>
          <p>Datoteka ostane lokalno v aplikaciji in se po obdelavi počisti.</p>
        </div>
        <button className="button secondary" onClick={() => inputRef.current?.click()}>
          Izberi PDF
        </button>
      </section>

      <section className="panel control-panel">
        <div className="file-row">
          <div>
            <span>Izbrana datoteka</span>
            <strong>{file ? file.name : "Ni izbrane datoteke"}</strong>
            {file && <small>{formatBytes(file.size)}</small>}
          </div>
          <button
            className="button"
            disabled={!file || state === "analyzing" || Boolean(selectedFileError)}
            onClick={runAnalysis}
          >
            Analiziraj PDF
          </button>
        </div>

        <div className={`status ${state}`}>
          {state === "idle" && "Izberi PDF za analizo."}
          {state === "ready" && (selectedFileError ?? "PDF je pripravljen za analizo.")}
          {state === "analyzing" && "Obdelujem PDF in renderiram strani..."}
          {state === "done" && "Analiza je zaključena."}
          {state === "error" && "Analiza ni uspela."}
        </div>

        {(error || selectedFileError) && (
          <div className="error-box">{error ?? selectedFileError}</div>
        )}
      </section>

      {analysis && (
        <>
          <section className="summary-grid">
            <Metric label="Vrsta PDF-ja" value={formatPdfKind(analysis.kind)} />
            <Metric label="Strani" value={analysis.page_count.toString()} />
            <Metric label="Besedilo" value={`${analysis.extracted_text_chars} znakov`} />
            <Metric label="Slike" value={totalImages.toString()} />
            <Metric label="Besedilni bloki" value={totalBlocks.toString()} />
          </section>

          <section className="pages">
            {analysis.pages.map((page) => {
              const preview = renderPagesByNumber.get(page.page_number);
              const showBlocks = visibleBlocks[page.page_number] ?? false;
              const structuredPage = analysis.document?.pages.find(
                (candidate) => candidate.page_number === page.page_number,
              );
              const structuredBlocks = structuredPage ? flattenStructuredBlocks(structuredPage) : [];
              const extractedText = structuredPage
                ? structuredPageText(structuredPage, textMode)
                : pageText(page);
              const tables = structuredBlocks.filter((block) => block.kind === "table");
              const selectedBlock =
                selectedBlockKey && structuredBlocks.length
                  ? structuredBlocks.find(
                      (block) => `${page.page_number}-${block.reading_order}` === selectedBlockKey,
                    )
                  : null;

              return (
                <article className="page-detail" key={page.page_number}>
                  <div className="page-header">
                    <div>
                      <h3>Stran {page.page_number}</h3>
                      <p>
                        {formatPdfKind(page.kind)} · {Math.round(page.dimensions.width)} x{" "}
                        {Math.round(page.dimensions.height)} {page.dimensions.unit} ·{" "}
                        {page.text_blocks.length} blokov · {page.image_count} slik
                      </p>
                    </div>
                    <label className="toggle">
                      <input
                        type="checkbox"
                        checked={showBlocks}
                        onChange={(event) =>
                          setVisibleBlocks((current) => ({
                            ...current,
                            [page.page_number]: event.target.checked,
                          }))
                        }
                      />
                      Prikaži bloke
                    </label>
                  </div>

                  {structuredPage && (
                    <div className="diagnostic-toolbar">
                      <div className="segmented">
                        <button
                          className={textMode === "original" ? "active" : ""}
                          onClick={() => setTextMode("original")}
                        >
                          Izvirno
                        </button>
                        <button
                          className={textMode === "normalized" ? "active" : ""}
                          onClick={() => setTextMode("normalized")}
                        >
                          Normalizirano
                        </button>
                      </div>
                      <span>
                        {structuredBlocks.length} strukturiranih blokov ·{" "}
                        {tables.length} zaznanih tabel
                      </span>
                    </div>
                  )}

                  {preview && (
                    <div className="preview-wrap">
                      <Image
                        src={preview.path}
                        alt={`Predogled strani ${page.page_number}`}
                        width={preview.width_px ?? 850}
                        height={preview.height_px ?? 1100}
                        unoptimized
                      />
                      {showBlocks && (
                        <div className="block-layer">
                          {structuredPage
                            ? structuredBlocks.map((block) => {
                                const key = `${page.page_number}-${block.reading_order}`;
                                return (
                                  <button
                                    key={key}
                                    type="button"
                                    className={`block-box block-${block.kind}${
                                      selectedBlockKey === key ? " selected" : ""
                                    }`}
                                    style={structuredBlockOverlayStyle(block, structuredPage)}
                                    onClick={() => setSelectedBlockKey(key)}
                                    aria-label={`Blok ${block.reading_order}: ${block.kind}`}
                                  >
                                    {block.reading_order}
                                  </button>
                                );
                              })
                            : page.text_blocks.map((block) => (
                                <span
                                  key={`${page.page_number}-${block.block_number}`}
                                  className="block-box"
                                  style={blockOverlayStyle(block, page)}
                                />
                              ))}
                        </div>
                      )}
                    </div>
                  )}

                  <div className="page-columns">
                    <div>
                      <h4>{textMode === "original" ? "Izvirno besedilo" : "Normalizirano besedilo"}</h4>
                      <pre>{extractedText || "Na tej strani ni izluščenega besedila."}</pre>
                    </div>
                    <div>
                      <h4>Strukturirani bloki</h4>
                      <div className="blocks-list">
                        {structuredBlocks.length === 0 && page.text_blocks.length === 0 && (
                          <p>Ni besedilnih blokov.</p>
                        )}
                        {structuredBlocks.length > 0
                          ? structuredBlocks.map((block) => (
                              <BlockRow
                                key={`${page.page_number}-${block.reading_order}`}
                                block={block}
                                active={selectedBlockKey === `${page.page_number}-${block.reading_order}`}
                                onSelect={() =>
                                  setSelectedBlockKey(`${page.page_number}-${block.reading_order}`)
                                }
                              />
                            ))
                          : page.text_blocks.map((block) => (
                              <div className="block-row" key={`${page.page_number}-${block.block_number}`}>
                                <strong>#{block.block_number}</strong>
                                <span>
                                  x0 {block.x0.toFixed(1)}, y0 {block.y0.toFixed(1)}, x1{" "}
                                  {block.x1.toFixed(1)}, y1 {block.y1.toFixed(1)}
                                </span>
                                <p>{block.text}</p>
                              </div>
                            ))}
                      </div>
                    </div>
                  </div>

                  {selectedBlock && <BlockInspector block={selectedBlock} />}

                  {tables.length > 0 && (
                    <div className="tables-view">
                      <h4>Zaznane tabele</h4>
                      {tables.map((table) => (
                        <table key={`${page.page_number}-table-${table.reading_order}`}>
                          <tbody>
                            {(table.table_cells ?? []).map((row, rowIndex) => (
                              <tr key={`${table.reading_order}-${rowIndex}`}>
                                {row.map((cell, cellIndex) => (
                                  <td key={`${table.reading_order}-${rowIndex}-${cellIndex}`}>
                                    {cell}
                                  </td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      ))}
                    </div>
                  )}
                </article>
              );
            })}
          </section>
        </>
      )}
    </div>
  );
}

function BlockRow({
  block,
  active,
  onSelect,
}: {
  block: StructuredBlock;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button className={`block-row diagnostic-row ${active ? "active" : ""}`} onClick={onSelect}>
      <strong>
        #{block.reading_order} · {blockKindLabel(block.kind)}
      </strong>
      <span>
        x0 {block.bbox.x0.toFixed(1)}, y0 {block.bbox.y0.toFixed(1)}, x1{" "}
        {block.bbox.x1.toFixed(1)}, y1 {block.bbox.y1.toFixed(1)}
      </span>
      {block.warnings.length > 0 && <em>Opozorilo: nizka zanesljivost, kandidat za OCR preverbo</em>}
      <p>{block.normalized_text || block.original_text}</p>
    </button>
  );
}

function BlockInspector({ block }: { block: StructuredBlock }) {
  const firstSpan = block.lines[0]?.spans[0];
  return (
    <div className="inspector">
      <h4>Podrobnosti bloka #{block.reading_order}</h4>
      <dl>
        <div>
          <dt>Vrsta</dt>
          <dd>{blockKindLabel(block.kind)}</dd>
        </div>
        <div>
          <dt>Koordinate</dt>
          <dd>
            {block.bbox.x0.toFixed(1)}, {block.bbox.y0.toFixed(1)}, {block.bbox.x1.toFixed(1)},{" "}
            {block.bbox.y1.toFixed(1)}
          </dd>
        </div>
        <div>
          <dt>Pisava</dt>
          <dd>{block.style.font ?? firstSpan?.font ?? "neznano"}</dd>
        </div>
        <div>
          <dt>Velikost</dt>
          <dd>{(block.style.font_size ?? firstSpan?.font_size ?? 0).toFixed(1)} pt</dd>
        </div>
        <div>
          <dt>Slog</dt>
          <dd>
            {block.style.bold ? "bold" : "regular"}
            {block.style.italic ? ", italic" : ""}
          </dd>
        </div>
        <div>
          <dt>Zanesljivost</dt>
          <dd>{Math.round(block.confidence * 100)} %</dd>
        </div>
      </dl>
    </div>
  );
}

function blockKindLabel(kind: StructuredBlock["kind"]): string {
  const labels: Record<StructuredBlock["kind"], string> = {
    heading: "naslov",
    paragraph: "odstavek",
    list: "seznam",
    list_item: "alineja",
    table: "tabela",
    caption: "napis",
    unknown: "neznano",
  };
  return labels[kind];
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
