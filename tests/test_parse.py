"""Unit tests for licence field parsing. No tesseract needed."""

from __future__ import annotations

import pytest

from tessy.parse import (
    LicenceFields,
    fold_confusables,
    match_label,
    normalise_date,
    parse_licence,
)

# Exactly what our tesseract build produced for the synthetic CA licence,
# including its misreadings. Regression-locking the real noise is the point.
NOISY_CA_ROWS = [
    "CALIFORNIA",
    "DRIVER LICENSE",
    "DL 11234562 SEX M",
    "LN CARDHOLDER HGT 5-08",
    "FN ALEXANDER J EYES BRN",
    "2570 24TH STREET cass C",
    "ANYTOWN, CA 95818",
    "poB 08/31/1977",
    "Exp 08/31/2028",
    "Iss 08/31/2024",
]


class TestLabelMatching:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("DL", "licence_no"),
            ("LN", "last_name"),
            ("FN", "first_name"),
            ("DOB", "dob"),
            ("EXP", "expiry"),
            ("SEX", "sex"),
            ("CLASS", "licence_class"),
        ],
    )
    def test_exact_labels(self, token, expected):
        assert match_label(token) == expected

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("poB", "dob"),  # observed tesseract misread
            ("cass", "licence_class"),
            ("Iss", "issued"),
            ("DOB:", "dob"),  # trailing punctuation
            ("exp.", "expiry"),
        ],
    )
    def test_noisy_labels_still_match(self, token, expected):
        assert match_label(token) == expected

    def test_unknown_token_is_not_a_label(self):
        assert match_label("CARDHOLDER") is None
        assert match_label("95818") is None

    def test_short_tokens_require_exact_match(self):
        # "XN" is one edit from "LN" but must not be accepted, or stray
        # two-letter values would be swallowed as labels.
        assert match_label("XN") is None


class TestDateNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("08/31/1977", "1977-08-31"),
            ("8/3/1977", "1977-08-03"),
            ("08-31-1977", "1977-08-31"),
            ("1977-08-31", "1977-08-31"),
            ("DOB 08/31/1977", "1977-08-31"),
        ],
    )
    def test_valid_dates(self, raw, expected):
        assert normalise_date(raw) == expected

    def test_two_digit_year_pivot(self):
        assert normalise_date("08/31/77") == "1977-08-31"
        assert normalise_date("08/31/28") == "2028-08-31"

    @pytest.mark.parametrize("raw", ["", "no date here", "13/45/2020", "99/99/9999"])
    def test_rejects_nonsense(self, raw):
        assert normalise_date(raw) is None


class TestConfusableFolding:
    def test_letter_digit_confusions_collapse(self):
        assert fold_confusables("I1234562") == fold_confusables("11234562")
        assert fold_confusables("O5551234") == fold_confusables("05551234")
        assert fold_confusables("S123") == fold_confusables("5123")

    def test_punctuation_and_case_ignored(self):
        assert fold_confusables("t-4459981") == fold_confusables("T4459981")

    def test_distinct_ids_stay_distinct(self):
        assert fold_confusables("A1234567") != fold_confusables("X1234567")


class TestParseLicence:
    @pytest.fixture
    def parsed(self) -> LicenceFields:
        return parse_licence(NOISY_CA_ROWS)

    def test_extracts_core_identity_fields(self, parsed):
        assert parsed.last_name == "CARDHOLDER"
        assert parsed.first_name == "ALEXANDER J"
        assert parsed.full_name == "ALEXANDER J CARDHOLDER"
        assert parsed.licence_no == "11234562"

    def test_recovers_fields_behind_misread_labels(self, parsed):
        # These are the ones exact matching would have dropped.
        assert parsed.dob == "1977-08-31"
        assert parsed.issued == "2024-08-31"
        assert parsed.licence_class == "C"

    def test_multiple_fields_on_one_row(self, parsed):
        # "DL 11234562 SEX M" carries two fields.
        assert parsed.sex == "M"
        assert parsed.height == "5-08"
        assert parsed.eyes == "BRN"

    def test_header_rows_become_jurisdiction_and_doc_type(self, parsed):
        assert parsed.jurisdiction == "CALIFORNIA"
        assert parsed.document_type == "DRIVER LICENSE"

    def test_address_spans_two_rows(self, parsed):
        assert parsed.address == "2570 24TH STREET, ANYTOWN, CA 95818"

    def test_completeness_and_warning_on_digit_only_licence(self, parsed):
        assert parsed.completeness() == 1.0
        assert any("leading letters" in w for w in parsed.warnings)

    def test_accepts_plain_string_input(self):
        parsed = parse_licence("\n".join(NOISY_CA_ROWS))
        assert parsed.last_name == "CARDHOLDER"

    def test_empty_input_is_safe(self):
        parsed = parse_licence([])
        assert parsed.licence_no is None
        assert parsed.completeness() == 0.0
        assert "no licence number found" in parsed.warnings

    def test_first_reading_of_a_field_wins(self):
        rows = ["DL AAA111", "DL BBB222"]
        assert parse_licence(rows).licence_no == "AAA111"

    def test_doubled_class_letter_is_collapsed(self):
        assert parse_licence(["CLASS Cc"]).licence_class == "C"

    def test_invalid_sex_value_rejected(self):
        assert parse_licence(["SEX 8"]).sex is None
        assert parse_licence(["SEX F"]).sex == "F"

    def test_swapped_dates_are_flagged(self):
        parsed = parse_licence(["DL X1", "DOB 01/01/2030", "EXP 01/01/1990"])
        assert any("swapped" in w for w in parsed.warnings)

    def test_title_row_does_not_eat_licence_number(self):
        # "LICENSE" is both a doc-type word and a licence-number label.
        parsed = parse_licence(["DRIVER LICENSE", "DL T4459981"])
        assert parsed.document_type == "DRIVER LICENSE"
        assert parsed.licence_no == "T4459981"

    def test_as_dict_round_trips_key_fields(self, parsed):
        data = parsed.as_dict()
        assert data["full_name"] == "ALEXANDER J CARDHOLDER"
        assert data["licence_no_folded"] == fold_confusables("11234562")
        assert isinstance(data["warnings"], list)
