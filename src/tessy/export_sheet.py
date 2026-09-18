"""Write extracted licence fields to a spreadsheet.

openpyxl is already a Tessy dependency (ingest reads .xlsx). Writing the
operator-facing result workbook keeps the whole loop offline and archivable
next to the scans.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import openpyxl

# Column order for the operator-facing workbook: verified id first, then the
# fields an examiner copies into a case record.
SPREADSHEET_COLUMNS: tuple[str, ...] = (
    "verified_licence_no",
    "licence_no",
    "full_name",
    "last_name",
    "first_name",
    "dob",
    "expiry",
    "issued",
    "sex",
    "height",
    "weight",
    "eyes",
    "hair",
    "licence_class",
    "address",
    "jurisdiction",
    "document_type",
    "ocr_confidence",
    "completeness",
    "warnings",
    "image_path",
    "source",
    "sheet",
    "row",
)

_HEADERS = {
    "verified_licence_no": "Verified DL#",
    "licence_no": "Licence no (indexed)",
    "full_name": "Name",
    "last_name": "Last name",
    "first_name": "First name",
    "dob": "Date of birth",
    "expiry": "Expires",
    "issued": "Issued",
    "sex": "Sex",
    "height": "Height",
    "weight": "Weight",
    "eyes": "Eyes",
    "hair": "Hair",
    "licence_class": "Class",
    "address": "Address",
    "jurisdiction": "Jurisdiction",
    "document_type": "Document",
    "ocr_confidence": "OCR confidence",
    "completeness": "Completeness",
    "warnings": "Warnings",
    "image_path": "Image",
    "source": "Source",
    "sheet": "Sheet",
    "row": "Row",
}

_VERIFIED_RE = re.compile(r"verified_licence_no:\s*(\S+)", re.IGNORECASE)


def verified_from_doc(doc: dict[str, Any]) -> str | None:
    """Recover the filename-verified DL# from sheet_text when present."""
    sheet_text = doc.get("sheet_text") or ""
    match = _VERIFIED_RE.search(str(sheet_text))
    if match:
        return match.group(1)
    # Folder runs set licence_no to the verified value as well.
    value = doc.get("licence_no")
    return str(value) if value else None


def _cell_value(doc: dict[str, Any], key: str) -> Any:
    if key == "verified_licence_no":
        return verified_from_doc(doc)
    if key == "warnings":
        raw = doc.get("warnings")
        if isinstance(raw, list):
            return "; ".join(raw)
        if isinstance(raw, str):
            try:
                return "; ".join(json.loads(raw))
            except (ValueError, TypeError):
                return raw
        return ""
    return doc.get(key)


def write_xlsx(docs: list[dict[str, Any]], path: str | Path) -> Path:
    """Write one row per document to an .xlsx workbook."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Licences"
    ws.append([_HEADERS[c] for c in SPREADSHEET_COLUMNS])
    for doc in docs:
        ws.append([_cell_value(doc, c) for c in SPREADSHEET_COLUMNS])
    wb.save(path)
    return path


def write_csv(docs: list[dict[str, Any]], path: str | Path) -> Path:
    """Write one row per document to a CSV file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SPREADSHEET_COLUMNS))
        writer.writeheader()
        for doc in docs:
            writer.writerow({c: _cell_value(doc, c) for c in SPREADSHEET_COLUMNS})
    return path
