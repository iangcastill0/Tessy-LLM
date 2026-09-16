"""Tests for the command line interface."""

from __future__ import annotations

import json

import pytest

from tessy.cli import main
from tessy.index import TessyIndex


@pytest.fixture
def populated_db(tmp_path):
    db = tmp_path / "case.db"
    with TessyIndex(db) as index:
        index.add_document(
            source="/cases/a.xlsx",
            sheet="Licences",
            row=2,
            image_path="/cases/img2.png",
            image_origin="embedded",
            ocr_text="CALIFORNIA DRIVER LICENSE DL 11234562 CARDHOLDER",
            ocr_confidence=94.2,
            ocr_psm=11,
            sheet_text="Case Ref: CASE-1002",
            fields={
                "licence_no": "11234562",
                "licence_no_folded": "11234562",
                "last_name": "CARDHOLDER",
                "first_name": "ALEXANDER J",
                "full_name": "ALEXANDER J CARDHOLDER",
                "dob": "1977-08-31",
                "jurisdiction": "CALIFORNIA",
                "completeness": 1.0,
                "warnings": ["licence_no is all digits"],
            },
        )
        index.add_document(
            source="/cases/a.xlsx",
            sheet="Licences",
            row=3,
            image_path="/cases/img3.png",
            ocr_text="TEXAS RIVERA",
            ocr_confidence=52.0,
            fields={
                "full_name": "MARIA L RIVERA",
                "licence_no": "T4459981",
                "jurisdiction": "TEXAS",
                "warnings": [],
            },
        )
    return db


class TestSearchCommand:
    def test_finds_and_prints_a_subject(self, populated_db, capsys):
        assert main(["search", "CARDHOLDER", "--db", str(populated_db)]) == 0
        out = capsys.readouterr().out
        assert "ALEXANDER J CARDHOLDER" in out
        assert "1977-08-31" in out

    def test_json_output_is_machine_readable(self, populated_db, capsys):
        assert main(["search", "RIVERA", "--db", str(populated_db), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["full_name"] == "MARIA L RIVERA"

    def test_multi_word_query_is_joined(self, populated_db, capsys):
        main(["search", "MARIA", "RIVERA", "--db", str(populated_db)])
        assert "MARIA L RIVERA" in capsys.readouterr().out

    def test_no_match_is_not_an_error(self, populated_db, capsys):
        assert main(["search", "NOBODY", "--db", str(populated_db)]) == 0
        assert "No matches" in capsys.readouterr().out

    def test_limit_flag(self, populated_db, capsys):
        main(["search", "DRIVER", "--db", str(populated_db), "-n", "1", "--json"])
        assert len(json.loads(capsys.readouterr().out)) <= 1


class TestShowCommand:
    def test_prints_full_record(self, populated_db, capsys):
        with TessyIndex(populated_db) as index:
            doc_id = index.search("CARDHOLDER")[0].id
        assert main(["show", str(doc_id), "--db", str(populated_db)]) == 0
        assert json.loads(capsys.readouterr().out)["jurisdiction"] == "CALIFORNIA"

    def test_unknown_id_exits_nonzero(self, populated_db, capsys):
        assert main(["show", "4242", "--db", str(populated_db)]) == 1


class TestReviewCommand:
    def test_flags_low_confidence_and_warnings(self, populated_db, capsys):
        assert main(["review", "--db", str(populated_db)]) == 0
        out = capsys.readouterr().out
        assert "MARIA L RIVERA" in out  # 52.0 confidence
        assert "licence_no is all digits" in out  # parser warning

    def test_threshold_is_configurable(self, populated_db, capsys):
        main(["review", "--db", str(populated_db), "--min-confidence", "0"])
        # Only the warning-bearing record should remain.
        assert "MARIA L RIVERA" not in capsys.readouterr().out


class TestStatsAndExport:
    def test_stats_reports_totals(self, populated_db, capsys):
        assert main(["stats", "--db", str(populated_db)]) == 0
        assert "documents" in capsys.readouterr().out

    def test_csv_export_has_header_and_rows(self, populated_db, capsys):
        assert main(["export", "--db", str(populated_db), "--format", "csv"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("id,source,sheet,row")
        assert len(lines) == 3  # header + 2 documents

    def test_json_export_round_trips(self, populated_db, capsys):
        main(["export", "--db", str(populated_db), "--format", "json"])
        assert len(json.loads(capsys.readouterr().out)) == 2

    def test_export_to_file(self, populated_db, tmp_path, capsys):
        target = tmp_path / "out.csv"
        main(["export", "--db", str(populated_db), "-o", str(target)])
        assert target.read_text().count("\n") == 3


class TestDoctorCommand:
    @pytest.mark.requires_tesseract
    def test_reports_a_healthy_install(self, capsys):
        assert main(["doctor"]) == 0
        assert "All checks passed" in capsys.readouterr().out

    def test_reports_missing_binary(self, monkeypatch, capsys):
        monkeypatch.delenv("TESSERACT_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert main(["doctor"]) == 1
        assert "FAIL" in capsys.readouterr().err


class TestArgumentHandling:
    def test_no_command_is_a_usage_error(self):
        with pytest.raises(SystemExit):
            main([])

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "tessy" in capsys.readouterr().out


@pytest.mark.requires_tesseract
class TestIngestCommand:
    def test_ingest_then_search(self, workbook_with_embedded_images, tmp_path, capsys):
        db = tmp_path / "cli.db"
        assert (
            main(
                [
                    "ingest",
                    str(workbook_with_embedded_images),
                    "--db",
                    str(db),
                    "--workdir",
                    str(tmp_path / "w"),
                    "--quiet",
                ]
            )
            == 0
        )
        assert "documents indexed: 2" in capsys.readouterr().out

        assert main(["search", "CARDHOLDER", "--db", str(db)]) == 0
        assert "ALEXANDER J CARDHOLDER" in capsys.readouterr().out
