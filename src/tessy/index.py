"""SQLite full-text index over OCR output and spreadsheet text.

SQLite with FTS5 is deliberate: the whole point is that a case index stays on
the examiner's machine as a single file. No server, no daemon, nothing that
could ship licence data off the box, and the index is trivially archivable
alongside the rest of the case material.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .parse import fold_confusables

SCHEMA_VERSION = 1

# Columns carried on each indexed document. Kept explicit so the FTS triggers
# and the insert statement cannot drift apart silently.
_FIELD_COLUMNS = (
    "licence_no",
    "licence_no_folded",
    "last_name",
    "first_name",
    "full_name",
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
)

_SCHEMA = f"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    sheet          TEXT NOT NULL,
    row            INTEGER NOT NULL,
    image_path     TEXT,
    image_origin   TEXT,
    ocr_text       TEXT NOT NULL DEFAULT '',
    ocr_confidence REAL,
    ocr_psm        INTEGER,
    sheet_text     TEXT NOT NULL DEFAULT '',
    {chr(10).join(f"    {c} TEXT," for c in _FIELD_COLUMNS)}
    completeness   REAL,
    warnings       TEXT,
    indexed_at     TEXT NOT NULL,
    UNIQUE(source, sheet, row, image_path)
);

CREATE INDEX IF NOT EXISTS idx_documents_licence  ON documents(licence_no_folded);
CREATE INDEX IF NOT EXISTS idx_documents_name     ON documents(last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_documents_dob      ON documents(dob);
CREATE INDEX IF NOT EXISTS idx_documents_source   ON documents(source, sheet, row);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    ocr_text, sheet_text, full_name, licence_no, licence_no_folded,
    address, jurisdiction,
    content='documents',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(
        rowid, ocr_text, sheet_text, full_name, licence_no, licence_no_folded,
        address, jurisdiction)
    VALUES (new.id, new.ocr_text, new.sheet_text, new.full_name, new.licence_no,
            new.licence_no_folded, new.address, new.jurisdiction);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(
        documents_fts, rowid, ocr_text, sheet_text, full_name, licence_no,
        licence_no_folded, address, jurisdiction)
    VALUES ('delete', old.id, old.ocr_text, old.sheet_text, old.full_name,
            old.licence_no, old.licence_no_folded, old.address, old.jurisdiction);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(
        documents_fts, rowid, ocr_text, sheet_text, full_name, licence_no,
        licence_no_folded, address, jurisdiction)
    VALUES ('delete', old.id, old.ocr_text, old.sheet_text, old.full_name,
            old.licence_no, old.licence_no_folded, old.address, old.jurisdiction);
    INSERT INTO documents_fts(
        rowid, ocr_text, sheet_text, full_name, licence_no, licence_no_folded,
        address, jurisdiction)
    VALUES (new.id, new.ocr_text, new.sheet_text, new.full_name, new.licence_no,
            new.licence_no_folded, new.address, new.jurisdiction);
END;
"""


@dataclass
class SearchHit:
    """One search result, with a highlighted snippet for context."""

    id: int
    source: str
    sheet: str
    row: int
    image_path: str | None
    full_name: str | None
    licence_no: str | None
    dob: str | None
    jurisdiction: str | None
    completeness: float | None
    ocr_confidence: float | None
    snippet: str
    score: float

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def _is_identifier_query(query: str) -> bool:
    """True when the query looks like a licence number rather than words."""
    stripped = re.sub(r"[^A-Za-z0-9]", "", query)
    return len(stripped) >= 5 and any(ch.isdigit() for ch in stripped) and " " not in query.strip()


def _quote_fts(term: str) -> str:
    """Wrap a term as an FTS5 string literal so punctuation can't inject syntax."""
    return '"' + term.replace('"', '""') + '"'


class TessyIndex:
    """A searchable index backed by one SQLite file."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> TessyIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing -----------------------------------------------------------
    def add_document(
        self,
        *,
        source: str,
        sheet: str,
        row: int,
        image_path: str | None,
        image_origin: str | None = None,
        ocr_text: str = "",
        ocr_confidence: float | None = None,
        ocr_psm: int | None = None,
        sheet_text: str = "",
        fields: dict[str, object] | None = None,
    ) -> int:
        """Insert or replace one document. Returns its row id.

        Keyed on (source, sheet, row, image_path) so re-running the pipeline over
        the same spreadsheet refreshes entries instead of duplicating them.
        """
        fields = dict(fields or {})
        values: dict[str, object] = {
            "source": source,
            "sheet": sheet,
            "row": row,
            "image_path": image_path,
            "image_origin": image_origin,
            "ocr_text": ocr_text or "",
            "ocr_confidence": ocr_confidence,
            "ocr_psm": ocr_psm,
            "sheet_text": sheet_text or "",
            "completeness": fields.get("completeness"),
            "warnings": json.dumps(fields.get("warnings") or []),
            "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        for column in _FIELD_COLUMNS:
            values[column] = fields.get(column)

        columns = ", ".join(values)
        placeholders = ", ".join(f":{k}" for k in values)
        updates = ", ".join(
            f"{k}=excluded.{k}" for k in values if k not in {"source", "sheet", "row"}
        )
        cur = self.conn.execute(
            f"INSERT INTO documents ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(source, sheet, row, image_path) DO UPDATE SET {updates} "
            f"RETURNING id",
            values,
        )
        doc_id = int(cur.fetchone()[0])
        self.conn.commit()
        return doc_id

    # -- reading -----------------------------------------------------------
    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        """Full-text search. Returns hits best-first.

        A query that looks like a licence number is additionally matched against
        the confusion-folded form, so searching ``I1234562`` still finds a scan
        OCR'd as ``11234562``.
        """
        query = query.strip()
        if not query:
            return []

        match_expr = _build_match(query)
        try:
            rows = self.conn.execute(
                """
                SELECT d.*,
                       snippet(documents_fts, 0, '[', ']', ' ... ', 12) AS snip,
                       bm25(documents_fts) AS score
                  FROM documents_fts
                  JOIN documents d ON d.id = documents_fts.rowid
                 WHERE documents_fts MATCH ?
                 ORDER BY score
                 LIMIT ?
                """,
                (match_expr, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"invalid search query {query!r}: {exc}") from exc

        return [
            SearchHit(
                id=r["id"],
                source=r["source"],
                sheet=r["sheet"],
                row=r["row"],
                image_path=r["image_path"],
                full_name=r["full_name"],
                licence_no=r["licence_no"],
                dob=r["dob"],
                jurisdiction=r["jurisdiction"],
                completeness=r["completeness"],
                ocr_confidence=r["ocr_confidence"],
                snippet=(r["snip"] or "").strip(),
                score=float(r["score"]),
            )
            for r in rows
        ]

    def get(self, doc_id: int) -> dict[str, object] | None:
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def all_documents(self) -> list[dict[str, object]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM documents ORDER BY source, sheet, row")
        ]

    def stats(self) -> dict[str, object]:
        row = self.conn.execute(
            """
            SELECT COUNT(*)                                        AS documents,
                   COUNT(DISTINCT source)                          AS sources,
                   AVG(ocr_confidence)                             AS mean_confidence,
                   AVG(completeness)                               AS mean_completeness,
                   SUM(CASE WHEN licence_no IS NULL THEN 1 ELSE 0 END) AS missing_licence_no,
                   SUM(CASE WHEN warnings != '[]' THEN 1 ELSE 0 END)   AS with_warnings
              FROM documents
            """
        ).fetchone()
        out = dict(row)
        for key in ("mean_confidence", "mean_completeness"):
            if out.get(key) is not None:
                out[key] = round(float(out[key]), 2)
        return out

    def needs_review(self, *, min_confidence: float = 70.0) -> list[dict[str, object]]:
        """Documents a human should eyeball: low OCR confidence or parser warnings."""
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT * FROM documents
                 WHERE (ocr_confidence IS NOT NULL AND ocr_confidence < ?)
                    OR warnings != '[]'
                    OR licence_no IS NULL
                 ORDER BY ocr_confidence IS NULL DESC, ocr_confidence ASC
                """,
                (min_confidence,),
            )
        ]


def _build_match(query: str) -> str:
    """Turn a user query into an FTS5 MATCH expression."""
    # Pass through explicit FTS5 syntax so power users keep AND/OR/NEAR/prefixes.
    if re.search(r'[:"*()]|\b(AND|OR|NOT|NEAR)\b', query):
        return query

    if _is_identifier_query(query):
        folded = fold_confusables(query)
        terms = {_quote_fts(query), _quote_fts(folded)}
        return " OR ".join(sorted(terms))

    return " ".join(_quote_fts(t) for t in query.split())
