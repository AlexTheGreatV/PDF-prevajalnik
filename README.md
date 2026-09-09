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
- No translation workflow until the PDF pipeline is verified
- Uploaded PDFs and rendered previews are processed through request-scoped temporary directories and cleaned after each request
- API rejects non-PDF uploads and files above the configured size limit
