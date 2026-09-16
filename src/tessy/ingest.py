"""Read case spreadsheets and pull out per-row text and licence images.

Two layouts are supported and auto-detected, because both are common in case
work and the operator should not have to care which they were handed:

* images embedded directly in the workbook, anchored to a row;
* a column of file paths pointing at image files next to the spreadsheet.

A single row may carry both. Paths are resolved relative to the spreadsheet so a
case folder stays portable.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".jp2"}

# Magic numbers -> extension, so embedded blobs are written with a sane suffix.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"RIFF", ".webp"),
)


class SpreadsheetError(RuntimeError):
    """Raised when a workbook cannot be read as case data."""


@dataclass
class ImageRef:
    """One licence image belonging to a spreadsheet row."""

    path: Path
    origin: str  # "embedded" or "path"
    sheet: str
    row: int
    column: str | None = None

    @property
    def label(self) -> str:
        return f"{self.sheet}!r{self.row}:{self.origin}"


@dataclass
class SheetRecord:
    """One spreadsheet row: its text columns plus any licence images on it."""

    sheet: str
    row: int
    text: dict[str, str] = field(default_factory=dict)
    images: list[ImageRef] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        """All text cells joined, for indexing alongside the OCR output."""
        return " ".join(f"{k}: {v}" for k, v in self.text.items() if v)


def _extension_for(blob: bytes) -> str:
    for magic, ext in _MAGIC:
        if blob.startswith(magic):
            return ext
    return ".png"


def _column_letter(index0: int) -> str:
    """0-indexed column number to spreadsheet letters (0 -> A, 26 -> AA)."""
    letters = ""
    index = index0 + 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _headers(worksheet, header_row: int) -> dict[int, str]:
    """Column index (1-based) -> header name, falling back to column letters."""
    out: dict[int, str] = {}
    for cell in worksheet[header_row] if worksheet.max_row >= header_row else []:
        name = str(cell.value).strip() if cell.value is not None else ""
        out[cell.column] = name or _column_letter(cell.column - 1)
    return out


def extract_embedded_images(
    xlsx_path: str | Path, workdir: str | Path, *, sheet: str | None = None
) -> dict[tuple[str, int], list[ImageRef]]:
    """Write every embedded image to `workdir`, keyed by (sheet, 1-based row).

    Uses openpyxl's image list, which exposes the drawing anchors we need to tie
    a picture back to its row.
    """
    xlsx_path, workdir = Path(xlsx_path), Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        workbook = openpyxl.load_workbook(xlsx_path)
    except zipfile.BadZipFile as exc:
        raise SpreadsheetError(
            f"{xlsx_path} is not a readable .xlsx file (legacy .xls is not supported; "
            "re-save it as .xlsx)"
        ) from exc

    found: dict[tuple[str, int], list[ImageRef]] = {}
    try:
        for worksheet in workbook.worksheets:
            if sheet and worksheet.title != sheet:
                continue
            for counter, image in enumerate(getattr(worksheet, "_images", [])):
                anchor = getattr(image, "anchor", None)
                marker = getattr(anchor, "_from", None)
                if marker is None:
                    continue
                row = int(marker.row) + 1  # anchors are 0-indexed
                col = int(marker.col)

                try:
                    blob = image._data()
                except Exception as exc:  # pragma: no cover - corrupt drawing
                    raise SpreadsheetError(
                        f"could not read embedded image {counter} on sheet "
                        f"{worksheet.title!r}: {exc}"
                    ) from exc

                dest = workdir / (
                    f"{_safe(worksheet.title)}_r{row}_{counter}{_extension_for(blob)}"
                )
                dest.write_bytes(blob)
                found.setdefault((worksheet.title, row), []).append(
                    ImageRef(
                        path=dest,
                        origin="embedded",
                        sheet=worksheet.title,
                        row=row,
                        column=_column_letter(col),
                    )
                )
    finally:
        workbook.close()

    return found


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)


def load_records(
    xlsx_path: str | Path,
    workdir: str | Path,
    *,
    sheet: str | None = None,
    header_row: int = 1,
) -> list[SheetRecord]:
    """Read `xlsx_path` into one SheetRecord per data row.

    Rows with neither text nor images are skipped so trailing blank rows in a
    spreadsheet do not become empty index entries.
    """
    xlsx_path, workdir = Path(xlsx_path), Path(workdir)
    if not xlsx_path.is_file():
        raise SpreadsheetError(f"No such spreadsheet: {xlsx_path}")

    embedded = extract_embedded_images(xlsx_path, workdir, sheet=sheet)

    try:
        workbook = openpyxl.load_workbook(xlsx_path, data_only=True)
    except zipfile.BadZipFile as exc:
        raise SpreadsheetError(f"{xlsx_path} is not a readable .xlsx file") from exc

    records: list[SheetRecord] = []
    try:
        for worksheet in workbook.worksheets:
            if sheet and worksheet.title != sheet:
                continue
            headers = _headers(worksheet, header_row)

            for row_cells in worksheet.iter_rows(min_row=header_row + 1):
                row_number = row_cells[0].row if row_cells else None
                if row_number is None:
                    continue

                record = SheetRecord(sheet=worksheet.title, row=row_number)

                for cell in row_cells:
                    if cell.value is None:
                        continue
                    value = str(cell.value).strip()
                    if not value:
                        continue
                    header = headers.get(cell.column, _column_letter(cell.column - 1))

                    resolved = _as_image_path(value, xlsx_path.parent)
                    if resolved is not None:
                        record.images.append(
                            ImageRef(
                                path=resolved,
                                origin="path",
                                sheet=worksheet.title,
                                row=row_number,
                                column=_column_letter(cell.column - 1),
                            )
                        )
                    else:
                        record.text[header] = value

                record.images.extend(embedded.get((worksheet.title, row_number), []))

                if record.text or record.images:
                    records.append(record)
    finally:
        workbook.close()

    return records


def _as_image_path(value: str, base: Path) -> Path | None:
    """Return a resolved path when `value` names an image file that exists."""
    candidate = value.strip().strip('"').strip("'")
    if not candidate or "\n" in candidate:
        return None
    if Path(candidate).suffix.lower() not in IMAGE_SUFFIXES:
        return None

    for option in (Path(candidate), base / candidate):
        try:
            if option.is_file():
                return option.resolve()
        except OSError:
            continue
    return None
