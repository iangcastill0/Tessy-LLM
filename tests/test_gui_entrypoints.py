"""Tests for the desktop app's non-windowing entry points.

--doctor and --selftest are what an operator is asked to run when a packaged
app misbehaves on a machine nobody can inspect, so they must work without a
display and report honestly.
"""

from __future__ import annotations

import pytest

from tessy.gui.app import main, run_doctor, run_selftest


class TestVersionAndHelp:
    def test_version(self, capsys):
        assert main(["--version"]) == 0
        assert "tessy" in capsys.readouterr().out

    def test_help(self, capsys):
        assert main(["--help"]) == 0
        out = capsys.readouterr().out
        assert "--doctor" in out and "--selftest" in out


class TestDoctor:
    @pytest.mark.requires_tesseract
    def test_reports_a_healthy_install(self, capsys):
        assert run_doctor() == 0
        out = capsys.readouterr().out
        assert "tesseract version" in out
        assert "All checks passed" in out

    @pytest.mark.requires_tesseract
    def test_reports_bundle_state(self, capsys):
        run_doctor()
        # Not frozen in a source checkout, and it must say so rather than lie.
        assert "frozen" in capsys.readouterr().out

    def test_missing_tesseract_is_a_failure(self, monkeypatch, capsys):
        monkeypatch.delenv("TESSERACT_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert run_doctor() == 1
        assert "NOT FOUND" in capsys.readouterr().out

    def test_no_languages_is_a_failure(self, monkeypatch, capsys):
        monkeypatch.setattr("tessy.ocr.available_languages", lambda: [])
        monkeypatch.setattr("tessy.ocr.find_tesseract", lambda: "/usr/bin/tesseract")
        monkeypatch.setattr("tessy.ocr.tesseract_version", lambda: "5.5.3")
        assert run_doctor() == 1
        assert "No language data" in capsys.readouterr().out

    def test_routed_from_main(self, monkeypatch):
        called: list[bool] = []
        monkeypatch.setattr("tessy.gui.app.run_doctor", lambda: called.append(True) or 0)
        assert main(["--doctor"]) == 0
        assert called


class TestSelftest:
    @pytest.mark.requires_tesseract
    def test_text_round_trips(self, capsys):
        assert run_selftest() == 0
        out = capsys.readouterr().out
        assert "TESSY SELFTEST 12345" in out
        assert "Self-test passed" in out

    @pytest.mark.requires_tesseract
    def test_keeps_the_probe_when_asked(self, tmp_path, monkeypatch):
        """TESSY_SELFTEST_DIR exists so a failing build can be inspected."""
        monkeypatch.setenv("TESSY_SELFTEST_DIR", str(tmp_path))
        run_selftest()
        assert (tmp_path / "selftest.png").is_file()

    def test_ocr_failure_is_reported_not_raised(self, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise RuntimeError("tesseract exploded")

        monkeypatch.setattr("tessy.ocr.run_best", boom)
        assert run_selftest() == 1
        assert "SELFTEST FAILED" in capsys.readouterr().out

    def test_mismatched_text_fails(self, monkeypatch, capsys):
        from tessy.ocr import OcrResult

        monkeypatch.setattr(
            "tessy.ocr.run_best",
            lambda *a, **k: OcrResult(text="SOMETHING ELSE", words=[], psm=6),
        )
        assert run_selftest() == 1
        out = capsys.readouterr().out
        assert "did not round-trip" in out

    def test_routed_from_main(self, monkeypatch):
        monkeypatch.setattr("tessy.gui.app.run_selftest", lambda: 0)
        assert main(["--selftest"]) == 0
