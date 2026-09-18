"""Tests for the SQLite/FTS5 index. No tesseract needed."""

from __future__ import annotations

import json

import pytest

from tessy.index import TessyIndex
from tessy.parse import fold_confusables

CA_FIELDS = {
    "licence_no": "11234562",
    "licence_no_folded": fold_confusables("11234562"),
    "last_name": "CARDHOLDER",
    "first_name": "ALEXANDER J",
    "full_name": "ALEXANDER J CARDHOLDER",
    "dob": "1977-08-31",
    "expiry": "2028-08-31",
    "jurisdiction": "CALIFORNIA",
    "address": "2570 24TH STREET, ANYTOWN, CA 95818",
    "completeness": 1.0,
    "warnings": ["licence_no is all digits"],
}

TX_FIELDS = {
    "licence_no": "T4459981",
    "licence_no_folded": fold_confusables("T4459981"),
    "last_name": "RIVERA",
    "first_name": "MARIA L",
    "full_name": "MARIA L RIVERA",
    "dob": "1985-02-14",
    "jurisdiction": "TEXAS",
    "address": "118 LAMAR BLVD, AUSTIN, TX 78701",
    "completeness": 0.83,
    "warnings": [],
}


@pytest.fixture
def index(tmp_path):
    with TessyIndex(tmp_path / "case.db") as idx:
        idx.add_document(
            source="/cases/a.xlsx",
            sheet="Licences",
            row=2,
            image_path="/cases/img2.png",
            image_origin="embedded",
            ocr_text="CALIFORNIA DRIVER LICENSE DL 11234562 CARDHOLDER ALEXANDER J",
            ocr_confidence=94.2,
            ocr_psm=11,
            sheet_text="Case Ref: CASE-1002 Notes: interviewed",
            fields=CA_FIELDS,
        )
        idx.add_document(
            source="/cases/a.xlsx",
            sheet="Licences",
            row=3,
            image_path="/cases/img3.png",
            image_origin="embedded",
            ocr_text="TEXAS DRIVER LICENSE DL T4459981 RIVERA MARIA L",
            ocr_confidence=61.0,
            ocr_psm=6,
            sheet_text="Case Ref: CASE-1003",
            fields=TX_FIELDS,
        )
        yield idx


class TestSearch:
    def test_finds_by_surname(self, index):
        hits = index.search("RIVERA")
        assert len(hits) == 1
        assert hits[0].full_name == "MARIA L RIVERA"

    def test_finds_by_spreadsheet_text_not_on_the_image(self, index):
        hits = index.search("CASE-1002")
        assert [h.row for h in hits] == [2]

    def test_finds_by_exact_licence_number(self, index):
        assert index.search("T4459981")[0].licence_no == "T4459981"

    def test_finds_despite_glyph_confusion(self, index):
        # The true number starts with a letter I; OCR recorded a digit 1.
        hits = index.search("I1234562")
        assert len(hits) == 1
        assert hits[0].licence_no == "11234562"

    def test_multi_word_query_is_conjunctive(self, index):
        assert len(index.search("MARIA RIVERA")) == 1
        assert index.search("MARIA CARDHOLDER") == []

    def test_snippet_marks_the_match(self, index):
        assert "[" in index.search("CARDHOLDER")[0].snippet

    def test_no_match_returns_empty(self, index):
        assert index.search("NONEXISTENTNAME") == []

    def test_blank_query_returns_empty(self, index):
        assert index.search("   ") == []

    def test_limit_is_respected(self, index):
        assert len(index.search("DRIVER LICENSE", limit=1)) == 1

    def test_punctuation_does_not_break_the_query(self, index):
        # Must not raise an FTS5 syntax error.
        assert index.search("AUSTIN, TX") is not None

    def test_jurisdiction_is_searchable(self, index):
        assert len(index.search("TEXAS")) == 1


class TestWriting:
    def test_reingesting_updates_instead_of_duplicating(self, index):
        before = index.stats()["documents"]
        index.add_document(
            source="/cases/a.xlsx",
            sheet="Licences",
            row=2,
            image_path="/cases/img2.png",
            ocr_text="RESCANNED AT HIGHER RESOLUTION",
            ocr_confidence=99.0,
            fields={**CA_FIELDS, "last_name": "REVISED", "full_name": "REVISED NAME"},
        )
        assert index.stats()["documents"] == before
        assert index.search("REVISED")[0].row == 2
        assert index.search("RESCANNED")[0].row == 2
        # The superseded text must be gone from the full-text index too, or a
        # re-run would leave stale OCR findable forever.
        assert index.search("CALIFORNIA DRIVER") == []

    def test_only_full_name_is_full_text_searchable(self, index):
        """Surnames are searchable through the derived full_name, not last_name.

        Documented deliberately: the pipeline always supplies full_name, so a
        caller writing last_name alone would not be findable by that surname.
        """
        index.add_document(
            source="/cases/b.xlsx",
            sheet="S",
            row=1,
            image_path="x.png",
            fields={"last_name": "ORPHANED"},
        )
        assert index.search("ORPHANED") == []
        index.add_document(
            source="/cases/b.xlsx",
            sheet="S",
            row=1,
            image_path="x.png",
            fields={"last_name": "ORPHANED", "full_name": "ORPHANED"},
        )
        assert len(index.search("ORPHANED")) == 1

    def test_text_only_row_can_be_indexed(self, index):
        index.add_document(
            source="/cases/a.xlsx",
            sheet="Licences",
            row=9,
            image_path=None,
            sheet_text="Case Ref: CASE-9999 no scan supplied",
        )
        assert index.search("CASE-9999")[0].row == 9

    def test_deleting_removes_from_fts(self, index):
        doc_id = index.search("RIVERA")[0].id
        index.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        index.conn.commit()
        assert index.search("RIVERA") == []


class TestReporting:
    def test_stats_counts_documents_and_sources(self, index):
        stats = index.stats()
        assert stats["documents"] == 2
        assert stats["sources"] == 1
        assert stats["with_warnings"] == 1

    def test_review_flags_low_confidence(self, index):
        flagged = index.needs_review(min_confidence=70.0)
        rows = {d["row"] for d in flagged}
        assert 3 in rows  # 61.0 confidence is below threshold

    def test_review_flags_parser_warnings_even_at_high_confidence(self, index):
        flagged = index.needs_review(min_confidence=0.0)
        assert 2 in {d["row"] for d in flagged}

    def test_get_returns_full_record(self, index):
        doc_id = index.search("CARDHOLDER")[0].id
        assert index.get(doc_id)["jurisdiction"] == "CALIFORNIA"

    def test_get_unknown_id_returns_none(self, index):
        assert index.get(99999) is None

    def test_index_persists_across_reopen(self, tmp_path):
        path = tmp_path / "persist.db"
        with TessyIndex(path) as idx:
            idx.add_document(
                source="s.xlsx",
                sheet="S",
                row=1,
                image_path="i.png",
                ocr_text="PERSISTED VALUE",
                fields={},
            )
        with TessyIndex(path) as reopened:
            assert len(reopened.search("PERSISTED")) == 1


class TestFieldUpdates:
    def test_update_fields_corrects_licence_and_fts(self, index):
        doc_id = index.search("CARDHOLDER")[0].id
        updated = index.update_fields(doc_id, {"licence_no": "I1234562"})
        assert updated["licence_no"] == "I1234562"
        assert updated["licence_no_folded"] == fold_confusables("I1234562")
        assert updated["reviewed_at"]
        # All-digit warning should drop once the leading letter is restored.
        assert "all digits" not in " ".join(json.loads(updated["warnings"]))
        assert index.search("I1234562")[0].id == doc_id
        # Folding still lets the old all-digit spelling find the corrected record.
        assert index.search("11234562")[0].id == doc_id
        assert index.get(doc_id)["licence_no"] == "I1234562"

    def test_update_fields_rebuilds_full_name_when_blank(self, index):
        doc_id = index.search("RIVERA")[0].id
        updated = index.update_fields(
            doc_id, {"last_name": "RIVERA-SMITH", "first_name": "MARIA L", "full_name": ""}
        )
        assert updated["full_name"] == "MARIA L RIVERA-SMITH"
        assert index.search("RIVERA-SMITH")[0].id == doc_id

    def test_mark_reviewed_excludes_from_review_queue(self, index):
        before = {d["id"] for d in index.needs_review()}
        assert before
        for doc_id in before:
            index.mark_reviewed(doc_id)
        assert index.needs_review() == []

    def test_update_unknown_id_returns_none(self, index):
        assert index.update_fields(99999, {"licence_no": "X"}) is None

    def test_reviewed_at_migrates_on_old_databases(self, tmp_path):
        """A v1 database without reviewed_at still opens and accepts corrections."""
        import sqlite3

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                sheet TEXT NOT NULL,
                row INTEGER NOT NULL,
                image_path TEXT,
                image_origin TEXT,
                ocr_text TEXT NOT NULL DEFAULT '',
                ocr_confidence REAL,
                ocr_psm INTEGER,
                sheet_text TEXT NOT NULL DEFAULT '',
                licence_no TEXT,
                licence_no_folded TEXT,
                last_name TEXT,
                first_name TEXT,
                full_name TEXT,
                dob TEXT,
                expiry TEXT,
                issued TEXT,
                sex TEXT,
                height TEXT,
                weight TEXT,
                eyes TEXT,
                hair TEXT,
                licence_class TEXT,
                address TEXT,
                jurisdiction TEXT,
                document_type TEXT,
                completeness REAL,
                warnings TEXT,
                indexed_at TEXT NOT NULL,
                UNIQUE(source, sheet, row, image_path)
            );
            CREATE VIRTUAL TABLE documents_fts USING fts5(
                ocr_text, sheet_text, full_name, licence_no, licence_no_folded,
                address, jurisdiction,
                content='documents', content_rowid='id'
            );
            """
        )
        conn.execute(
            "INSERT INTO documents(source, sheet, row, image_path, ocr_text, "
            "full_name, licence_no, warnings, indexed_at) "
            "VALUES ('s.xlsx','S',1,NULL,'','LEGACY','11234562','[]','2020-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        with TessyIndex(path) as idx:
            doc = idx.all_documents()[0]
            assert "reviewed_at" in doc
            updated = idx.update_fields(doc["id"], {"licence_no": "I1234562"})
            assert updated["reviewed_at"]
