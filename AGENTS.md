# AGENTS.md

## Project Mission

Build an internal document translation application for JUB/KOOP, starting with a reliable local PDF processing pipeline. Keep the architecture ready for a later SaaS path, but do not build SaaS-only features before the internal workflow is proven.

## Current Milestone

Compare local OPUS-MT and HPLT English-to-Slovenian CTranslate2 profiles without changing the default model.

Allowed:

- Provider-independent translation logic
- Local OPUS-MT and HPLT/CTranslate2 inference and diagnostics
- Local FastAPI translation endpoints

Guardrails:

- Do not call external translation APIs without explicit user approval.
- Never download models during app startup, tests, or builds.
- Download or prepare a model only through an explicit user-run profile command pinned to an official revision.
- Keep models, test-document translations, and temporary results out of Git.
- Preserve `native_text`, OCR results, and the source document model.
- Do not hardcode translations or silently repair damaged placeholders.
- Do not add payments, public registration, or remote document uploads.

## Architecture

- `apps/web`: Next.js + TypeScript frontend
- `apps/api`: FastAPI backend and PDF processing
- PostgreSQL: durable application data
- Redis: job queue infrastructure
- Docker Compose: local orchestration

## Engineering Rules

- Keep document processing local.
- Treat PDF analysis as a testable service before exposing it through API routes.
- Prefer typed request/response models.
- Use PyMuPDF for PDF structure, text, image, and coordinate extraction.
- Use Poppler for page rendering verification.
- Add tests for digital, scanned, and mixed PDFs.
- Keep frontend thin until the backend pipeline is verified.

## Commands

```powershell
python -m pip install -r apps/api/requirements-dev.txt
pytest apps/api/tests
```
