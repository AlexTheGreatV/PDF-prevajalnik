# JUB/KOOP Document Translation Pipeline

Internal web application for document translation workflows. The first milestone is a verified local PDF pipeline: inspect PDFs, classify them, extract layout/text blocks, and render pages for visual QA.

No external AI services are called in this milestone. Documents stay local.

## Stack

- Next.js + TypeScript for the web UI
- Python FastAPI for backend and PDF processing
- PostgreSQL for application data
- Redis for job queue infrastructure
- Docker Compose for local services
- PyMuPDF for PDF inspection and extraction
- Poppler for PDF page rendering and verification
- pytest for backend tests

## Current Capabilities

- Analyze an uploaded PDF
- Report page count
- Classify PDF as `digital`, `scanned`, `mixed`, or `empty`
- Count extracted text characters
- Report page dimensions
- Extract text blocks with coordinates
- Render pages to PNG images through Poppler
- Run backend tests using generated local fixture PDFs
- Use a Slovenian web UI for drag-and-drop PDF analysis and page previews
- Prepare and review structured translation segments without changing source text
- Optionally translate English segments to Slovenian with a local OPUS-MT/CTranslate2 model
- Preview reviewed translations in reliable digital table cells while preserving the table grid and technical values

## Local Backend Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r apps/api/requirements-dev.txt
python -m pytest
```

Run tests from the repository root. On Windows, a project-local temporary directory
can avoid permissions problems in the system temporary folder:

```powershell
python -m pytest --basetemp C:\Projects\PDF-prevajalnik\tmp\pytest -p no:cacheprovider
```

Page previews request 110 DPI. The render API defaults to 150 DPI and accepts
72-400 DPI with an optional one-based `page_number`. Browser OCR requests only
the selected page at 300 DPI; manual crops can be inspected before recognition.

Run the API directly:

```powershell
$env:PYTHONPATH='apps/api'
python -m uvicorn app.main:app --app-dir apps/api --reload --port 8000
```

### Optional local translation

The base PDF API does not require the translation runtime. Install the optional runtime in the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r apps/api/requirements-translation.txt
```

Preparing a model additionally requires the heavier conversion dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r apps/api/requirements-translation-prep.txt
.\.venv\Scripts\python.exe apps/api/scripts/prepare_local_translation_model.py --profile hplt-en-sl-v2 --download
```

The preparation command supports the pinned `opus-en-sla` and `hplt-en-sl-v2` profiles. It downloads an official revision only when explicitly invoked, converts it to CTranslate2 INT8, and writes source and output hashes to a local manifest. It never runs during API startup, tests, or frontend builds. Models are stored under ignored `local-models/`; use `--source-dir` instead of `--download` when an approved snapshot was transferred separately. Existing output is not replaced unless `--force` is explicitly passed.

HPLT is the default local English-to-Slovenian profile. OPUS-MT remains available as an explicit alternative:

```powershell
.\.venv\Scripts\python.exe apps/api/scripts/prepare_local_translation_model.py --profile opus-en-sla --download
```

HPLT publishes a shared SentencePiece model and a score-based TSV vocabulary. The preparation script verifies both official files, derives the ID-based YAML vocabulary required by CTranslate2's Marian converter, and records that derivation in the model manifest.

Runtime configuration:

```text
LOCAL_TRANSLATION_PROFILE=hplt-en-sl-v2
LOCAL_TRANSLATION_MODEL_PATH=local-models/hplt-en-sl-v2-ct2-int8
LOCAL_TRANSLATION_DEVICE=cpu
LOCAL_TRANSLATION_COMPUTE_TYPE=int8
```

To use the OPUS-MT alternative, set `LOCAL_TRANSLATION_PROFILE=opus-en-sla` together with `LOCAL_TRANSLATION_MODEL_PATH=local-models/opus-mt-en-sla-ct2-int8`. The application never downloads either model during startup, tests, or builds.

Translation jobs include a configurable technical glossary. Built-in entries use whole-segment matching and produce unreviewed translation candidates that still require manual confirmation; inline locking is opt-in and always marks the result for review. Schema version 3 introduced the glossary snapshot and model-inference chunks.

Translation job schema version 4 binds the job to the analyzed source through its SHA-256, page count, page geometry, rotation, and document-model version. Versions 1 through 3 remain importable, but their missing identity and layout fields are populated only after every immutable source segment has been checked against the current analysis.

Paragraphs and list items are translated sentence by sentence while remaining one reviewable segment. The local runtime counts actual SentencePiece tokens, uses a dynamic decoding limit between 64 and 768 tokens, rejects truncated output, and permits one safe punctuation-based split when a single chunk exceeds a model limit.

Local translation endpoints:

- `GET /api/translation/providers/local/status`
- `POST /api/translation/providers/local/translate`

Translated PDF preview endpoints:

- `POST /api/pdf/export-translated/plan` validates identity, source text, coordinates, conflicts, glyphs, and fit without changing a PDF.
- `POST /api/pdf/export-translated` returns a new `<original-name>-sl-preview.pdf` in `diagnostic` or `replacement_preview` mode.

Only manually reviewed, identity-matched digital text is eligible. Reliable structured tables are planned cell by cell: source text and geometry must match, content stays inside the original cell, standalone technical values remain unchanged, and one overflowing or ambiguous cell produces an explicit partial export instead of rejecting safe neighboring cells. Graphic tables, image-only cells, rotated pages, overlapping replacements, source mismatches, and overflow are skipped with structured reasons. Replacement preflight completes before text redaction; the source PDF is never modified, temporary files are removed after each request, and images, fills, borders, and other vector graphics are preserved. Preview fonts are loaded only from pinned local packages, without downloads or system-font lookup.

Typography preflight uses the FiraGO OpenType 1.001 family included in the pinned `pymupdf-fonts` 1.0.5 package, with regular, bold, italic, and bold-italic faces; Droid Sans Fallback is retained only for glyph fallback. The resolver recalculates the dominant source span style from the identity-checked PDF, preserves its decimal PyMuPDF RGB color, approximate alignment, and line height, and reports mixed styles. It first tries the original box, then a collision-checked expansion within the same page half, and finally bounded scaling: at most 15% for headings and 25% for body text, never below 6 pt. Embedded source-font subsets are not reused because complete glyph coverage and embedding rights cannot be established reliably.

Native text duplicated in an overlapping image layer is eligible only when a 300-DPI local analysis proves a simple background. Central thresholds require at least 128 background samples, mean RGB variance at most 18, edge density at most 0.02, texture score at most 0.04, and side-to-side color distance at most 8; values within 15% of a limit are treated as uncertain and skipped. The exporter keeps the image XObject unchanged, removes only the native text glyphs, paints minimal line-level patches in the measured color, and reruns the background check for an expanded fit box. Photographs, gradients, textures, icons, lines, image-only OCR text, and uncertain regions remain skipped. General inpainting is reserved for a separate future experimental mode that would require manual confirmation.

Accepted OCR export sources are anchored in a process-local API registry before translation review. Registration and acceptance are separate requests, records expire after 30 minutes, and a changed source creates a new revision that makes the previous one stale. This prototype registry is neither persistent nor shared between API workers: restart, expiry, or routing an export to another worker removes the proof of application state and safely blocks OCR export. It records explicit application acceptance only; it does not prove OCR correctness or that a human performed the review.

Run the prototype API with exactly one worker/process. After expiry or restart, use the OCR review button to register and accept the source again, then manually review the retained translation again. Re-registration changes the revision and invalidates the old translation approval without discarding the edited translation.

The prototype supports English to Slovenian only. Documents and translations are not sent to an external translation service, and translated segments always require manual review.

## Local Frontend Setup

This project uses pnpm for the Next.js app:

```powershell
cd apps/web
pnpm install
pnpm run typecheck
pnpm run lint
pnpm run test
pnpm run build
pnpm run dev
```

The pnpm build-script allowlist is intentionally narrow in `apps/web/pnpm-workspace.yaml`.
Allowed packages:

- `sharp`: optional Next.js production image optimization dependency; its install script checks or builds platform-specific libvips/native bindings.
- `esbuild`: platform binary check used by frontend tooling.
- `unrs-resolver`: platform resolver binding used by ESLint import resolution.

## Docker Setup

Docker is expected for the full local stack:

```powershell
docker compose up --build
```

Services:

- Web: http://localhost:3000
- API: http://localhost:8000
- PostgreSQL: localhost:5432
- Redis: localhost:6379

## API Endpoints

- `GET /health`
- `POST /api/pdf/analyze`
- `POST /api/pdf/render`
- `GET /api/translation/providers/local/status`
- `POST /api/translation/providers/local/translate`

Both PDF endpoints accept multipart form data with a `file` field. Render also accepts an optional `dpi` field.

## Project Layout

```text
apps/
  api/
    app/
      api/
      core/
      services/
    tests/
  web/
    app/
    components/
infra/
  poppler/
```

## Guardrails

- No payments
- No public registration
- No external AI calls
- No document uploads to third-party services
- No external translation APIs without explicit approval
- No automatic model downloads during startup, tests, or builds
- Local models and generated translation results stay outside Git
- Uploaded PDFs and rendered previews are processed through request-scoped temporary directories and cleaned after each request
- API rejects non-PDF uploads and files above the configured size limit
