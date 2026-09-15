import Image from "next/image";
import type { OcrReviewItem } from "../lib/ocr-review";

export function OcrReviewList({
  items,
  onAccept,
  onReject,
}: {
  items: OcrReviewItem[];
  onAccept: (id: string) => void;
  onReject: (id: string) => void;
}) {
  return (
    <section className="panel ocr-results">
      <div className="page-header">
        <div>
          <h3>OCR pregled</h3>
          <p>{items.length} kandidatov · izvirno besedilo ostane nespremenjeno</p>
        </div>
      </div>
      {items.map((item) => (
        <div className={`ocr-candidate ocr-status-${item.status}`} key={item.id}>
          <div>
            <strong>
              Stran {item.page_number} · {reasonLabel(item.reason)}
            </strong>
            <span>
              {statusLabel(item.status)}
              {item.confidence !== null ? ` · zanesljivost ${item.confidence.toFixed(1)} %` : ""}
              {item.preprocessing_method ? ` · ${preprocessLabel(item.preprocessing_method)}` : ""}
              {item.segmentation_mode ? ` · ${segmentationLabel(item.segmentation_mode)}` : ""}
            </span>
            <small>
              PDF bbox: {item.pdf_bbox.x0.toFixed(1)}, {item.pdf_bbox.y0.toFixed(1)},{" "}
              {item.pdf_bbox.x1.toFixed(1)}, {item.pdf_bbox.y1.toFixed(1)}
            </small>
            {item.crop_debug && (
              <small>
                Render {item.crop_debug.render_width} x {item.crop_debug.render_height}px · pixel bbox{" "}
                {item.crop_debug.pixel_bbox.x0.toFixed(0)}, {item.crop_debug.pixel_bbox.y0.toFixed(0)},{" "}
                {item.crop_debug.pixel_bbox.x1.toFixed(0)}, {item.crop_debug.pixel_bbox.y1.toFixed(0)} ·
                canvas {item.crop_debug.canvas_width} x {item.crop_debug.canvas_height}px · padding{" "}
                {item.crop_debug.padding_px}px
              </small>
            )}
            {item.error && <em>{item.error}</em>}
          </div>
          {item.crop_image_url && item.crop_debug && (
            <div className="ocr-crop">
              <Image
                src={item.crop_image_url}
                alt={`OCR izrez strani ${item.page_number}`}
                width={item.crop_debug.canvas_width || 240}
                height={item.crop_debug.canvas_height || 80}
                unoptimized
              />
              <a className="button secondary" href={item.crop_image_url} download={`ocr-crop-${item.id}.png`}>
                Prenesi OCR izrez
              </a>
            </div>
          )}
          <div className="ocr-compare">
            <div>
              <h4>Izvorno</h4>
              <pre>{item.native_text || "Ni izvornega besedila."}</pre>
            </div>
            <div>
              <h4>OCR</h4>
              <pre>{item.ocr_text || "Ni OCR rezultata."}</pre>
            </div>
            <div>
              <h4>Izbrano</h4>
              <pre>{item.effective_text || "Ni izbranega besedila."}</pre>
            </div>
          </div>
          {item.attempts.length > 0 && (
            <div className="ocr-attempts">
              <h4>OCR poskusi</h4>
              <table>
                <thead>
                  <tr>
                    <th>Predobdelava</th>
                    <th>Način</th>
                    <th>Zanesljivost</th>
                    <th>Rezultat</th>
                  </tr>
                </thead>
                <tbody>
                  {item.attempts.map((attempt, index) => (
                    <tr key={`${item.id}-attempt-${index}`}>
                      <td>{preprocessLabel(attempt.preprocessing_method)}</td>
                      <td>{segmentationLabel(attempt.segmentation_mode)}</td>
                      <td>{attempt.confidence.toFixed(1)} %</td>
                      <td>{attempt.text || "Ni besedila."}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="ocr-actions">
            <button
              className="button secondary"
              disabled={item.status !== "suggested" && item.status !== "accepted"}
              onClick={() => onReject(item.id)}
            >
              Ohrani izvirnik
            </button>
            <button
              className="button secondary"
              disabled={!item.ocr_text || !["suggested", "rejected", "accepted"].includes(item.status)}
              onClick={() => onAccept(item.id)}
            >
              {item.status === "accepted" ? "Ponovno registriraj in sprejmi OCR" : "Uporabi OCR rezultat"}
            </button>
          </div>
        </div>
      ))}
    </section>
  );
}

function statusLabel(status: OcrReviewItem["status"]): string {
  const labels: Record<OcrReviewItem["status"], string> = {
    pending: "čaka",
    processing: "v obdelavi",
    suggested: "predlog pripravljen",
    accepted: "OCR sprejet",
    rejected: "izvirnik ohranjen",
    failed: "napaka",
    needs_review: "potreben je ročni pregled",
  };
  return labels[status];
}

export function reasonLabel(reason: OcrReviewItem["reason"]): string {
  const labels: Record<OcrReviewItem["reason"], string> = {
    scanned_page: "skenirana stran",
    low_native_text: "malo izvornega besedila",
    suspicious_glyph: "sumljiv znak",
    image_text_candidate: "besedilo v sliki",
    font_encoding_anomaly: "font encoding",
    manual_selection: "ročni izbor",
  };
  return labels[reason];
}

function preprocessLabel(method: string): string {
  const labels: Record<string, string> = {
    original: "izvirni izrez",
    "scale-2": "2x povečava",
    "scale-3": "3x povečava",
    "grayscale-2": "sivinsko 2x",
    "contrast-2": "kontrast 2x",
    "threshold-2": "prag 2x",
  };
  return labels[method] ?? method;
}

function segmentationLabel(method: OcrReviewItem["segmentation_mode"]): string {
  const labels: Record<NonNullable<OcrReviewItem["segmentation_mode"]>, string> = {
    single_line: "ena vrstica",
    single_block: "blok besedila",
    sparse_text: "redko besedilo",
  };
  return method ? labels[method] : "privzeto";
}
