"""Tests for folder-of-images ingest and spreadsheet export."""

from __future__ import annotations

import openpyxl
import pytest

from tessy.export_sheet import verified_from_doc, write_xlsx
from tessy.ingest import SpreadsheetError, load_image_folder, verified_licence_no_from_filename
from tessy.parse import apply_verified_licence_no, looks_like_driver_licence, parse_licence


class TestFilenameVerifiedId:
    def test_stem_is_the_licence_number(self, tmp_path):
        assert verified_licence_no_from_filename(tmp_path / "I1234562.png") == "I1234562"

    def test_strips_front_back_suffix(self, tmp_path):
        assert verified_licence_no_from_filename(tmp_path / "T4459981_front.jpg") == "T4459981"
        assert verified_licence_no_from_filename(tmp_path / "T4459981-back.PNG") == "T4459981"


class TestLoadImageFolder:
    def test_builds_one_record_per_image(self, tmp_path):
        (tmp_path / "I1234562.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "T4459981.jpg").write_bytes(b"\xff\xd8\xff")
        (tmp_path / "notes.txt").write_text("ignored")

        records = load_image_folder(tmp_path)
        assert len(records) == 2
        assert records[0].text["verified_licence_no"] == "I1234562"
        assert records[1].text["verified_licence_no"] == "T4459981"
        assert all(r.images for r in records)

    def test_empty_folder_raises(self, tmp_path):
        with pytest.raises(SpreadsheetError, match="No licence images"):
            load_image_folder(tmp_path)

    def test_missing_folder_raises(self, tmp_path):
        with pytest.raises(SpreadsheetError, match="Not a directory"):
            load_image_folder(tmp_path / "nope")


class TestVerifiedLicenceOverride:
    def test_filename_wins_over_ocr_and_warns_on_mismatch(self):
        fields = parse_licence("CALIFORNIA DRIVER LICENSE\nDL 11234562\nDOB 08/31/1977")
        apply_verified_licence_no(fields, "I1234562")
        assert fields.licence_no == "I1234562"
        assert any("filename" in w or "verified" in w for w in fields.warnings)

    def test_looks_like_driver_licence(self):
        assert looks_like_driver_licence("CALIFORNIA DRIVER LICENSE DL 123 DOB 01/01/1990")
        assert not looks_like_driver_licence("grocery receipt total $12.50")


class TestSpreadsheetExport:
    def test_writes_one_row_per_document(self, tmp_path):
        docs = [
            {
                "licence_no": "I1234562",
                "sheet_text": "verified_licence_no: I1234562 filename: I1234562.png",
                "full_name": "ALEXANDER J CARDHOLDER",
                "dob": "1977-08-31",
                "warnings": '["check me"]',
                "image_path": "/x/I1234562.png",
                "source": "/x",
                "sheet": "DL images",
                "row": 1,
            }
        ]
        out = write_xlsx(docs, tmp_path / "out.xlsx")
        wb = openpyxl.load_workbook(out)
        rows = list(wb.active.iter_rows(values_only=True))
        assert rows[0][0] == "Verified DL#"
        assert rows[1][0] == "I1234562"
        assert rows[1][2] == "ALEXANDER J CARDHOLDER"

    def test_verified_from_doc_parses_sheet_text(self):
        doc = {"sheet_text": "verified_licence_no: NY9988776 filename: NY9988776.png"}
        assert verified_from_doc(doc) == "NY9988776"
