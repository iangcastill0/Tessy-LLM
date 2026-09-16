"""End-to-end tests: spreadsheet in, searchable index out."""

from __future__ import annotations

import openpyxl
import pytest

from tessy.index import TessyIndex
from tessy.pipeline import process_spreadsheet


@pytest.mark.requires_tesseract
class TestFullRun:
    @pytest.fixture
    def run(self, workbook_with_embedded_images, tmp_path):
        db = tmp_path / "case.db"
        report = process_spreadsheet(workbook_with_embedded_images, db, workdir=tmp_path / "work")
        return report, db

    def test_every_row_is_processed(self, run):
        report, _ = run
        assert report.rows == 2
        assert report.images == 2
        assert report.indexed == 2
        assert report.failures == []

    def test_ocr_quality_is_usable(self, run):
        report, _ = run
        assert report.mean_confidence > 70

    def test_fields_are_extracted_and_indexed(self, run):
        _, db = run
        with TessyIndex(db) as index:
            hit = index.search("CARDHOLDER")[0]
            assert hit.full_name == "ALEXANDER J CARDHOLDER"
            assert hit.dob == "1977-08-31"
            assert hit.jurisdiction == "CALIFORNIA"

    def test_second_subject_is_distinct(self, run):
        _, db = run
        with TessyIndex(db) as index:
            hit = index.search("RIVERA")[0]
            assert hit.jurisdiction == "TEXAS"
            assert hit.dob == "1985-02-14"

    def test_spreadsheet_text_is_searchable_alongside_ocr(self, run):
        _, db = run
        with TessyIndex(db) as index:
            assert len(index.search("CASE-2002")) == 1

    def test_licence_number_survives_glyph_confusion(self, run):
        _, db = run
        with TessyIndex(db) as index:
            # True number is I1234562; OCR may record a leading digit 1.
            assert len(index.search("I1234562")) == 1

    def test_rerunning_does_not_duplicate(self, workbook_with_embedded_images, tmp_path):
        db = tmp_path / "case.db"
        for _ in range(2):
            process_spreadsheet(workbook_with_embedded_images, db, workdir=tmp_path / "work")
        with TessyIndex(db) as index:
            assert index.stats()["documents"] == 2

    def test_image_paths_are_recorded_for_provenance(self, run):
        _, db = run
        with TessyIndex(db) as index:
            for doc in index.all_documents():
                assert doc["image_path"]
                assert doc["image_origin"] == "embedded"


@pytest.mark.requires_tesseract
class TestResilience:
    def test_unreadable_image_does_not_abort_the_run(self, tmp_path):
        """One corrupt scan must not cost the operator the other 499 rows."""
        from synthetic import render_licence

        good = render_licence(tmp_path / "good.png")
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image at all")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Case Ref", "Scan"])
        ws.append(["CASE-1", str(bad)])
        ws.append(["CASE-2", str(good)])
        sheet = tmp_path / "mixed.xlsx"
        wb.save(sheet)

        report = process_spreadsheet(sheet, tmp_path / "case.db", workdir=tmp_path / "w")

        assert len(report.failures) == 1
        assert report.indexed == 1  # the good one still got through
        with TessyIndex(tmp_path / "case.db") as index:
            assert len(index.search("CARDHOLDER")) == 1

    def test_row_without_an_image_is_still_indexed_on_its_text(self, tmp_path):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Case Ref", "Notes"])
        ws.append(["CASE-7001", "No licence supplied by subject"])
        sheet = tmp_path / "textonly.xlsx"
        wb.save(sheet)

        report = process_spreadsheet(sheet, tmp_path / "case.db", workdir=tmp_path / "w")

        assert report.text_only_rows == 1
        with TessyIndex(tmp_path / "case.db") as index:
            assert len(index.search("CASE-7001")) == 1


@pytest.mark.requires_tesseract
def test_report_summary_mentions_counts(workbook_with_embedded_images, tmp_path):
    report = process_spreadsheet(
        workbook_with_embedded_images, tmp_path / "c.db", workdir=tmp_path / "w"
    )
    summary = report.summary()
    assert "rows read" in summary
    assert "documents indexed" in summary
