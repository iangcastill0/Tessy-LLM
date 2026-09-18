"""Extract structured licence fields from raw OCR text.

Two things make this harder than a plain regex sweep:

1. Labels themselves get misread. Real output from our own smoke test included
   ``poB`` for ``DOB``, ``cass`` for ``CLASS`` and ``Iss`` for ``ISS``. Exact
   label matching would silently drop those fields, so labels are matched
   fuzzily.
2. One visual row usually carries several label/value pairs
   (``DL 11234562 SEX M``), so a row is scanned token by token and split at each
   label it recognises rather than assumed to hold a single field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Canonical field -> label spellings seen on US/CA licences, including the
# AAMVA numeric codes printed on many of them.
LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "licence_no": ("DL", "DLN", "LIC", "LICENSE", "LICENCE", "NO", "4D", "IDN"),
    "last_name": ("LN", "LAST", "SURNAME", "1"),
    "first_name": ("FN", "FIRST", "GIVEN", "2"),
    "dob": ("DOB", "BIRTH", "BRTH", "BIRTHDATE", "3"),
    "expiry": ("EXP", "EXPIRES", "EXPIRY", "4B"),
    "issued": ("ISS", "ISSUED", "ISSUE", "4A"),
    "sex": ("SEX", "GENDER", "15"),
    "height": ("HGT", "HEIGHT", "16"),
    "weight": ("WGT", "WEIGHT", "17"),
    "eyes": ("EYES", "EYE", "18"),
    "hair": ("HAIR", "19"),
    "licence_class": ("CLASS", "CLS", "9"),
    "address": ("ADDRESS", "ADDR", "8"),
    "endorsements": ("END", "ENDORSEMENTS"),
    "restrictions": ("RSTR", "REST", "RESTRICTIONS"),
}

# Reverse lookup: normalised alias -> canonical field.
_ALIAS_TO_FIELD: dict[str, str] = {
    alias: field_name for field_name, aliases in LABEL_ALIASES.items() for alias in aliases
}

DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b")
DOC_TYPE_RE = re.compile(
    r"\b(DRIVER\s*LICEN[SC]E|DRIVERS?\s*LICEN[SC]E|IDENTIFICATION\s*CARD|ID\s*CARD)\b",
    re.IGNORECASE,
)
ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")

US_STATES = {
    "ALABAMA",
    "ALASKA",
    "ARIZONA",
    "ARKANSAS",
    "CALIFORNIA",
    "COLORADO",
    "CONNECTICUT",
    "DELAWARE",
    "FLORIDA",
    "GEORGIA",
    "HAWAII",
    "IDAHO",
    "ILLINOIS",
    "INDIANA",
    "IOWA",
    "KANSAS",
    "KENTUCKY",
    "LOUISIANA",
    "MAINE",
    "MARYLAND",
    "MASSACHUSETTS",
    "MICHIGAN",
    "MINNESOTA",
    "MISSISSIPPI",
    "MISSOURI",
    "MONTANA",
    "NEBRASKA",
    "NEVADA",
    "NEW HAMPSHIRE",
    "NEW JERSEY",
    "NEW MEXICO",
    "NEW YORK",
    "NORTH CAROLINA",
    "NORTH DAKOTA",
    "OHIO",
    "OKLAHOMA",
    "OREGON",
    "PENNSYLVANIA",
    "RHODE ISLAND",
    "SOUTH CAROLINA",
    "SOUTH DAKOTA",
    "TENNESSEE",
    "TEXAS",
    "UTAH",
    "VERMONT",
    "VIRGINIA",
    "WASHINGTON",
    "WEST VIRGINIA",
    "WISCONSIN",
    "WYOMING",
    "DISTRICT OF COLUMBIA",
}

# Glyph pairs Tesseract routinely swaps in alphanumeric identifiers. Folding an
# ID through this map gives a search key that matches however it was misread.
CONFUSABLE_FOLD = str.maketrans(
    {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "|": "1",
        "Z": "2",
        "S": "5",
        "B": "8",
        "G": "6",
        "T": "7",
        "A": "4",
    }
)

_STOPWORDS_FOR_LABEL = {"AND", "THE", "OF"}


def normalise_label(token: str) -> str:
    """Strip punctuation and case so ``poB:`` and ``DOB`` compare equal."""
    return re.sub(r"[^A-Z0-9]", "", token.upper())


def _edit_distance(a: str, b: str, *, cap: int) -> int:
    """Levenshtein distance, abandoned early once it exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1

    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i]
        for j, ch_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ch_a != ch_b),  # substitution
                )
            )
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


def match_label(token: str, *, max_distance: int | None = None) -> str | None:
    """Resolve a token to a canonical field name, tolerating OCR noise.

    Edit distance rather than a similarity ratio, because the ratio is not
    predictable at these token lengths: ``POB`` scores only 0.67 against ``DOB``
    and would be rejected by any cutoff high enough to be safe, yet it is plainly
    one substitution away.

    Two guards keep this from swallowing values:

    * tokens shorter than 3 characters must match exactly - a 2-letter label like
      ``LN`` sits one edit from far too many real values;
    * an ambiguous best match (tied with the runner-up, e.g. a misread that is
      one edit from both ``HGT`` and ``WGT``) is rejected rather than guessed.
    """
    key = normalise_label(token)
    if not key or key in _STOPWORDS_FOR_LABEL:
        return None
    if key in _ALIAS_TO_FIELD:
        return _ALIAS_TO_FIELD[key]
    if len(key) < 3:
        return None

    cap = max_distance if max_distance is not None else (1 if len(key) <= 4 else 2)

    best: tuple[int, str] | None = None
    runner_up: int | None = None
    for alias in _ALIAS_TO_FIELD:
        if len(alias) < 3:
            continue  # never fuzzy-match onto a 2-char or numeric alias
        distance = _edit_distance(key, alias, cap=cap)
        if distance > cap:
            continue
        if best is None or distance < best[0]:
            runner_up = best[0] if best else None
            best = (distance, alias)
        elif runner_up is None or distance < runner_up:
            runner_up = distance

    if best is None:
        return None
    # Reject ties: two different fields equally close means we do not know.
    if (
        runner_up is not None
        and runner_up == best[0]
        and _ALIAS_TO_FIELD[best[1]] != _resolve_tied(key, best[0])
    ):
        return None
    return _ALIAS_TO_FIELD[best[1]]


def _resolve_tied(key: str, distance: int) -> str | None:
    """Return the single field all equally-close aliases agree on, else None."""
    fields = {
        _ALIAS_TO_FIELD[alias]
        for alias in _ALIAS_TO_FIELD
        if len(alias) >= 3 and _edit_distance(key, alias, cap=distance) == distance
    }
    return fields.pop() if len(fields) == 1 else None


def fold_confusables(value: str) -> str:
    """Normalise an identifier into a glyph-confusion-insensitive search key."""
    return re.sub(r"[^A-Z0-9]", "", value.upper()).translate(CONFUSABLE_FOLD)


def normalise_date(value: str) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` string, or None when no date is present.

    Two-digit years are pivoted at 30: licences carry both long-past birth dates
    and near-future expiry dates, and 1930-2029 covers the realistic range.
    """
    iso = ISO_DATE_RE.search(value)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
    else:
        match = DATE_RE.search(value)
        if not match:
            return None
        month, day, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000 if year < 30 else 1900

    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


@dataclass
class LicenceFields:
    """Structured result of parsing one licence."""

    licence_no: str | None = None
    last_name: str | None = None
    first_name: str | None = None
    dob: str | None = None
    expiry: str | None = None
    issued: str | None = None
    sex: str | None = None
    height: str | None = None
    weight: str | None = None
    eyes: str | None = None
    hair: str | None = None
    licence_class: str | None = None
    address: str | None = None
    endorsements: str | None = None
    restrictions: str | None = None
    jurisdiction: str | None = None
    document_type: str | None = None
    raw_text: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def licence_no_folded(self) -> str | None:
        """Confusion-folded licence number, for lookups that survive OCR noise."""
        return fold_confusables(self.licence_no) if self.licence_no else None

    @property
    def full_name(self) -> str | None:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) if parts else None

    def completeness(self) -> float:
        """Fraction of the core identity fields that were recovered."""
        core = (
            self.licence_no,
            self.last_name,
            self.first_name,
            self.dob,
            self.expiry,
            self.jurisdiction,
        )
        return sum(1 for value in core if value) / len(core)

    def as_dict(self) -> dict[str, object]:
        return {
            "licence_no": self.licence_no,
            "licence_no_folded": self.licence_no_folded,
            "last_name": self.last_name,
            "first_name": self.first_name,
            "full_name": self.full_name,
            "dob": self.dob,
            "expiry": self.expiry,
            "issued": self.issued,
            "sex": self.sex,
            "height": self.height,
            "weight": self.weight,
            "eyes": self.eyes,
            "hair": self.hair,
            "licence_class": self.licence_class,
            "address": self.address,
            "endorsements": self.endorsements,
            "restrictions": self.restrictions,
            "jurisdiction": self.jurisdiction,
            "document_type": self.document_type,
            "completeness": round(self.completeness(), 3),
            "warnings": list(self.warnings),
        }


_DATE_FIELDS = {"dob", "expiry", "issued"}
_SHORT_CODE_FIELDS = {"sex", "licence_class", "eyes", "hair"}


def _clean_value(field_name: str, value: str) -> str | None:
    value = value.strip(" :;.,-\t")
    if not value:
        return None

    if field_name in _DATE_FIELDS:
        return normalise_date(value)

    if field_name == "sex":
        head = value.upper().lstrip()[:1]
        return head if head in {"M", "F", "X"} else None

    if field_name in _SHORT_CODE_FIELDS:
        token = value.split()[0].upper()
        token = re.sub(r"[^A-Z0-9]", "", token)
        # "Cc" is a doubled misread of a single-letter class code.
        if len(token) == 2 and token[0] == token[1]:
            token = token[0]
        return token or None

    if field_name == "licence_no":
        token = value.split()[0].upper()
        token = re.sub(r"[^A-Z0-9*-]", "", token)
        return token or None

    return re.sub(r"\s+", " ", value).strip() or None


def _consume_headers(rows: list[str], result: LicenceFields) -> list[str]:
    """Pull jurisdiction and document type off the card's header rows.

    Done before token-level parsing because the title line "DRIVER LICENSE"
    contains the word LICENSE, which is also a licence-number label; left to the
    token scanner it would be read as an empty licence number and the document
    type would be lost.
    """
    remaining: list[str] = []
    for row in rows:
        stripped = row.strip()
        upper = re.sub(r"[^A-Z ]", "", stripped.upper()).strip()

        if result.jurisdiction is None and upper in US_STATES:
            result.jurisdiction = upper
            continue

        doc_match = DOC_TYPE_RE.search(stripped)
        # Only treat it as the title when the row is essentially just the title,
        # so a row like "DL 1234 LICENSE CLASS C" still parses as fields.
        if result.document_type is None and doc_match:
            leftover = DOC_TYPE_RE.sub("", stripped).strip(" :,-")
            if len(leftover) <= 2:
                result.document_type = re.sub(r"\s+", " ", doc_match.group(0).upper())
                continue

        remaining.append(row)
    return remaining


def parse_rows(rows: list[str]) -> LicenceFields:
    """Parse pre-grouped visual rows (see ``OcrResult.visual_lines``)."""
    result = LicenceFields(raw_text="\n".join(rows))
    unlabelled: list[str] = []

    for row in _consume_headers(rows, result):
        tokens = row.split()
        if not tokens:
            continue

        current_field: str | None = None
        buffer: list[str] = []
        leading: list[str] = []

        def flush(target: str | None, words: list[str]) -> None:
            if not target or not words:
                return
            cleaned = _clean_value(target, " ".join(words))
            # First reading of a field wins; licences repeat codes in barcodes.
            if cleaned and getattr(result, target, None) is None:
                setattr(result, target, cleaned)

        for token in tokens:
            matched = match_label(token)
            if matched:
                flush(current_field, buffer)
                current_field, buffer = matched, []
            elif current_field:
                buffer.append(token)
            else:
                leading.append(token)
        flush(current_field, buffer)

        if leading:
            unlabelled.append(" ".join(leading))

    _infer_from_unlabelled(result, unlabelled)
    _add_warnings(result)
    return result


def _infer_from_unlabelled(result: LicenceFields, unlabelled: list[str]) -> None:
    """Recover jurisdiction, document type and address from rows without labels."""
    for chunk in unlabelled:
        upper = chunk.upper().strip()

        if result.jurisdiction is None and upper in US_STATES:
            result.jurisdiction = upper
            continue

        if result.document_type is None and DOC_TYPE_RE.search(upper):
            result.document_type = re.sub(r"\s+", " ", upper)
            continue

    # Address: prefer the chunk containing a ZIP, then join it with the street
    # line immediately above it, which is how licence addresses are laid out.
    if result.address is None:
        for index, chunk in enumerate(unlabelled):
            if ZIP_RE.search(chunk) and chunk.upper().strip() not in US_STATES:
                parts = []
                if index > 0:
                    prev = unlabelled[index - 1].strip()
                    if prev.upper() not in US_STATES and not DOC_TYPE_RE.search(prev):
                        parts.append(prev)
                parts.append(chunk.strip())
                result.address = ", ".join(p.strip(" ,") for p in parts if p.strip(" ,"))
                break


def _add_warnings(result: LicenceFields) -> None:
    """Flag anything a human reviewer should re-check by eye."""
    if (
        result.licence_no
        and re.fullmatch(r"[0-9]+", result.licence_no)
        and len(result.licence_no) >= 8
    ):
        result.warnings.append(
            "licence_no is all digits; leading letters are commonly misread "
            "(I->1, O->0) - verify against the image"
        )
    # A name carrying digits almost always means an adjacent label was misread
    # and its value got swept into the name. The raw value is kept rather than
    # trimmed - guessing where the name ends risks discarding a real one - but
    # the operator is told to look.
    for label in ("first_name", "last_name"):
        value = getattr(result, label)
        if value and any(ch.isdigit() for ch in value):
            result.warnings.append(
                f"{label} contains digits ({value!r}); an adjacent field was "
                "probably misread into it - verify against the image"
            )

    if not result.licence_no:
        result.warnings.append("no licence number found")
    if not result.dob:
        result.warnings.append("no date of birth found")
    if result.expiry and result.dob and result.expiry <= result.dob:
        result.warnings.append("expiry is not after date of birth - fields may be swapped")


def warnings_for_fields(fields: LicenceFields) -> list[str]:
    """Re-derive parser warnings from (possibly human-corrected) field values."""
    fields.warnings = []
    _add_warnings(fields)
    return list(fields.warnings)


def parse_licence(text_or_rows: str | list[str]) -> LicenceFields:
    """Parse licence fields from OCR output.

    Accepts either the reconstructed visual rows (preferred) or a raw string.
    """
    if isinstance(text_or_rows, str):
        rows = [ln for ln in text_or_rows.splitlines() if ln.strip()]
    else:
        rows = list(text_or_rows)
    return parse_rows(rows)
