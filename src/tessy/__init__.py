"""Tessy - OCR driver's licences from case spreadsheets and index the text."""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "TessyIndex",
    "load_records",
    "parse_licence",
    "process_spreadsheet",
    "run_best",
]


def __getattr__(name: str):
    # Imported lazily so `import tessy` stays cheap and does not require the
    # tesseract binary to be present just to read __version__.
    if name == "TessyIndex":
        from .index import TessyIndex

        return TessyIndex
    if name == "load_records":
        from .ingest import load_records

        return load_records
    if name == "parse_licence":
        from .parse import parse_licence

        return parse_licence
    if name == "process_spreadsheet":
        from .pipeline import process_spreadsheet

        return process_spreadsheet
    if name == "run_best":
        from .ocr import run_best

        return run_best
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
