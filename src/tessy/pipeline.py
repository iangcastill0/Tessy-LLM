"""End-to-end run: spreadsheet in, searchable index out."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .index import TessyIndex
from .ingest import ImageRef, SheetRecord, load_image_folder, load_records
from .ocr import DEFAULT_LANG, DEFAULT_PSMS, OcrResult, TesseractFailed, run_best
from .parse import apply_verified_licence_no, looks_like_driver_licence, parse_licence
from .preprocess import preprocess

log = logging.getLogger("tessy")


@dataclass
class Failure:
    where: str
    error: str


@dataclass
class ProcessReport:
    """Summary of one pipeline run."""

    spreadsheet: str
    rows: int = 0
    images: int = 0
    indexed: int = 0
    text_only_rows: int = 0
    failures: list[Failure] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    duration_s: float = 0.0
    cancelled: bool = False

    @property
    def mean_confidence(self) -> float:
        return sum(self.confidences) / len(self.confidences) if self.confidences else 0.0

    def summary(self) -> str:
        lines = [
            f"spreadsheet     : {self.spreadsheet}",
            f"rows read       : {self.rows}",
            f"licence images  : {self.images}",
            f"documents indexed: {self.indexed}",
            f"text-only rows  : {self.text_only_rows}",
            f"mean OCR conf   : {self.mean_confidence:.1f}",
            f"elapsed         : {self.duration_s:.1f}s",
        ]
        if self.cancelled:
            lines.insert(1, "status          : CANCELLED (partial results kept)")
        if self.failures:
            lines.append(f"failures        : {len(self.failures)}")
            lines.extend(f"  - {f.where}: {f.error}" for f in self.failures[:10])
            if len(self.failures) > 10:
                lines.append(f"  ... and {len(self.failures) - 10} more")
        return "\n".join(lines)


def ocr_image(
    image: ImageRef,
    workdir: Path,
    *,
    lang: str = DEFAULT_LANG,
    psms: tuple[int, ...] = DEFAULT_PSMS,
    do_preprocess: bool = True,
) -> OcrResult:
    """Preprocess (optionally) and OCR a single licence image."""
    target = image.path
    if do_preprocess:
        cleaned = workdir / "preprocessed" / f"{image.path.stem}_clean.png"
        try:
            target = preprocess(image.path, cleaned)
        except Exception as exc:  # noqa: BLE001 - fall back to the original scan
            log.warning("preprocess failed for %s (%s); using original", image.path, exc)
            target = image.path
    return run_best(target, lang=lang, psms=psms)


def process_record(
    record: SheetRecord,
    index: TessyIndex,
    *,
    source: str,
    workdir: Path,
    report: ProcessReport,
    lang: str = DEFAULT_LANG,
    psms: tuple[int, ...] = DEFAULT_PSMS,
    do_preprocess: bool = True,
) -> None:
    """OCR every image on a row and index the results.

    A row with no images is still indexed on its text alone, so the spreadsheet's
    own notes remain searchable next to the OCR output.
    """
    sheet_text = record.combined_text

    if not record.images:
        index.add_document(
            source=source,
            sheet=record.sheet,
            row=record.row,
            image_path=None,
            sheet_text=sheet_text,
        )
        report.text_only_rows += 1
        report.indexed += 1
        return

    for image in record.images:
        report.images += 1
        try:
            result = ocr_image(image, workdir, lang=lang, psms=psms, do_preprocess=do_preprocess)
        except (TesseractFailed, FileNotFoundError, OSError) as exc:
            # One unreadable scan must not abort a 500-row case file.
            report.failures.append(Failure(where=image.label, error=str(exc)))
            log.error("OCR failed for %s: %s", image.path, exc)
            continue

        fields = parse_licence(result.visual_lines)
        verified = record.text.get("verified_licence_no")
        apply_verified_licence_no(fields, verified)
        if result.text and not looks_like_driver_licence(result.text):
            fields.warnings.append(
                "OCR text may not be a driver licence - verify against the image"
            )
            fields.warnings = list(dict.fromkeys(fields.warnings))

        index.add_document(
            source=source,
            sheet=record.sheet,
            row=record.row,
            image_path=str(image.path),
            image_origin=image.origin,
            ocr_text=result.text,
            ocr_confidence=round(result.mean_confidence, 2),
            ocr_psm=result.psm,
            sheet_text=sheet_text,
            fields=fields.as_dict(),
        )
        report.confidences.append(result.mean_confidence)
        report.indexed += 1


def process_spreadsheet(
    xlsx_path: str | Path,
    db_path: str | Path,
    *,
    workdir: str | Path | None = None,
    sheet: str | None = None,
    header_row: int = 1,
    lang: str = DEFAULT_LANG,
    psms: tuple[int, ...] = DEFAULT_PSMS,
    do_preprocess: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProcessReport:
    """Run the full pipeline over one spreadsheet.

    `on_progress(done, total, label)` is called before each row, and
    `should_cancel()` is polled between rows. Both exist so a GUI can show
    progress and stop a long run without killing the process; a cancelled run
    keeps whatever it already indexed and says so in the report.
    """
    started = time.monotonic()
    xlsx_path = Path(xlsx_path).resolve()
    workdir = Path(workdir) if workdir else Path(db_path).parent / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    report = ProcessReport(spreadsheet=str(xlsx_path))
    records = load_records(xlsx_path, workdir, sheet=sheet, header_row=header_row)
    report.rows = len(records)

    with TessyIndex(db_path) as index:
        for position, record in enumerate(records, start=1):
            if should_cancel is not None and should_cancel():
                report.cancelled = True
                log.info("run cancelled after %d of %d rows", position - 1, len(records))
                break

            if on_progress is not None:
                on_progress(
                    position,
                    len(records),
                    f"{record.sheet} row {record.row} ({len(record.images)} image(s))",
                )
            process_record(
                record,
                index,
                source=str(xlsx_path),
                workdir=workdir,
                report=report,
                lang=lang,
                psms=psms,
                do_preprocess=do_preprocess,
            )

    report.duration_s = time.monotonic() - started
    return report


def process_image_folder(
    folder: str | Path,
    db_path: str | Path,
    *,
    workdir: str | Path | None = None,
    lang: str = DEFAULT_LANG,
    psms: tuple[int, ...] = DEFAULT_PSMS,
    do_preprocess: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProcessReport:
    """OCR a folder of licence images named by verified DL#.

    ``I1234562.png`` becomes one indexed row whose licence number is taken from
    the filename (operator-verified) and whose other fields come from OCR.
    """
    started = time.monotonic()
    folder = Path(folder).resolve()
    workdir = Path(workdir) if workdir else Path(db_path).parent / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    report = ProcessReport(spreadsheet=str(folder))
    records = load_image_folder(folder)
    report.rows = len(records)

    with TessyIndex(db_path) as index:
        for position, record in enumerate(records, start=1):
            if should_cancel is not None and should_cancel():
                report.cancelled = True
                log.info(
                    "folder run cancelled after %d of %d images",
                    position - 1,
                    len(records),
                )
                break

            verified = record.text.get("verified_licence_no", record.images[0].path.name)
            if on_progress is not None:
                on_progress(position, len(records), f"{verified} ({record.images[0].path.name})")
            process_record(
                record,
                index,
                source=str(folder),
                workdir=workdir,
                report=report,
                lang=lang,
                psms=psms,
                do_preprocess=do_preprocess,
            )

    report.duration_s = time.monotonic() - started
    return report
