"use client";

import { useMemo, useRef, useState } from "react";
import Image from "next/image";

import { analyzePdf, renderPdf } from "../lib/pdf-client";
import type { PdfAnalysis, RenderResult } from "../lib/pdf-types";
import {
  blockOverlayStyle,
  countImages,
  countTextBlocks,
  formatBytes,
  formatPdfKind,
  pageText,
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
              const extractedText = pageText(page);

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
                        <div className="block-layer" aria-hidden="true">
                          {page.text_blocks.map((block) => (
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
                      <h4>Izluščeno besedilo</h4>
                      <pre>{extractedText || "Na tej strani ni izluščenega besedila."}</pre>
                    </div>
                    <div>
                      <h4>Besedilni bloki</h4>
                      <div className="blocks-list">
                        {page.text_blocks.length === 0 && <p>Ni besedilnih blokov.</p>}
                        {page.text_blocks.map((block) => (
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
                </article>
              );
            })}
          </section>
        </>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
