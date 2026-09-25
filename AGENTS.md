# AGENTS.md

## Project Mission

Build an internal document translation application for JUB/KOOP, starting with a reliable local PDF processing pipeline. Keep the architecture ready for a later SaaS path, but do not build SaaS-only features before the internal workflow is proven.

## Current Milestone

Allow reviewed native text and explicitly accepted, tightly bounded OCR text on demonstrably uniform backgrounds to be exported without changing source images, table geometry, borders, fills, or standalone technical values.

Allowed:

- Provider-independent translation logic
- Local OPUS-MT and HPLT/CTranslate2 inference and diagnostics
- Local FastAPI translation endpoints
- User-controlled exact, preferred, do-not-translate, and explicitly locked glossary entries
- A provider-independent persistent local glossary backed by SQLite, with explicit approval, revision history, and import preview
- Local streaming preview and explicit import of user-supplied CSV or supported TBX Basic terminology candidates into the persistent glossary
- A dedicated local adapter for user-supplied official IATE pipe-delimited CSV exports, with exact-header detection, concept grouping, bounded diagnostics, and explicit candidate selection
- A separate persistent local translation memory for manually reviewed complete segments or deterministic sentence chunks, with conservative exact matching and explicit approval
- Local diagnostic and replacement-preview PDF export into a new temporary file
- Local FiraGO typography resolution with regular, bold, italic, and bold-italic faces
- Local background patching over demonstrably uniform image regions without modifying image XObjects
- Cell-level export for reliable digital tables with source matching, fit preflight, and partial-export diagnostics
- Selective export of explicitly accepted OCR regions after separate translation review, immutable PDF identity checks, and uniform-background preflight
- Versioned provider-independent Slovenian translation rules with explicit automation limits, source hashes, rule provenance, and manual quality checks
- OCR review revisions are held for 30 minutes in one API process. They are intentionally lost on restart, are not shared between workers, and a missing record must block export rather than trust client data.

Guardrails:

- Do not call external translation APIs without explicit user approval.
- Never download models during app startup, tests, or builds.
- Download or prepare a model only through an explicit user-run profile command pinned to an official revision.
- Keep models, test-document translations, and temporary results out of Git.
- Preserve `native_text`, OCR results, and the source document model.
- Do not hardcode translations or silently repair damaged placeholders.
- Never auto-review glossary or model translations; a successful placeholder retry must require manual review.
- Only approved persistent glossary entries may affect newly prepared translations. Preserve the glossary revision and active-set hash used by each translation job; later edits must not silently rewrite existing work.
- Keep the local glossary database, its WAL/SHM files, imports, exports, and backups out of Git. Do not store document paragraphs in glossary records.
- Never download terminology during startup, tests, builds, or translation. External terminology imports must be user-supplied, processed locally, SHA-256 bound to a server preview, and inserted only as `proposed` records in one transaction.
- Keep terminology previews process-local, expiring, bounded by count and memory size, single-use after confirmation, and authoritative for parsed candidates and provenance. Recheck current glossary state inside the write transaction.
- Reject XML DTD and ENTITY declarations. Do not silently overwrite existing glossary entries, guess missing source metadata, or let unapproved imported candidates affect translation.
- Never form Cartesian products from IATE terms. Group only by `E_ID`, reject ambiguous, inactive, unknown-status, or cross-concept-conflicting concepts, and retain official provenance separately from local review status.
- Keep translation-memory databases, WAL/SHM files, exports, and backups out of Git. Only an explicit user action may propose a manually reviewed correction, and only approved exact matches may affect a new job.
- Never use fuzzy or semantic translation-memory matching in milestone 8D1. Case, punctuation, numbers, units, and sentence boundaries remain significant; TM output must pass technical-integrity checks and always remain unreviewed.
- Never perform translation-memory substring matching. Preserve per-chunk provenance and mark a segment as mixed when approved TM and local-model chunks are assembled together.
- Do not prepare a translation job until persistent-glossary and translation-memory loading has settled successfully or the user has explicitly chosen to continue without the unavailable resource.
- Never claim that SSKJ, eSSKJ, SIST, IATE, standards, regulations, or another external terminology source was checked unless a source record was explicitly supplied and retained.
- Keep translation diagnostics in the review UI and JSON metadata; never insert comments, warnings, or diagnostic text into the final translated document.
- Never overwrite a source PDF. Export only reviewed, identity-matched, geometry-checked digital text blocks after fit preflight.
- Preserve images and vector graphics, and skip ambiguous, overlapping, unaccepted or stale OCR, rotated, unsupported-table, or overflowing regions with a structured reason.
- OCR export must remain inside the accepted bbox, must not cover icons, lines, neighboring text, cells, photographs, gradients, or textures, and must never reconstruct an image or rasterize a page.
- Never resize table rows or columns, reconstruct ambiguous merged cells, alter borders/fills, or translate standalone technical values automatically.
- Keep Droid Sans Fallback as a final glyph fallback, never download fonts at runtime, and never reuse an embedded source font without clear embedding rights and complete glyph coverage.
- Never patch photographs, gradients, textures, icons, lines, image-only OCR text, or uncertain regions. Do not add inpainting or full-page rasterization.
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
