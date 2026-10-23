from __future__ import annotations

import html
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.pdf_export_models import PdfExportSegment, PdfTextStyle

REQUIRED_GLYPHS = "čšžČŠŽ₂°C"
FAMILY_NAME = "FiraGO"
MIN_SOURCE_FONT_SIZE = 0.1
MAX_SOURCE_FONT_SIZE = 72.0


@dataclass(frozen=True)
class FontAsset:
    code: str
    family: str
    path: Path
    bold: bool
    italic: bool


class PdfTypographyResolver:
    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = temp_dir
        self.assets = self._prepare_fonts()
        fallback_font = pymupdf.Font("china-s")
        fallback_path = temp_dir / "DroidSansFallback.ttf"
        fallback_path.write_bytes(fallback_font.buffer)
        self.fallback = FontAsset("china-s", "Droid Sans Fallback Regular", fallback_path, False, False)
        self.archive = pymupdf.Archive(str(temp_dir))

    def resolve(
        self,
        page: pymupdf.Page,
        rect: pymupdf.Rect,
        segment: PdfExportSegment,
        *,
        layout_rect: pymupdf.Rect | None = None,
    ) -> tuple[PdfTextStyle, FontAsset | None, list[str], int]:
        lines, spans = _spans_in_rect(page, rect)
        warnings: list[str] = []
        if spans:
            weights: dict[tuple, int] = {}
            for span in spans:
                key = (span["font"], round(span["size"], 3), span["flags"], span["color"])
                weights[key] = weights.get(key, 0) + max(1, len(span["text"].strip()))
            dominant, dominant_weight = max(weights.items(), key=lambda item: item[1])
            total_weight = sum(weights.values())
            confidence = dominant_weight / total_weight
            if len(weights) > 1 and confidence < 0.9:
                warnings.append("mixed_source_style")
            font_name, font_size, flags, color_value = dominant
            bold = bool(flags & 16)
            italic = bool(flags & 2)
            source_xref = _font_xref(page, font_name)
        else:
            hint = segment.source_style
            font_name = hint.font if hint else None
            font_size = float(hint.font_size if hint and hint.font_size else 10.0)
            bold = bool(hint.bold if hint else False)
            italic = bool(hint.italic if hint else False)
            color_value = _integer_color(hint.color if hint else None)
            source_xref = None
            confidence = 0.0
            warnings.append("source_style_fallback")
        line_height = _line_height(lines, font_size)
        direction = tuple(lines[0]["dir"]) if lines else (1.0, 0.0)
        style = PdfTextStyle(
            source_font_name=font_name,
            source_font_xref=source_xref,
            font_size=font_size,
            bold=bold,
            italic=italic,
            color=_color_hex(color_value),
            line_height=line_height,
            alignment=_alignment(lines, layout_rect or rect),
            writing_direction=direction,
            rotation=page.rotation,
            dominant_style_confidence=round(confidence, 4),
        )
        asset = self.assets[(bold, italic)]
        if not self.supports(asset, segment.edited_translation):
            if self.supports(self.fallback, segment.edited_translation):
                asset = self.fallback
                warnings.append("droid_glyph_fallback")
                if bold or italic:
                    warnings.append("font_style_fallback")
            else:
                asset = None
        return style, asset, warnings, len(lines)

    def supports(self, asset: FontAsset, text: str) -> bool:
        font = pymupdf.Font(fontfile=str(asset.path))
        return all(character.isspace() or font.has_glyph(ord(character)) != 0 for character in text)

    def source_font_sizes(self, page: pymupdf.Page, rect: pymupdf.Rect) -> list[float]:
        """Return server-observed source sizes for the spans inside a proven rectangle."""
        _, spans = _spans_in_rect(page, rect)
        sizes: list[float] = []
        for span in spans:
            try:
                size = float(span.get("size", 0))
            except (TypeError, ValueError):
                return []
            if not math.isfinite(size) or not MIN_SOURCE_FONT_SIZE <= size <= MAX_SOURCE_FONT_SIZE:
                return []
            sizes.append(size)
        return sorted(sizes)

    def html_and_css(
        self,
        text: str,
        style: PdfTextStyle,
        asset: FontAsset,
        *,
        use_full_table_interior: bool = False,
    ) -> tuple[str, str]:
        weight = 700 if asset.bold else 400
        font_style = "italic" if asset.italic else "normal"
        css_family = f"{FAMILY_NAME}-{asset.code}"
        escaped = _escape_without_font_ligatures(text)
        body_css = "body,p {margin:0;padding:0;}" if use_full_table_interior else ""
        css = body_css + (
            f"@font-face {{font-family:'{css_family}';src:url('{asset.path.name}');font-weight:{weight};font-style:{font_style};}}"
            f"* {{font-family:'{css_family}';font-size:{style.font_size:.4f}pt;font-weight:{weight};font-style:{font_style};"
            f"font-variant-ligatures:none;font-feature-settings:'liga' 0,'clig' 0;"
            f"color:{style.color};line-height:{style.line_height:.4f};text-align:{style.alignment};margin:0;padding:0;}}"
        )
        return f"<p>{escaped}</p>", css

    def _prepare_fonts(self) -> dict[tuple[bool, bool], FontAsset]:
        definitions = {
            (False, False): ("figo", "FiraGO-Regular.ttf"),
            (True, False): ("figbo", "FiraGO-Bold.ttf"),
            (False, True): ("figit", "FiraGO-Italic.ttf"),
            (True, True): ("figbi", "FiraGO-BoldItalic.ttf"),
        }
        assets = {}
        for (bold, italic), (code, filename) in definitions.items():
            font = pymupdf.Font(code)
            if any(font.has_glyph(ord(character)) == 0 for character in REQUIRED_GLYPHS):
                raise RuntimeError(f"FiraGO font {code} is missing required glyphs")
            path = self.temp_dir / filename
            path.write_bytes(font.buffer)
            assets[(bold, italic)] = FontAsset(code, FAMILY_NAME, path, bold, italic)
        return assets


def _spans_in_rect(page: pymupdf.Page, rect: pymupdf.Rect) -> tuple[list[dict], list[dict]]:
    raw = page.get_text("dict", clip=rect, sort=True)
    lines: list[dict] = []
    spans: list[dict] = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            line_spans = [{**span, "text": span.get("text", "")} for span in line.get("spans", []) if span.get("text", "").strip()]
            if line_spans:
                lines.append(line)
                spans.extend(line_spans)
    return lines, spans


def _font_xref(page: pymupdf.Page, font_name: str | None) -> int | None:
    for item in page.get_fonts(full=True):
        if font_name in {item[3], item[4]}:
            return item[0]
    return None


def _line_height(lines: list[dict], font_size: float) -> float:
    if len(lines) > 1:
        baselines = [line["bbox"][1] for line in lines]
        gaps = [right - left for left, right in zip(baselines, baselines[1:]) if right > left]
        if gaps:
            return min(2.0, max(0.8, statistics.median(gaps) / font_size))
    return 1.2


def _alignment(lines: list[dict], rect: pymupdf.Rect) -> str:
    if not lines or rect.width <= 0:
        return "left"
    line_rect = pymupdf.Rect(lines[0]["bbox"])
    left_gap = line_rect.x0 - rect.x0
    right_gap = rect.x1 - line_rect.x1
    tolerance = max(1.0, rect.width * 0.05)
    if abs(left_gap - right_gap) <= tolerance and left_gap > tolerance:
        return "center"
    if right_gap <= tolerance and left_gap > tolerance:
        return "right"
    return "left"


def _integer_color(value: int | str | None) -> int:
    if isinstance(value, int):
        return value if 0 <= value <= 0xFFFFFF else 0
    if isinstance(value, str):
        try:
            return int(value[1:], 16) if value.startswith("#") else int(value, 10)
        except ValueError:
            return 0
    return 0


def _color_hex(value: int) -> str:
    return f"#{value & 0xFFFFFF:06x}"


def _escape_without_font_ligatures(text: str) -> str:
    # MuPDF's HTML shaper can emit incorrect ToUnicode mappings for FiraGO
    # ligatures. A span boundary around f/F prevents shaping without adding text.
    return "".join(
        "<br>" if character == "\n" else f"<span>{html.escape(character)}</span>" if character in "fF" else html.escape(character)
        for character in text
    )
