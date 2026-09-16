"""Command line interface for Tessy."""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import sys
from pathlib import Path

from . import __version__

DEFAULT_DB = "data/output/case_index.db"


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"index database path (default: {DEFAULT_DB})"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tessy",
        description="OCR driver's licences from a spreadsheet and index the text.",
    )
    parser.add_argument("--version", action="version", version=f"tessy {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the tesseract installation")
    doctor.set_defaults(func=cmd_doctor)

    ingest = sub.add_parser("ingest", help="OCR a spreadsheet and index the results")
    ingest.add_argument("spreadsheet", help="path to the .xlsx case file")
    _add_db_arg(ingest)
    ingest.add_argument("--sheet", default=None, help="only this worksheet")
    ingest.add_argument("--header-row", type=int, default=1, help="header row (default 1)")
    ingest.add_argument("--lang", default="eng", help="tesseract language (default eng)")
    ingest.add_argument(
        "--psm",
        type=int,
        action="append",
        default=None,
        help="page-segmentation mode to try; repeat to try several",
    )
    ingest.add_argument(
        "--no-preprocess", action="store_true", help="skip image clean-up before OCR"
    )
    ingest.add_argument("--workdir", default=None, help="scratch dir for extracted images")
    ingest.add_argument("--quiet", action="store_true", help="no per-row progress")
    ingest.set_defaults(func=cmd_ingest)

    search = sub.add_parser("search", help="full-text search the index")
    search.add_argument("query", nargs="+", help="search terms")
    _add_db_arg(search)
    search.add_argument("-n", "--limit", type=int, default=20, help="max hits")
    search.add_argument("--json", action="store_true", help="JSON output")
    search.set_defaults(func=cmd_search)

    show = sub.add_parser("show", help="print one indexed document in full")
    show.add_argument("doc_id", type=int)
    _add_db_arg(show)
    show.set_defaults(func=cmd_show)

    review = sub.add_parser("review", help="list documents needing human review")
    _add_db_arg(review)
    review.add_argument(
        "--min-confidence",
        type=float,
        default=70.0,
        help="flag documents below this mean OCR confidence (default 70)",
    )
    review.set_defaults(func=cmd_review)

    stats = sub.add_parser("stats", help="index summary")
    _add_db_arg(stats)
    stats.set_defaults(func=cmd_stats)

    export = sub.add_parser("export", help="export extracted fields")
    _add_db_arg(export)
    export.add_argument("--format", choices=("csv", "json"), default="csv")
    export.add_argument("-o", "--output", default="-", help="output file, or - for stdout")
    export.set_defaults(func=cmd_export)

    return parser


# -- commands --------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    from .ocr import TesseractNotFound, available_languages, find_tesseract, tesseract_version

    try:
        binary = find_tesseract()
    except TesseractNotFound as exc:
        print(f"FAIL  tesseract: {exc}", file=sys.stderr)
        return 1

    print(f"OK    binary    : {binary}")
    print(f"OK    version   : {tesseract_version()}")
    langs = available_languages()
    if langs:
        print(f"OK    languages : {', '.join(langs)}")
    else:
        print(
            "FAIL  languages : none installed. Tesseract built from source ships no "
            "traineddata; install eng.traineddata into its tessdata directory.",
            file=sys.stderr,
        )
        return 1

    try:
        import openpyxl
        import PIL

        print(f"OK    openpyxl  : {openpyxl.__version__}")
        print(f"OK    Pillow    : {PIL.__version__}")
    except ImportError as exc:  # pragma: no cover - dependency guard
        print(f"FAIL  python deps: {exc}", file=sys.stderr)
        return 1

    print("\nAll checks passed.")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ocr import DEFAULT_PSMS
    from .pipeline import process_spreadsheet

    psms = tuple(args.psm) if args.psm else DEFAULT_PSMS
    report = process_spreadsheet(
        args.spreadsheet,
        args.db,
        workdir=args.workdir,
        sheet=args.sheet,
        header_row=args.header_row,
        lang=args.lang,
        psms=psms,
        do_preprocess=not args.no_preprocess,
        progress=not args.quiet,
    )
    print()
    print(report.summary())
    print(f"\nIndex written to {Path(args.db).resolve()}")
    print(f"Search it with:  tessy search <terms> --db {args.db}")
    return 1 if report.failures and report.indexed == 0 else 0


def cmd_search(args: argparse.Namespace) -> int:
    from .index import TessyIndex

    query = " ".join(args.query)
    with TessyIndex(args.db) as index:
        try:
            hits = index.search(query, limit=args.limit)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps([h.as_dict() for h in hits], indent=2))
        return 0

    if not hits:
        print(f"No matches for {query!r}.")
        return 0

    print(f"{len(hits)} match(es) for {query!r}:\n")
    for hit in hits:
        name = hit.full_name or "(name not parsed)"
        print(f"  #{hit.id}  {name}")
        print(
            f"      licence : {hit.licence_no or '-'}   dob: {hit.dob or '-'}   "
            f"jurisdiction: {hit.jurisdiction or '-'}"
        )
        print(f"      source  : {Path(hit.source).name} [{hit.sheet}] row {hit.row}")
        if hit.ocr_confidence is not None:
            print(f"      ocr conf: {hit.ocr_confidence:.1f}")
        if hit.snippet:
            print(f"      match   : {hit.snippet}")
        print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .index import TessyIndex

    with TessyIndex(args.db) as index:
        doc = index.get(args.doc_id)
    if not doc:
        print(f"No document with id {args.doc_id}.", file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2, default=str))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from .index import TessyIndex

    with TessyIndex(args.db) as index:
        flagged = index.needs_review(min_confidence=args.min_confidence)

    if not flagged:
        print("Nothing flagged for review.")
        return 0

    print(f"{len(flagged)} document(s) need a human check:\n")
    for doc in flagged:
        warnings = json.loads(doc.get("warnings") or "[]")
        conf = doc.get("ocr_confidence")
        print(
            f"  #{doc['id']}  {doc.get('full_name') or '(name not parsed)'}"
            f"   conf: {conf if conf is not None else '-'}"
        )
        print(f"      image: {doc.get('image_path') or '(text-only row)'}")
        for warning in warnings:
            print(f"      ! {warning}")
        print()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from .index import TessyIndex

    with TessyIndex(args.db) as index:
        stats = index.stats()
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key.replace('_', ' '):<{width}} : {value if value is not None else '-'}")
    return 0


EXPORT_COLUMNS = (
    "id",
    "source",
    "sheet",
    "row",
    "image_path",
    "licence_no",
    "last_name",
    "first_name",
    "full_name",
    "dob",
    "expiry",
    "issued",
    "sex",
    "height",
    "eyes",
    "licence_class",
    "address",
    "jurisdiction",
    "document_type",
    "ocr_confidence",
    "completeness",
    "warnings",
)


def cmd_export(args: argparse.Namespace) -> int:
    from .index import TessyIndex

    with TessyIndex(args.db) as index:
        docs = index.all_documents()

    to_stdout = args.output == "-"
    with contextlib.ExitStack() as stack:
        if to_stdout:
            stream = sys.stdout
        else:
            stream = stack.enter_context(open(args.output, "w", newline=""))

        if args.format == "json":
            json.dump(docs, stream, indent=2, default=str)
            stream.write("\n")
        else:
            writer = csv.DictWriter(stream, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            for doc in docs:
                writer.writerow({k: doc.get(k) for k in EXPORT_COLUMNS})

    if not to_stdout:
        print(f"Wrote {len(docs)} row(s) to {args.output}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
