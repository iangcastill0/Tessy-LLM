# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Tessy OCRs driver's licences listed in a case spreadsheet and indexes the
extracted text for search. It is investigative tooling: the operator already
holds the documents and needs them searchable.

## Hard rules

- **Never commit case data.** `data/`, `*.xlsx`, `*.csv`, `*.db` are gitignored.
  Real licence images and extracted PII must not enter git history. Check
  `git status` before committing.
- **Tests use synthetic licences only** (`tests/synthetic.py`). Never add a real
  scan as a fixture.
- **No network calls in the pipeline.** Everything is local by design. Do not
  introduce a cloud OCR service, a model API or telemetry.

## Layout

```
src/tessy/
  ingest.py      spreadsheet -> rows + images (embedded and path columns)
  preprocess.py  PIL-only image clean-up (greyscale, upscale, contrast)
  ocr.py         tesseract subprocess wrapper; TSV parsing, confidence, rows
  parse.py       OCR text -> structured licence fields
  index.py       SQLite FTS5 index and search
  pipeline.py    orchestration
  cli.py         argparse CLI
scripts/build_tesseract.sh   from-source Tesseract build + language data
```

## Things that will bite you

- **Building Tesseract installs no language data.** A fresh build reports
  `0 languages` and all OCR fails. `scripts/build_tesseract.sh` handles this and
  size-checks the download, because a failed fetch writes a small HTML error page
  that Tesseract lists as a valid language and then fails on at runtime.
- **Don't trust Tesseract's own line numbering.** PSM 11 emits each label and
  value as separate "lines", destroying label/value adjacency. Use
  `OcrResult.visual_lines`, which regroups words by vertical position and gives
  identical rows under PSM 6 and 11. `OcrResult.lines` (Tesseract's grouping) is
  kept for debugging only.
- **Labels get misread, so they are matched by edit distance**, not equality.
  Real observed output includes `poB` for `DOB` and `cass` for `CLASS`. A
  similarity *ratio* is not usable here: `POB` scores only 0.67 against `DOB`.
  Tokens under 3 characters must match exactly, and ambiguous ties are rejected.
- **Glyph confusion in identifiers is expected** (`I`/`1`, `O`/`0`, `S`/`5`,
  `B`/`8`). Identifiers are indexed both raw and confusion-folded
  (`parse.fold_confusables`) so either spelling finds the record. Don't "fix"
  this by guessing a correction.
- **NaN confidences must be rejected explicitly** in `_parse_tsv`. Every
  comparison against NaN is False, so one would slip past a `conf < 0` check,
  poison `mean_confidence`, and make a bad document invisible to the
  low-confidence review filter.

## Working here

```bash
make install-dev
make test          # full suite (needs tesseract)
make test-unit     # only the tesseract-free tests
make lint          # ruff check
make format        # ruff format + autofix
```

Tests needing the binary are marked `requires_tesseract` and skip when it is
absent. Keep parsing, indexing and ingest logic free of that dependency so they
stay fast and runnable anywhere.

CI runs lint, format check, unit tests on Python 3.10–3.12, the full suite
against a packaged Tesseract, and a packaging job. `ruff format --check` is
enforced — run `make format` before pushing.

## Conventions

- Type hints throughout; `from __future__ import annotations` at the top.
- Comments explain *why*, not *what* — particularly where behaviour looks odd
  but is defending against real OCR noise.
- Failures in the pipeline are per-image: one unreadable scan must never abort a
  run over hundreds of rows. Collect it in `ProcessReport.failures` instead.
