import base64
import subprocess
import uuid
from pathlib import Path

from PIL import Image
import pymupdf

from app.models import RenderedPage, RenderResult


def render_pdf_pages(
    path: Path,
    output_root: Path,
    filename: str | None = None,
    dpi: int = 150,
    pdftoppm_bin: str = "pdftoppm",
    page_number: int | None = None,
) -> RenderResult:
    if not 72 <= dpi <= 400:
        raise ValueError("DPI must be between 72 and 400.")
    with pymupdf.open(path) as document:
        if page_number is not None and not 1 <= page_number <= document.page_count:
            raise ValueError("Requested page does not exist.")
        selected_pages = [document[page_number - 1]] if page_number else list(document)
        if any(page.rect.width * page.rect.height * (dpi / 72) ** 2 > 40_000_000 for page in selected_pages):
            raise ValueError("Rendered page exceeds the pixel limit.")
    render_id = uuid.uuid4().hex
    output_dir = output_root / render_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_prefix = output_dir / "page"

    subprocess.run(
        [
            pdftoppm_bin,
            "-png",
            "-r",
            str(dpi),
            *(["-f", str(page_number), "-l", str(page_number)] if page_number else []),
            str(path),
            str(output_prefix),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    rendered_pages: list[RenderedPage] = []
    for page_path in sorted(output_dir.glob("page-*.png")):
        page_number = _page_number_from_name(page_path)
        width_px: int | None = None
        height_px: int | None = None
        with Image.open(page_path) as image:
            width_px, height_px = image.size

        rendered_pages.append(
            RenderedPage(
                page_number=page_number,
                path=str(page_path),
                width_px=width_px,
                height_px=height_px,
            )
        )

    return RenderResult(filename=filename or path.name, dpi=dpi, pages=rendered_pages)


def render_pdf_pages_as_data_urls(
    path: Path,
    output_root: Path,
    filename: str | None = None,
    dpi: int = 150,
    pdftoppm_bin: str = "pdftoppm",
    page_number: int | None = None,
) -> RenderResult:
    result = render_pdf_pages(
        path=path,
        output_root=output_root,
        filename=filename,
        dpi=dpi,
        pdftoppm_bin=pdftoppm_bin,
        page_number=page_number,
    )

    embedded_pages: list[RenderedPage] = []
    for page in result.pages:
        page_path = Path(page.path)
        encoded = base64.b64encode(page_path.read_bytes()).decode("ascii")
        embedded_pages.append(
            RenderedPage(
                page_number=page.page_number,
                path=f"data:image/png;base64,{encoded}",
                width_px=page.width_px,
                height_px=page.height_px,
            )
        )

    return RenderResult(filename=result.filename, dpi=result.dpi, pages=embedded_pages)


def _page_number_from_name(path: Path) -> int:
    suffix = path.stem.split("-")[-1]
    return int(suffix)
