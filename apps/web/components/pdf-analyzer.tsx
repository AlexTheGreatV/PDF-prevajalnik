"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Image from "next/image";

import {
  checkBrowserOcrRuntime,
  explainBrowserOcrError,
} from "../lib/browser-ocr-provider";
import { ManualRegionSelector, type SelectionBox } from "./manual-region-selector";
import { useOcrQueue } from "./use-ocr-queue";
import { OcrReviewList, reasonLabel } from "./ocr-review-list";
import { TranslationWorkspace } from "./translation-workspace";
import { recognizeBestVariant, cropImageToCanvas, ensureImageReady } from "../lib/ocr-canvas";
import { analyzePdf, registerAndAcceptOcrSource, renderPdf } from "../lib/pdf-client";
import type { PdfAnalysis, RenderResult, StructuredBlock } from "../lib/pdf-types";
import {
  acceptOcrResult,
  applyOcrSuggestion,
  createManualOcrItem,
  createOcrReviewItems,
  displaySelectionToPdfBBox,
  markOcrProcessing,
  normalizeBBox,
  pdfBBoxToPixelBBox,
  relativeOcrPadding,
  rejectOcrResult,
  type OcrReviewItem,
} from "../lib/ocr-review";
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
type OcrAvailability = "checking" | "ready" | "unavailable";
const MIN_SELECTION_SIZE = 3;

export function PdfAnalyzer() {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const imageRefs = useRef<Record<number, HTMLImageElement | null>>({});
  const [file, setFile] = useState<File | null>(null);
  const [analysis, setAnalysis] = useState<PdfAnalysis | null>(null);
  const [renderResult, setRenderResult] = useState<RenderResult | null>(null);
  const [state, setState] = useState<ProcessingState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [visibleBlocks, setVisibleBlocks] = useState<Record<number, boolean>>({});
  const [textMode, setTextMode] = useState<"original" | "normalized">("normalized");
  const [selectedBlockKey, setSelectedBlockKey] = useState<string | null>(null);
  const [ocrAvailability, setOcrAvailability] = useState<OcrAvailability>("checking");
  const [ocrAvailabilityMessage, setOcrAvailabilityMessage] = useState("Preverjam lokalne OCR datoteke.");
  const [ocrItems, setOcrItems] = useState<OcrReviewItem[]>([]);
  const queue = useOcrQueue();
  const { isRunning: isOcrRunning, progress: ocrProgress, externalUrls: ocrNetworkReport, setProgress: setOcrProgress, setExternalUrls: setOcrNetworkReport } = queue;
  const [ocrError, setOcrError] = useState<string | null>(null);
  const [manualMode, setManualMode] = useState(false);
  const [pendingSelection, setPendingSelection] = useState<SelectionBox | null>(null);
  const [preparedSelection, setPreparedSelection] = useState<{
    item: OcrReviewItem; image: HTMLImageElement; crop: string; width: number; height: number;
  } | null>(null);
  const [preparingSelection, setPreparingSelection] = useState(false);
  const selectionGeneration = useRef(0);

  const selectedFileError = file ? validatePdfFile(file) : null;
  const totalBlocks = analysis ? countTextBlocks(analysis) : 0;
  const totalImages = analysis ? countImages(analysis) : 0;
  const pendingOcrCount = ocrItems.filter((item) => item.status === "pending").length;

  useEffect(() => {
    checkBrowserOcrRuntime()
      .then((result) => {
        if (result.ok) {
          setOcrAvailability("ready");
          setOcrAvailabilityMessage("OCR je pripravljen.");
          return;
        }
        setOcrAvailability("unavailable");
        setOcrAvailabilityMessage(`OCR ni na voljo. Manjka: ${result.missing.join(", ")}`);
      })
      .catch((caught) => {
        setOcrAvailability("unavailable");
        setOcrAvailabilityMessage(explainBrowserOcrError(caught));
      });

  }, []);

  const renderPagesByNumber = useMemo(() => {
    return new Map(renderResult?.pages.map((page) => [page.page_number, page]) ?? []);
  }, [renderResult]);

  const structuredPagesByNumber = useMemo(() => {
    return new Map(analysis?.document?.pages.map((page) => [page.page_number, page]) ?? []);
  }, [analysis]);

  function chooseFile(nextFile: File | null) {
    queue.cancel();
    selectionGeneration.current += 1;
    setPreparedSelection(null);
    setFile(nextFile);
    setAnalysis(null);
    setRenderResult(null);
    setVisibleBlocks({});
    setSelectedBlockKey(null);
    setOcrItems([]);
    setOcrError(null);
    setOcrProgress({ done: 0, total: 0 });
    setOcrNetworkReport([]);
    setManualMode(false);
    setPendingSelection(null);
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
    setOcrItems([]);
    setOcrError(null);
    setOcrProgress({ done: 0, total: 0 });
    setOcrNetworkReport([]);

    try {
      const [analysisPayload, renderPayload] = await Promise.all([
        analyzePdf(file),
        renderPdf(file),
      ]);
      setAnalysis(analysisPayload);
      setRenderResult(renderPayload);
      setOcrItems(createOcrReviewItems(analysisPayload));
      setState("done");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Analiza ni uspela.");
      setState("error");
    }
  }

  async function runOcrReview(targetItems?: OcrReviewItem[], preparedImage?: HTMLImageElement) {
    if (!analysis || !renderResult) return;
    setOcrError(null);
    await queue.run(
      targetItems ?? ocrItems.filter((item) => item.status === "pending"),
      async (item, provider, signal) => {
        const page = structuredPagesByNumber.get(item.page_number);
        if (!page) throw new Error("Stran za OCR ni več na voljo.");
        const image = preparedImage ?? await loadOcrPage(item.page_number);
        signal.throwIfAborted();
        const base = pdfBBoxToPixelBBox(item.pdf_bbox, page, image, 0);
        const padding = item.source === "manual" ? 4 : relativeOcrPadding(base);
        const pixels = pdfBBoxToPixelBBox(item.pdf_bbox, page, image, padding);
        updateOcrItem(item.id, () => markOcrProcessing(item, pixels));
        const result = await recognizeBestVariant(provider, image, item, pixels, page, padding, signal);
        return applyOcrSuggestion(item, result.best, result.attempts, result.cropImageUrl, result.cropDebug);
      },
      (item) => updateOcrItem(item.id, () => item),
    );
  }

  function cancelOcrReview() { queue.cancel(); }

  async function acceptOcrReview(id: string) {
    if (!file) return;
    const item = ocrItems.find((candidate) => candidate.id === id);
    if (!item) return;
    setOcrError(null);
    try {
      const revisionId = await registerAndAcceptOcrSource(file, item);
      updateOcrItem(id, (current) => acceptOcrResult(current, revisionId));
    } catch (caught) {
      setOcrError(caught instanceof Error ? caught.message : "Sprejem OCR revizije ni uspel.");
    }
  }

  async function loadOcrPage(pageNumber: number): Promise<HTMLImageElement> {
    if (!file) throw new Error("Izvorni PDF ni več na voljo.");
    const rendered = await renderPdf(file, { dpi: 300, page_number: pageNumber });
    const source = rendered.pages.find((page) => page.page_number === pageNumber);
    if (!source) throw new Error("Zahtevana OCR stran ni na voljo.");
    const image = new window.Image();
    image.src = source.path;
    await ensureImageReady(image);
    return image;
  }

  async function runManualOcr() {
    if (preparedSelection) {
      const prepared = preparedSelection;
      setPreparedSelection(null);
      setPendingSelection(null);
      setOcrItems((current) => [...current, prepared.item]);
      await runOcrReview([prepared.item], prepared.image);
      return;
    }
    if (!analysis || !pendingSelection) return;
    const page = structuredPagesByNumber.get(pendingSelection.page_number);
    const image = imageRefs.current[pendingSelection.page_number];
    if (!page || !image) return;
    const selection = normalizeBBox(pendingSelection);
    if (selection.x1 - selection.x0 < MIN_SELECTION_SIZE || selection.y1 - selection.y0 < MIN_SELECTION_SIZE) {
      setOcrError("Izbrano območje je premajhno za OCR.");
      return;
    }
    const rect = image.getBoundingClientRect();
    const pdfBox = displaySelectionToPdfBBox(
      selection,
      { width: page.width, height: page.height },
      { naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight, displayWidth: rect.width, displayHeight: rect.height },
    );
    const item = createManualOcrItem(page, pdfBox);
    const generation = ++selectionGeneration.current;
    setPreparingSelection(true);
    try {
      const highResolutionImage = await loadOcrPage(page.page_number);
      const pixels = pdfBBoxToPixelBBox(pdfBox, page, highResolutionImage, 4);
      const canvas = cropImageToCanvas(highResolutionImage, pixels, "original");
      if (generation === selectionGeneration.current) {
        setPreparedSelection({ item, image: highResolutionImage, crop: canvas.toDataURL("image/png"), width: canvas.width, height: canvas.height });
      }
    } catch (caught) {
      setOcrError(explainBrowserOcrError(caught));
    } finally {
      setPreparingSelection(false);
    }
  }

  function updateOcrItem(id: string, update: (item: OcrReviewItem) => OcrReviewItem) {
    setOcrItems((current) => current.map((item) => (item.id === id ? update(item) : item)));
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

      <section className="panel ocr-panel">
        <div>
          <span>Lokalni browser OCR</span>
          <strong>
            {ocrAvailability === "ready"
              ? "OCR je pripravljen"
              : ocrAvailability === "checking"
                ? "Preverjam OCR"
                : "OCR ni na voljo"}
          </strong>
          <small>{ocrAvailabilityMessage}</small>
          {ocrItems.length > 0 && (
            <small>
              {ocrItems.length} kandidatov · {pendingOcrCount} čakajočih
            </small>
          )}
        </div>
        <div className="actions">
          <button
            className="button secondary"
            disabled={
              !analysis ||
              !renderResult ||
              isOcrRunning ||
              pendingOcrCount === 0 ||
              ocrAvailability !== "ready"
            }
            onClick={() => void runOcrReview()}
          >
            {isOcrRunning ? "Preverjam sumljiva območja ..." : "OCR-preveri sumljive dele"}
          </button>
          {isOcrRunning && (
            <button className="button secondary" onClick={cancelOcrReview}>
              Prekliči OCR
            </button>
          )}
          <button
            className={`button secondary${manualMode ? " active" : ""}`}
            disabled={!analysis || !renderResult || isOcrRunning || ocrAvailability !== "ready"}
            onClick={() => setManualMode((current) => !current)}
          >
            Izberi območje za OCR
          </button>
        </div>
        {isOcrRunning && <progress value={ocrProgress.done} max={ocrProgress.total || 1} />}
        {manualMode && <small>Povlecite pravokotnik po predogledu strani in potrdite OCR območje.</small>}
        {pendingSelection && (
          <div className="actions">
            <button className="button" disabled={preparingSelection || isOcrRunning} onClick={() => void runManualOcr()}>
              {preparingSelection ? "Pripravljam izrez ..." : preparedSelection ? "Zaženi OCR izreza" : "Prikaži izrez pri 300 DPI"}
            </button>
            <button className="button secondary" onClick={() => { selectionGeneration.current += 1; setPendingSelection(null); setPreparedSelection(null); }}>
              Ponovi izbor
            </button>
          </div>
        )}
        {preparedSelection && (
          <div className="ocr-crop">
            <label>
              Razporeditev besedila
              <select value={preparedSelection.item.segmentation_mode ?? "sparse_text"} onChange={(event) => setPreparedSelection({ ...preparedSelection, item: { ...preparedSelection.item, segmentation_mode: event.target.value as "single_line" | "single_block" | "sparse_text" } })}>
                <option value="sparse_text">Redko besedilo</option>
                <option value="single_line">Ena vrstica</option>
                <option value="single_block">Blok besedila</option>
              </select>
            </label>
            <Image src={preparedSelection.crop} alt="Izbrani izrez pred OCR" width={preparedSelection.width} height={preparedSelection.height} unoptimized />
            <a href={preparedSelection.crop} download="ocr-selection.png">Prenesi izbrani izrez</a>
          </div>
        )}
      </section>

      {ocrError && <div className="error-box">{ocrError}</div>}
      {ocrNetworkReport.length > 0 && (
        <div className="error-box">OCR je zaznal zunanje zahteve: {ocrNetworkReport.join(", ")}</div>
      )}
      {ocrItems.length > 0 && (
        <OcrReviewList
          items={ocrItems}
          onAccept={(id) => void acceptOcrReview(id)}
          onReject={(id) => updateOcrItem(id, rejectOcrResult)}
        />
      )}

      {analysis?.document && file && (
        <div className="analysis-workbench">
          <div className="analysis-workbench-main">
            <TranslationWorkspace key={analysis.sha256} analysis={analysis} ocrItems={ocrItems} sourceFile={file} />
          </div>
          <aside className="document-side-preview" aria-label="Predogled PDF dokumenta">
            <h2>Predogled PDF</h2>
            {analysis.pages.map((page) => {
              const preview = renderPagesByNumber.get(page.page_number);
              return preview ? (
                <figure key={page.page_number}>
                  <figcaption>Stran {page.page_number}</figcaption>
                  <Image
                    src={preview.path}
                    alt={`Predogled strani ${page.page_number}`}
                    width={preview.width_px ?? 850}
                    height={preview.height_px ?? 1100}
                    unoptimized
                  />
                </figure>
              ) : null;
            })}
          </aside>
        </div>
      )}

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
              const pageOcrItems = ocrItems.filter((item) => item.page_number === page.page_number);

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
                        ref={(node) => {
                          imageRefs.current[page.page_number] = node;
                        }}
                        src={preview.path}
                        alt={`Predogled strani ${page.page_number}`}
                        width={preview.width_px ?? 850}
                        height={preview.height_px ?? 1100}
                        unoptimized
                      />
                      {structuredPage && pageOcrItems.length > 0 && (
                        <div className="ocr-overlay-layer">
                          {pageOcrItems.map((item) => (
                            <span
                              key={item.id}
                              className={`ocr-overlay ocr-${item.status}`}
                              style={structuredBlockOverlayStyle(
                                { bbox: item.pdf_bbox } as StructuredBlock,
                                structuredPage,
                              )}
                              title={`${reasonLabel(item.reason)} · ${item.status}`}
                            />
                          ))}
                        </div>
                      )}
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
                      {manualMode && (
                        <ManualRegionSelector pageNumber={page.page_number} selection={pendingSelection}
                          onSelection={(selection) => {
                            selectionGeneration.current += 1;
                            setPreparedSelection(null);
                            setPendingSelection(selection);
                          }}
                        />
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
