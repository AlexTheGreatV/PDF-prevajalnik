from __future__ import annotations

import math
from dataclasses import dataclass

import pymupdf
from PIL import Image, ImageFilter

from app.models import BBox
from app.pdf_export_models import ImageOverlapKind, PdfBackgroundMetrics


@dataclass(frozen=True)
class ImageOverlapThresholds:
    dpi: int = 300
    patch_padding_pt: float = 1.0
    sample_ring_pt: float = 2.5
    minimum_samples: int = 128
    maximum_color_variance: float = 18.0
    maximum_edge_density: float = 0.02
    maximum_texture_score: float = 0.04
    maximum_side_color_delta: float = 8.0
    texture_color_distance: float = 12.0
    ambiguity_margin: float = 0.15


DEFAULT_THRESHOLDS = ImageOverlapThresholds()
BULLET_MARKERS = {"-", "•", "–", "—", "*"}


@dataclass(frozen=True)
class ImageOverlapAssessment:
    kind: ImageOverlapKind
    overlapping_image_xrefs: list[int]
    overlap_ratio: float
    sampled_background_color: str | None
    metrics: PdfBackgroundMetrics | None
    confidence: float
    safe_for_background_patch: bool
    patch_rects: list[BBox]
    redaction_rects: list[BBox]
    warnings: list[str]
    skip_reason: str | None


class PdfImageOverlapAnalyzer:
    def __init__(self, thresholds: ImageOverlapThresholds = DEFAULT_THRESHOLDS) -> None:
        self.thresholds = thresholds

    def analyze(
        self,
        page: pymupdf.Page,
        rect: pymupdf.Rect,
        *,
        native_text: bool = True,
        preserve_list_marker: bool = False,
        patch_rects: list[pymupdf.Rect] | None = None,
        patch_clip: pymupdf.Rect | None = None,
        ignore_enclosing_drawings: bool = False,
        allow_opaque_vector_cover: bool = False,
    ) -> ImageOverlapAssessment:
        images = _overlapping_images(page, rect)
        if not images:
            return ImageOverlapAssessment("none", [], 0.0, None, None, 1.0, False, [], [], [], None)
        if allow_opaque_vector_cover and _opaque_vector_fill_covers_images(page, rect):
            return ImageOverlapAssessment(
                "none", [], 0.0, None, None, 1.0, False, [], [],
                ["overlapping_image_hidden_by_opaque_vector_fill"], None,
            )
        xrefs = sorted({int(info["xref"]) for info in images if info.get("xref")})
        overlap_ratio = min(1.0, sum((_rect(info["bbox"]) & rect).get_area() for info in images) / max(rect.get_area(), 0.001))
        if not native_text:
            return ImageOverlapAssessment(
                "image_only_text", xrefs, round(overlap_ratio, 4), None, None, 1.0, False, [],
                [], ["image_only_text_requires_manual_ocr_workflow"], "image_only_text",
            )

        source_patches, glyph_rects = _text_patch_rects(page, rect, preserve_list_marker)
        requested_patches = patch_rects or source_patches
        clipped_patches = _clip_patches_to_images(
            requested_patches,
            images,
            self.thresholds.patch_padding_pt,
            patch_clip,
        )
        if not clipped_patches:
            return self._uncertain(xrefs, overlap_ratio, "no_patchable_text_glyphs")
        if _intersects_other_text(page, rect, requested_patches):
            return self._complex(xrefs, overlap_ratio, "neighboring_text_in_patch_area")
        if _intersects_drawing(page, clipped_patches, ignore_enclosing_drawings):
            return self._complex(xrefs, overlap_ratio, "vector_element_in_patch_area")

        try:
            metric_values, color = self._measure(page, rect, requested_patches, glyph_rects)
        except (RuntimeError, ValueError, OSError):
            return self._uncertain(xrefs, overlap_ratio, "background_render_failed")
        metrics = PdfBackgroundMetrics(**metric_values)
        ratios = [
            metrics.color_variance / self.thresholds.maximum_color_variance,
            metrics.edge_density / self.thresholds.maximum_edge_density,
            metrics.texture_score / self.thresholds.maximum_texture_score,
            metrics.side_color_delta / self.thresholds.maximum_side_color_delta,
        ]
        enough_samples = metrics.sample_count >= self.thresholds.minimum_samples
        within_limits = enough_samples and all(ratio <= 1.0 for ratio in ratios)
        near_threshold = any(ratio >= 1.0 - self.thresholds.ambiguity_margin for ratio in ratios)
        metrics.near_threshold = near_threshold
        confidence = max(0.0, min(1.0, 1.0 - max(ratios) * 0.5)) if enough_samples else 0.0
        if not enough_samples:
            return ImageOverlapAssessment(
                "uncertain", xrefs, round(overlap_ratio, 4), color, metrics, confidence, False,
                [_bbox(item) for item in clipped_patches], [_bbox(item) for item in source_patches],
                ["insufficient_background_samples"], "insufficient_samples",
            )
        if not within_limits:
            return ImageOverlapAssessment(
                "complex_background", xrefs, round(overlap_ratio, 4), color, metrics, confidence, False,
                [_bbox(item) for item in clipped_patches], [_bbox(item) for item in source_patches],
                ["background_complexity_exceeds_limit"], "complex_background",
            )
        if near_threshold:
            return ImageOverlapAssessment(
                "uncertain", xrefs, round(overlap_ratio, 4), color, metrics, confidence, False,
                [_bbox(item) for item in clipped_patches], [_bbox(item) for item in source_patches],
                ["background_metrics_near_threshold"], "near_threshold",
            )
        return ImageOverlapAssessment(
            "simple_background", xrefs, round(overlap_ratio, 4), color, metrics, round(confidence, 4), True,
            [_bbox(item) for item in clipped_patches], [_bbox(item) for item in source_patches],
            ["image_layer_contains_visible_text"], None,
        )

    def _measure(
        self,
        page: pymupdf.Page,
        rect: pymupdf.Rect,
        patches: list[pymupdf.Rect],
        glyph_rects: list[pymupdf.Rect],
    ) -> tuple[dict[str, float | int | bool], str]:
        margin = self.thresholds.sample_ring_pt
        clip = pymupdf.Rect(rect.x0 - margin, rect.y0 - margin, rect.x1 + margin, rect.y1 + margin) & page.rect
        pixmap = page.get_pixmap(clip=clip, dpi=self.thresholds.dpi, alpha=False)
        if pixmap.width < 3 or pixmap.height < 3 or pixmap.n < 3:
            raise RuntimeError("invalid background render")
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        scale_x = image.width / clip.width
        scale_y = image.height / clip.height
        sample_points, side_points = _sample_points(
            image.size, clip, patches, glyph_rects, scale_x, scale_y, self.thresholds.sample_ring_pt
        )
        if not sample_points:
            raise RuntimeError("no background samples")
        pixels = image.load()
        samples = [pixels[x, y] for x, y in sample_points]
        means = [sum(pixel[index] for pixel in samples) / len(samples) for index in range(3)]
        variances = [sum((pixel[index] - means[index]) ** 2 for pixel in samples) / len(samples) for index in range(3)]
        edge_image = image.convert("L").filter(ImageFilter.FIND_EDGES)
        edge_pixels = edge_image.load()
        edge_density = sum(edge_pixels[x, y] >= 32 for x, y in sample_points) / len(sample_points)
        texture_score = sum(_distance(pixel, means) > self.thresholds.texture_color_distance for pixel in samples) / len(samples)
        side_means = []
        for points in side_points:
            if points:
                side_means.append([sum(pixels[x, y][index] for x, y in points) / len(points) for index in range(3)])
        side_delta = max((_distance(left, right) for index, left in enumerate(side_means) for right in side_means[index + 1 :]), default=0.0)
        color = "#" + "".join(f"{round(value):02x}" for value in means)
        return {
            "dpi": self.thresholds.dpi,
            "sample_count": len(samples),
            "color_variance": round(sum(variances) / 3, 4),
            "edge_density": round(edge_density, 6),
            "texture_score": round(texture_score, 6),
            "side_color_delta": round(side_delta, 4),
            "near_threshold": False,
        }, color

    def _uncertain(self, xrefs: list[int], overlap_ratio: float, warning: str) -> ImageOverlapAssessment:
        return ImageOverlapAssessment("uncertain", xrefs, round(overlap_ratio, 4), None, None, 0.0, False, [], [], [warning], warning)

    def _complex(self, xrefs: list[int], overlap_ratio: float, warning: str) -> ImageOverlapAssessment:
        return ImageOverlapAssessment("complex_background", xrefs, round(overlap_ratio, 4), None, None, 0.0, False, [], [], [warning], warning)


def _overlapping_images(page: pymupdf.Page, rect: pymupdf.Rect) -> list[dict]:
    return [info for info in page.get_image_info(xrefs=True) if not (_rect(info["bbox"]) & rect).is_empty]


def _text_patch_rects(page: pymupdf.Page, rect: pymupdf.Rect, preserve_list_marker: bool) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    lines: list[pymupdf.Rect] = []
    glyphs: list[pymupdf.Rect] = []
    raw = page.get_text("rawdict", clip=rect, sort=True)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = [char for span in line.get("spans", []) for char in span.get("chars", []) if char.get("c", "").strip()]
            if preserve_list_marker:
                while chars and chars[0].get("c") in BULLET_MARKERS:
                    chars.pop(0)
            if not chars:
                continue
            char_rects = [_rect(char["bbox"]) for char in chars]
            glyphs.extend(char_rects)
            line_rect = pymupdf.Rect(char_rects[0])
            for char_rect in char_rects[1:]:
                line_rect |= char_rect
            lines.append(line_rect)
    return lines or [rect], glyphs


def _clip_patches_to_images(
    patches: list[pymupdf.Rect],
    images: list[dict],
    padding: float,
    patch_clip: pymupdf.Rect | None = None,
) -> list[pymupdf.Rect]:
    result: list[pymupdf.Rect] = []
    for patch in patches:
        padded = pymupdf.Rect(patch.x0 - padding, patch.y0 - padding, patch.x1 + padding, patch.y1 + padding)
        if patch_clip is not None:
            padded &= patch_clip
        for image in images:
            clipped = padded & _rect(image["bbox"])
            if not clipped.is_empty:
                result.append(clipped)
    return result


def _intersects_other_text(page: pymupdf.Page, source_rect: pymupdf.Rect, patches: list[pymupdf.Rect]) -> bool:
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if not span.get("text", "").strip():
                    continue
                span_rect = _rect(span["bbox"])
                if not (span_rect & source_rect).is_empty:
                    continue
                if any(not (span_rect & patch).is_empty for patch in patches):
                    return True
    return False


def _intersects_drawing(page: pymupdf.Page, patches: list[pymupdf.Rect], ignore_enclosing: bool = False) -> bool:
    for drawing in page.get_drawings():
        drawing_rect = _rect(drawing["rect"])
        for patch in patches:
            if (drawing_rect & patch).is_empty:
                continue
            if ignore_enclosing and drawing_rect.contains(patch):
                continue
            return True
    return False


def _opaque_vector_fill_covers_images(page: pymupdf.Page, rect: pymupdf.Rect) -> bool:
    image_sequences = [
        index
        for index, (kind, bbox, *_) in enumerate(page.get_bboxlog())
        if kind == "fill-image" and not (_rect(bbox) & rect).is_empty
    ]
    if not image_sequences:
        return False
    latest_image = max(image_sequences)
    drawings = page.get_drawings()
    for drawing in drawings:
        items = drawing.get("items") or []
        if (
            int(drawing.get("seqno", -1)) > latest_image
            and drawing.get("fill") is not None
            and float(drawing.get("fill_opacity", 0.0)) >= 0.999
            and drawing.get("color") is None
            and len(items) == 1
            and items[0][0] == "re"
            and _rect(items[0][1]).contains(rect)
            and not any(
                int(later.get("seqno", -1)) > int(drawing.get("seqno", -1))
                and _drawing_items_intersect(later, rect)
                for later in drawings
            )
        ):
            return True
    return False


def _drawing_items_intersect(drawing: dict, rect: pymupdf.Rect) -> bool:
    width = max(0.1, float(drawing.get("width") or 0.1))
    for item in drawing.get("items") or []:
        if item[0] == "re":
            item_rect = _rect(item[1])
            if drawing.get("fill") is None and drawing.get("color") is not None:
                half = width / 2
                border_bands = (
                    pymupdf.Rect(item_rect.x0 - half, item_rect.y0 - half, item_rect.x1 + half, item_rect.y0 + half),
                    pymupdf.Rect(item_rect.x0 - half, item_rect.y1 - half, item_rect.x1 + half, item_rect.y1 + half),
                    pymupdf.Rect(item_rect.x0 - half, item_rect.y0 + half, item_rect.x0 + half, item_rect.y1 - half),
                    pymupdf.Rect(item_rect.x1 - half, item_rect.y0 + half, item_rect.x1 + half, item_rect.y1 - half),
                )
                if any(not (band & rect).is_empty for band in border_bands):
                    return True
                continue
        elif item[0] == "l":
            left, right = item[1], item[2]
            item_rect = pymupdf.Rect(
                min(left.x, right.x) - width / 2,
                min(left.y, right.y) - width / 2,
                max(left.x, right.x) + width / 2,
                max(left.y, right.y) + width / 2,
            )
        else:
            return not (_rect(drawing["rect"]) & rect).is_empty
        if not (item_rect & rect).is_empty:
            return True
    return False


def _sample_points(
    image_size: tuple[int, int],
    clip: pymupdf.Rect,
    patches: list[pymupdf.Rect],
    glyphs: list[pymupdf.Rect],
    scale_x: float,
    scale_y: float,
    ring_pt: float,
) -> tuple[list[tuple[int, int]], list[list[tuple[int, int]]]]:
    width, height = image_size
    glyph_boxes = [_pixel_box(item, clip, scale_x, scale_y, width, height, 1) for item in glyphs]
    all_points: set[tuple[int, int]] = set()
    sides: list[list[tuple[int, int]]] = [[], [], [], []]
    for patch in patches:
        inner = _pixel_box(patch, clip, scale_x, scale_y, width, height, 0)
        outer_rect = pymupdf.Rect(patch.x0 - ring_pt, patch.y0 - ring_pt, patch.x1 + ring_pt, patch.y1 + ring_pt) & clip
        outer = _pixel_box(outer_rect, clip, scale_x, scale_y, width, height, 0)
        for y in range(outer[1], outer[3]):
            for x in range(outer[0], outer[2]):
                inside_inner = inner[0] <= x < inner[2] and inner[1] <= y < inner[3]
                inside_glyph = any(box[0] <= x < box[2] and box[1] <= y < box[3] for box in glyph_boxes)
                if inside_glyph:
                    continue
                if not inside_inner:
                    all_points.add((x, y))
                    side = 0 if y < inner[1] else 1 if y >= inner[3] else 2 if x < inner[0] else 3
                    sides[side].append((x, y))
                elif x in {inner[0], inner[2] - 1} or y in {inner[1], inner[3] - 1}:
                    all_points.add((x, y))
    return sorted(all_points), sides


def _pixel_box(rect: pymupdf.Rect, clip: pymupdf.Rect, sx: float, sy: float, width: int, height: int, padding: int) -> tuple[int, int, int, int]:
    return (
        max(1, math.floor((rect.x0 - clip.x0) * sx) - padding),
        max(1, math.floor((rect.y0 - clip.y0) * sy) - padding),
        min(width - 1, math.ceil((rect.x1 - clip.x0) * sx) + padding),
        min(height - 1, math.ceil((rect.y1 - clip.y0) * sy) + padding),
    )


def _distance(left, right) -> float:
    return math.sqrt(sum((float(left[index]) - float(right[index])) ** 2 for index in range(3)))


def _rect(value) -> pymupdf.Rect:
    return pymupdf.Rect(value)


def _bbox(rect: pymupdf.Rect) -> BBox:
    return BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)
