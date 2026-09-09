"use client";

import { useRef, useState, type PointerEvent } from "react";
import { normalizeBBox } from "../lib/ocr-review";

export type SelectionBox = { page_number: number; x0: number; y0: number; x1: number; y1: number };

export function ManualRegionSelector({ pageNumber, selection, onSelection }: {
  pageNumber: number;
  selection: SelectionBox | null;
  onSelection: (selection: SelectionBox | null) => void;
}) {
  const active = useRef<SelectionBox | null>(null);
  const [drag, setDrag] = useState<SelectionBox | null>(null);
  function point(event: PointerEvent<HTMLDivElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: Math.max(0, Math.min(rect.width, event.clientX - rect.left)), y: Math.max(0, Math.min(rect.height, event.clientY - rect.top)) };
  }
  const box = drag ?? (selection?.page_number === pageNumber ? selection : null);
  const normalized = box ? normalizeBBox(box) : null;
  return (
    <div className="manual-selection-layer"
      onPointerDown={(event) => {
        const { x, y } = point(event);
        event.currentTarget.setPointerCapture(event.pointerId);
        onSelection(null);
        active.current = { page_number: pageNumber, x0: x, y0: y, x1: x, y1: y };
        setDrag(active.current);
      }}
      onPointerMove={(event) => {
        if (!active.current) return;
        const { x, y } = point(event);
        active.current = { ...active.current, x1: x, y1: y };
        setDrag(active.current);
      }}
      onPointerUp={(event) => {
        if (!active.current) return;
        const { x, y } = point(event);
        onSelection(normalizeBBox({ ...active.current, x1: x, y1: y }));
        active.current = null;
        setDrag(null);
      }}
      onPointerCancel={() => { active.current = null; setDrag(null); onSelection(null); }}
    >
      {normalized && <span className="selection-rect" style={{ left: normalized.x0, top: normalized.y0, width: normalized.x1 - normalized.x0, height: normalized.y1 - normalized.y0 }} />}
    </div>
  );
}
