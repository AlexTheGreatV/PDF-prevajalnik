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

### Persistent Local Acceptance Profile

The named `fresh-user-test` profile uses existing, ignored glossary/TM databases under
`local-data/profiles/fresh-user-test/`, not temporary review databases. It is a
separate test collection; edits and usage are not merged into production.
Before changing its data, back up the glossary, TM and durable OCR evidence together.
With the API stopped, run:

```powershell
./.venv/Scripts/python.exe apps/api/scripts/local_test_data.py --backup
./apps/api/scripts/start_local_test.ps1 -ConfigurationOnly
```

The backup command uses SQLite's backup API for each database and records paths,
schema, integrity/FK checks and hashes in an ignored profile backup manifest.
Each database snapshot is consistent, including WAL contents; stopping the API
also prevents changes between the three snapshots. A missing OCR database is
reported without creating one or importing evidence from another profile.
Never copy an active WAL database as an ordinary file.

After building the frontend, with ports 8000 and 3001 free, run from the root:

```powershell
./apps/api/scripts/start_local_test.ps1
```

The launcher uses the project `.venv`, the existing local HPLT CPU/INT8 model,
and `next start`. It refuses missing databases/models and occupied ports; it
does not download, create empty databases, stop other processes or overwrite
jobs. `-ApiOnly` starts only the API when an existing frontend is retained.
Advanced mode shows the actual database paths and profile reported by the API.
The launcher pins all three paths, including `OCR_DATABASE_PATH`, to this profile
independently of the working directory or inherited OCR environment setting.
Retain the same profile and paths after restart. Never move OCR acceptances from
a review profile into `fresh-user-test`; a legacy job without evidence in the
selected profile needs explicit source review.

For a fresh English technical sheet, explicitly select English, Slovenian and
the `technical_datasheets` domain before preparing the translation. An empty
domain intentionally excludes domain-specific TM entries. Imported jobs keep
their own snapshots; editing a job does not teach the persistent glossary/TM.

To return to production, stop these two explicitly identified test processes
and start the usual local API with `LOCAL_DATA_PROFILE=production` and absolute
`GLOSSARY_DATABASE_PATH` / `TRANSLATION_MEMORY_DATABASE_PATH` pointing at the
two databases directly under `local-data/`. Do not merge or replace databases.
Check Advanced mode before translating. Existing browser jobs are not updated
by changing the running API or its collection.

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

Translation jobs include a configurable technical glossary. Built-in entries use whole-segment matching; document-specific instructions have the highest priority, followed by approved persistent user entries and built-in terms. User glossary results remain translation candidates and are never marked as reviewed automatically. Inline locking is document-specific, opt-in, and always marks the result for review.

The persistent glossary uses Python's built-in SQLite driver and defaults to `local-data/glossary.sqlite3` (`GLOSSARY_DATABASE_PATH`). The database is created lazily, uses WAL mode, revision history, optimistic concurrency, and logical archive/restore operations. Only `approved` entries affect newly prepared translation jobs. Each job stores the approved-set hash and entry revisions it used, so a later glossary edit is shown as stale without changing old source text or translations.

The local translation memory is a separate SQLite store at `local-data/translation-memory.sqlite3` (`TRANSLATION_MEMORY_DATABASE_PATH`). It stores complete manually reviewed translation units, not terminology. A unit is either the complete `TranslationSegment` or a complete deterministic sentence `TranslationChunk` produced by the existing safe chunker; the application never searches arbitrary substrings inside a segment. Matching is deliberately exact: NFC normalization, newline normalization, and outer trimming are allowed, while case, punctuation, numbers, units, sentence content, and chunk boundaries remain significant. Selection uses an approved same-domain entry before an approved generic entry; conflicting equally specific targets are reported and are not selected. A valid hit is still `translated` and requires manual review. When a segment combines TM and local-model chunks, it is marked `mixed` and keeps provenance per chunk. The priority is document glossary, approved persistent glossary, approved exact translation-memory hit, built-in/profile rules, then the local model.

Translation-memory loading is explicit (`loading`, `ready`, `error`, or `explicitly_skipped`). A job cannot be prepared until the persistent glossary and TM snapshots are ready or the user explicitly continues without one of them. Full source and target sentences are stored locally only after the user proposes a reviewed manual correction; model output is never stored automatically. The TM stores no PDF or image data. `source_document_sha256` is traceability metadata, not a document copy.

Translation-memory JSON is export-only in milestone 8D1. The local database uses migrations, foreign keys, WAL, a busy timeout, optimistic revisions, and transactional snapshots. If it is unavailable, PDF analysis and `/health` remain available; preparing a job requires either a loaded deterministic snapshot or an explicit choice to continue without TM. The local prototype has no user accounts, cross-device synchronization, fuzzy search, or multi-host coordination.

`local-data/` is ignored by Git. Back up the SQLite database only while the API is stopped, or use SQLite's own backup mechanism so the main database and WAL state remain consistent. This milestone is a single-machine local prototype: concurrent requests are serialized through SQLite transactions, but there are no user accounts, permissions, cross-device synchronization, or PostgreSQL migration yet. JSON is export-only. CSV export preserves reversible spreadsheet-formula escaping; importing external terminology uses the separate SHA-256-bound preview flow described below. Exports do not recreate the immutable revision history, so moving data between computers transfers glossary entries rather than the complete audit trail.

Translation job schema version 8 carries glossary snapshots, the active-set hash, per-entry revision/provenance, and usage/conflict diagnostics. Older supported jobs migrate without falsely claiming a persistent glossary snapshot.

### External terminology candidate import

The glossary accepts only user-supplied UTF-8 CSV and a conservative TBX Basic subset. CSV requires explicit source/target column mapping and accepts a UTF-8 BOM and CRLF line endings. TBX accepts namespace-qualified or unqualified `tbx`/`martif` roots, `termEntry`, `langSet` with `xml:lang`, `tig` or `ntig`, `term`, and selected `descrip` metadata (`subjectField`, `definition`, `context`, `reliabilityCode`). `termNote` status values are diagnostic only. Exactly one distinct source and one distinct target term must be present in an entry; multiple terms are reported as ambiguous instead of being paired or ranked. Unknown elements are reported as ignored, and missing metadata or confidence is never inferred. DTD and ENTITY declarations are rejected before parsing. Files are streamed through a unique application temporary directory, limited to 5 MB and 1,000 records with at most 2,000 characters per field, hashed with SHA-256, and deleted after preview generation.

Preview is read-only and retained in one API process for 30 minutes. At most 16 previews and 8 MB of serialized preview data may be active. Confirmation references the server-held preview ID, file hash, and selected candidate IDs; the server-held record also binds the parsed candidates, column mapping result, languages, domain, source, and attribution. Selected records are inserted in one SQLite `BEGIN IMMEDIATE` transaction as `proposed`; current database state is checked again, newly appeared duplicates or conflicts are skipped and reported, and existing records are never overwritten. A token is removed after confirmation, expiry, or process restart and cannot be replayed. Only a later explicit per-entry approval can make a candidate available to new translation jobs. The original TBX/CSV file is not retained, no remote terminology service or CDN is contacted, and restart or multi-worker routing invalidates an outstanding preview. Supported metadata is external source, record ID, source-provided confidence, definition, context, note, source version/date, license/attribution, import hash, batch ID, import time, and local revision. Missing metadata is left empty rather than inferred.

Official IATE CSV exports use a separate, exact profile: the documented 12-column header and `|` delimiter must match exactly, rows are grouped only by `E_ID`, and only concepts with one unique EN and one unique SL term can become candidates. Duplicate rows are diagnosed and removed; missing-language, multi-term, inactive lifecycle, rejected or unknown evaluation, and cross-concept source conflicts are excluded without constructing term combinations. Final concept counts are mutually exclusive in this order: missing language, ambiguous multiple terms or metadata, rejected status or lifecycle, cross-concept conflict, safe candidate. The preview separately reports a raw overlapping multi-term count; cross-concept diagnostics distinguish excluded concepts from normalized-source conflict groups. Conflict normalization applies NFC and external-whitespace normalization but preserves case, punctuation, numbers, and technical symbols. IATE input is limited to 5 MiB, 20,000 rows, 10,000 concepts, 2,000 characters per field, and 1,000 resulting candidates. The generic CSV/TBX limit remains 1,000 records. Preview preserves domain, reliability, evaluation, primarity, lifecycle, institution, origins, modification timestamp, file SHA-256, and the attribution `Download IATE, European Union, 2026`; confirmation still imports only explicitly selected records as `proposed`. Glossary schema version 3 adds nullable columns for this source metadata and preserves older entries and history.

Translation job schema version 4 binds the job to the analyzed source through its SHA-256, page count, page geometry, rotation, and document-model version. Versions 1 through 3 remain importable, but their missing identity and layout fields are populated only after every immutable source segment has been checked against the current analysis.

Translation job schema version 5 adds the versioned Slovenian profile `sl-general-sskj-v1`, its source and canonical SHA-256 values, actually applied automatic rule IDs, manual and unverifiable rule IDs, structured diagnostics, terminology-source records, and the section 16 quality checklist. Older jobs remain importable with no profile attribution. The canonical source is stored as `docs/translation-profiles/sl-general-sskj-v1.md`; the original DOCX is not tracked.

The static rule execution mode is distinct from runtime applicability (`applicable`, `not_applicable`, or `unknown`) and the runtime check result (`passed`, `failed`, `needs_review`, `not_checkable`, or `not_applicable`). Legal, literary, advertising, markup, placeholder, and other domain-specific rules become applicable only when a concrete document or segment trigger is present. Canonical profile hashing normalizes the checked-in text to UTF-8 with LF line endings and no BOM before SHA-256 calculation, making the recorded hash independent of checkout line endings.

The profile does not contact SSKJ, eSSKJ, SIST, IATE, or another external terminology source at runtime. When no user-approved citation is recorded, the UI reports `Terminološki vir ni preverjen`; SSKJ/eSSKJ are never represented as the sole authority for orthographic or professional terminology decisions. Diagnostic warnings remain in the review UI and JSON only, never in the final translated PDF.

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

## Durable Local OCR Acceptance

An explicit OCR source acceptance is stored in a separate local SQLite database.
By default its path follows `GLOSSARY_DATABASE_PATH` and `LOCAL_DATA_PROFILE`;
`OCR_DATABASE_PATH` can override the path, but records remain profile-scoped.
Keep this database and its journals/backups out of Git. Registration tokens expire
after 30 minutes, while an accepted source revision remains verifiable after token
expiry and restart.
Verification binds the server record to the PDF hash, page, exact region, text and
revision. A JSON reference alone is not evidence, and legacy jobs without a durable
record still require review. OCR acceptance never approves its translation.
The local API and filesystem are trusted local components, not authenticated user
identity services; no reviewer identity is inferred from a stored acceptance.

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
