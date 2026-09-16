"""Tests for the tesseract wrapper.

TSV parsing and line reconstruction are tested without invoking tesseract;
the integration tests below are marked and skip when no binary is present.
"""

from __future__ import annotations

import pytest

from tessy.ocr import (
    OcrResult,
    TesseractNotFound,
    Word,
    _parse_tsv,
    find_tesseract,
    run_best,
    run_once,
)

TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def _tsv(*rows: str) -> str:
    return "\n".join([TSV_HEADER, *rows])


class TestTsvParsing:
    def test_keeps_words_and_drops_structural_rows(self):
        words = _parse_tsv(
            _tsv(
                "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",  # page row
                "5\t1\t1\t1\t1\t1\t10\t20\t50\t15\t96.4\tDOB",
                "5\t1\t1\t1\t1\t2\t70\t20\t90\t15\t93.1\t08/31/1977",
            )
        )
        assert [w.text for w in words] == ["DOB", "08/31/1977"]
        assert words[0].confidence == pytest.approx(96.4)

    def test_blank_text_is_ignored(self):
        assert _parse_tsv(_tsv("5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\t   ")) == []

    def test_malformed_confidence_is_treated_as_structural(self):
        assert _parse_tsv(_tsv("5\t1\t1\t1\t1\t1\t0\t0\t1\t1\tNaN\tX")) == []

    def test_empty_input(self):
        assert _parse_tsv("") == []


class TestOcrResult:
    def _word(self, text, conf=90.0, top=0, height=20, left=0):
        return Word(text=text, confidence=conf, left=left, top=top, width=30, height=height)

    def test_mean_confidence(self):
        result = OcrResult(text="", words=[self._word("A", 80), self._word("B", 100)])
        assert result.mean_confidence == pytest.approx(90.0)

    def test_mean_confidence_with_no_words_is_zero(self):
        assert OcrResult(text="").mean_confidence == 0.0

    def test_visual_lines_group_by_vertical_position(self):
        # "DL 1234" and "SEX M" sit on one visual row; "LN SMITH" on the next.
        words = [
            self._word("DL", top=100, left=10),
            self._word("1234", top=102, left=60),
            self._word("SEX", top=101, left=200),
            self._word("M", top=100, left=260),
            self._word("LN", top=150, left=10),
            self._word("SMITH", top=152, left=60),
        ]
        assert OcrResult(text="", words=words).visual_lines == [
            "DL 1234 SEX M",
            "LN SMITH",
        ]

    def test_visual_lines_order_by_horizontal_position(self):
        words = [
            self._word("SECOND", top=10, left=200),
            self._word("FIRST", top=10, left=10),
        ]
        assert OcrResult(text="", words=words).visual_lines == ["FIRST SECOND"]

    def test_visual_lines_empty_when_no_words(self):
        assert OcrResult(text="").visual_lines == []


class TestBinaryDiscovery:
    def test_explicit_bad_path_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TESSERACT_BIN", str(tmp_path / "not-here"))
        with pytest.raises(TesseractNotFound):
            find_tesseract()

    def test_missing_from_path_raises_with_guidance(self, monkeypatch):
        monkeypatch.delenv("TESSERACT_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(TesseractNotFound, match="build_tesseract.sh"):
            find_tesseract()


@pytest.mark.requires_tesseract
class TestRealOcr:
    def test_reads_a_synthetic_licence(self, licence_image):
        result = run_once(licence_image, psm=6)
        assert "CARDHOLDER" in result.text.upper()
        assert result.mean_confidence > 60

    def test_run_best_picks_a_usable_mode(self, licence_image):
        result = run_best(licence_image)
        assert result.psm in (6, 11, 4)
        assert result.word_count > 10
        assert result.mean_confidence > 70

    def test_visual_lines_pair_labels_with_values(self, licence_image):
        rows = run_best(licence_image).visual_lines
        joined = " | ".join(rows)
        # The label and its value must land on the same reconstructed row,
        # regardless of which PSM won.
        assert any("DL" in r and "1234562" in r for r in rows), joined

    def test_missing_image_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_once(tmp_path / "absent.png")
