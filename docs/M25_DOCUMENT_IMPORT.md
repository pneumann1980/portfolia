# M25 – Intelligent Document Import & Evidence-Based Enrichment

## Status: experimental foundation (not M25 complete)

Branch: `feature/m25-document-import-foundation` (based on v0.21.3).
The first read-only feature is available at `/journal/documents`. It analyzes up to 20 PDF,
PNG, JPEG, or WebP uploads, shows page-specific candidates for dates, ISINs and EVM
hashes, and **never** creates, updates or deletes journal transactions.

### Extraction

- The file signature is inspected instead of trusting the filename or Content-Type.
- Input is capped at 25 MiB per file, PDFs at 50 pages and rasterized images at 25 million pixels.
- Embedded text is extracted using PyMuPDF. Pages without useful text use local
  Tesseract OCR with German and English language packs.
- Pillow handles image decoding and orientation. No OCR images are sent over a network.
- Temporary OCR inputs are automatically deleted. The initial preview does not persist uploads.
- Potentially ambiguous numeric notation is refused until the decimal convention is known.
- Field evidence includes document digest, page and character offset. Matches are only
  **candidates**, not confirmed asset identity or trade execution facts.

### Evidence resolver (experimental)

`app/documentimport/evidence.py` now provides pure source-aware field decisions,
keeps competing values, requires verified event identity for provider and journal
references, and prevents public reference prices from being treated as executed
prices, fees or cost basis. This is a read-only component, *not yet wired to
Rec ingestion, provider queries or durable document storage*. Synthetic unit
tests cover source mismatches, conflicts and financial-estimate guards.

### Initial reconciliation bridge (review-only)

The preview can optionally save detected action lines as **REVIEW** records
through the existing `CsvImportService.ingest()` service. This is explicitly
non-economic staging: timestamp missing, no quantities, no currency legs,
no asserted execution price, fees or cost basis. The staging event key includes
the document digest and candidate index. The existing review page remains
responsible for later manual resolution. This is not completed automated
reconciliation or document enrichment. Re-upload deduplication and persistent
original documents remain open.

### Still required before v0.22.0

- Reliable transaction segmentation and provider profiles (including tables, dividends,
  trading fees, more chains, locale handling and screenshots with dark backgrounds).
- Persistent document storage with authorized retrieval/deletion and consistent backups.
- Field-by-field reconciliation against existing transactions, user overrides, provider
  reads and privacy-gated public lookups with auditable provenance and conflict handling.
- Mapping to `Rec` and `CsvImportService.ingest()`, preview/approval/undo support,
  with integrity checks and full regression coverage.
- Production limits for CPU and processing time and benchmark evidence for Docker/Unraid.
- User-visible integrated progress, cancellation, source comparison and bulk decisions.
- End-to-end tests and successful CI / Docker verification.

Never use this read-only preview to derive executed prices, fees, cost basis,
or taxable events without further validation and explicit user approval.
