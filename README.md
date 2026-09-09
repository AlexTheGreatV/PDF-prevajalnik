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
.\.venv\Scripts\python.exe apps/api/scripts/prepare_local_translation_model.py --profile opus-en-sla --download
```

The preparation command supports the pinned `opus-en-sla` and `hplt-en-sl-v2` profiles. It downloads an official revision only when explicitly invoked, converts it to CTranslate2 INT8, and writes source and output hashes to a local manifest. It never runs during API startup, tests, or frontend builds. Models are stored under ignored `local-models/`; use `--source-dir` instead of `--download` when an approved snapshot was transferred separately. Existing output is not replaced unless `--force` is explicitly passed.

The direct HPLT English-to-Slovenian comparison model can be prepared separately without changing the default OPUS model:

```powershell
.\.venv\Scripts\python.exe apps/api/scripts/prepare_local_translation_model.py --profile hplt-en-sl-v2 --download
```

HPLT publishes a shared SentencePiece model and a score-based TSV vocabulary. The preparation script verifies both official files, derives the ID-based YAML vocabulary required by CTranslate2's Marian converter, and records that derivation in the model manifest.

Runtime configuration:

```text
LOCAL_TRANSLATION_MODEL_PATH=local-models/opus-mt-en-sla-ct2-int8
LOCAL_TRANSLATION_DEVICE=cpu
LOCAL_TRANSLATION_COMPUTE_TYPE=int8
```

Local translation endpoints:

- `GET /api/translation/providers/local/status`
- `POST /api/translation/providers/local/translate`

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
