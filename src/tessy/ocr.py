"""Thin, well-behaved wrapper around the `tesseract` binary.

We shell out to tesseract rather than binding libtesseract so that the pipeline
works against whatever build the operator installed (system package or the
from-source build produced by `scripts/build_tesseract.sh`).

TSV output is used instead of plain text because it carries per-word confidence,
which lets us flag low-quality scans for manual review instead of silently
indexing garbage.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LANG = "eng"
# PSM 6 = "assume a single uniform block of text". Licences are laid out as
# scattered fields, so 11 (sparse text) often wins; we try several and keep the
# most confident result.
DEFAULT_PSMS = (6, 11, 4)


class TesseractNotFound(RuntimeError):
    """Raised when the tesseract binary cannot be located."""


class TesseractFailed(RuntimeError):
    """Raised when tesseract exits non-zero."""


@dataclass(frozen=True)
class Word:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int
    block_num: int = 0
    par_num: int = 0
    line_num: int = 0

    @property
    def line_key(self) -> tuple[int, int, int]:
        """Identifies the text line this word belongs to."""
        return (self.block_num, self.par_num, self.line_num)


@dataclass
class OcrResult:
    """Outcome of a single OCR pass over one image."""

    text: str
    words: list[Word] = field(default_factory=list)
    psm: int | None = None
    source: str | None = None

    @property
    def mean_confidence(self) -> float:
        """Mean per-word confidence (0-100). 0.0 when no words were found."""
        scored = [w.confidence for w in self.words if w.confidence >= 0]
        if not scored:
            return 0.0
        return sum(scored) / len(scored)

    @property
    def word_count(self) -> int:
        return len(self.words)

    @property
    def visual_lines(self) -> list[str]:
        """Words regrouped by where they physically sit on the page.

        Tesseract's own line numbering cannot be trusted across page-segmentation
        modes: PSM 11 (sparse) emits every label and its value as separate
        "lines", which destroys label/value adjacency. Clustering on vertical
        position instead reproduces what a human sees as one row, identically
        under PSM 6 and PSM 11.
        """
        if not self.words:
            return []

        ordered = sorted(self.words, key=lambda w: w.top + w.height / 2)
        heights = sorted(w.height for w in self.words if w.height > 0)
        median_h = heights[len(heights) // 2] if heights else 10
        tolerance = max(median_h * 0.6, 4)

        rows: list[list[Word]] = []
        current: list[Word] = []
        anchor: float | None = None
        for word in ordered:
            centre = word.top + word.height / 2
            if anchor is None or abs(centre - anchor) <= tolerance:
                current.append(word)
                centres = [w.top + w.height / 2 for w in current]
                anchor = sum(centres) / len(centres)
            else:
                rows.append(current)
                current = [word]
                anchor = centre
        if current:
            rows.append(current)

        out: list[str] = []
        for row in rows:
            line = " ".join(w.text for w in sorted(row, key=lambda w: w.left)).strip()
            if line:
                out.append(line)
        return out

    @property
    def lines(self) -> list[str]:
        """Words regrouped into their original text lines.

        Field parsing is far more reliable per line than over one flat string,
        because a label and its value sit on the same line.
        """
        grouped: dict[tuple[int, int, int], list[Word]] = {}
        for word in self.words:
            grouped.setdefault(word.line_key, []).append(word)

        out: list[str] = []
        for key in sorted(grouped, key=lambda k: (k, min(w.top for w in grouped[k]))):
            ordered = sorted(grouped[key], key=lambda w: w.left)
            line = " ".join(w.text for w in ordered).strip()
            if line:
                out.append(line)
        return out


def find_tesseract() -> str:
    """Locate the tesseract binary.

    Honours $TESSERACT_BIN so an operator can point at a from-source build that
    is not first on PATH.
    """
    explicit = os.environ.get("TESSERACT_BIN")
    if explicit:
        if Path(explicit).is_file() and os.access(explicit, os.X_OK):
            return explicit
        raise TesseractNotFound(f"TESSERACT_BIN={explicit!r} is not an executable file")

    found = shutil.which("tesseract")
    if not found:
        raise TesseractNotFound(
            "tesseract not found on PATH. Build it with scripts/build_tesseract.sh "
            "or set TESSERACT_BIN to the binary."
        )
    return found


def tesseract_version() -> str:
    """Return the version string reported by the binary, e.g. '5.5.3'."""
    binary = find_tesseract()
    proc = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
    # tesseract writes its banner to stdout on 5.x, stderr on some 4.x builds.
    banner = proc.stdout or proc.stderr
    match = re.search(r"tesseract\s+v?([0-9][0-9.\w-]*)", banner, re.IGNORECASE)
    return match.group(1) if match else banner.strip().splitlines()[0]


def available_languages() -> list[str]:
    """Languages for which traineddata is installed."""
    binary = find_tesseract()
    proc = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, check=False)
    out = proc.stdout or proc.stderr
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # First line is a header like "List of available languages (3):"
    return [ln for ln in lines if not ln.lower().startswith("list of")]


def _int_field(row: dict[str, str], key: str) -> int:
    """Read an integer column from a TSV row, defaulting to 0."""
    try:
        return int(float(row.get(key) or 0))
    except ValueError:
        return 0


def _parse_tsv(tsv: str) -> list[Word]:
    """Turn tesseract's TSV output into Word records, dropping layout-only rows."""
    words: list[Word] = []
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf", "-1"))
        except ValueError:
            conf = -1.0
        # NaN must be rejected explicitly: every comparison against it is False,
        # so it would slip past the check below, poison mean_confidence, and make
        # the document invisible to the low-confidence review filter.
        if not math.isfinite(conf) or conf < 0:
            # Negative confidence marks a structural row (page/block/line), not a word.
            continue

        words.append(
            Word(
                text=text,
                confidence=conf,
                left=_int_field(row, "left"),
                top=_int_field(row, "top"),
                width=_int_field(row, "width"),
                height=_int_field(row, "height"),
                block_num=_int_field(row, "block_num"),
                par_num=_int_field(row, "par_num"),
                line_num=_int_field(row, "line_num"),
            )
        )
    return words


def run_once(
    image_path: str | Path,
    *,
    lang: str = DEFAULT_LANG,
    psm: int = 6,
    oem: int = 3,
    timeout: int = 120,
) -> OcrResult:
    """Run a single tesseract pass and return the parsed result."""
    image_path = Path(image_path)
    if not image_path.is_file():
        raise FileNotFoundError(f"No such image: {image_path}")

    binary = find_tesseract()
    cmd = [
        binary,
        str(image_path),
        "stdout",
        "-l",
        lang,
        "--psm",
        str(psm),
        "--oem",
        str(oem),
        "tsv",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise TesseractFailed(
            f"tesseract exited {proc.returncode} for {image_path}: {proc.stderr.strip()}"
        )

    words = _parse_tsv(proc.stdout)
    text = " ".join(w.text for w in words)
    return OcrResult(text=text, words=words, psm=psm, source=str(image_path))


def run_best(
    image_path: str | Path,
    *,
    lang: str = DEFAULT_LANG,
    psms: tuple[int, ...] = DEFAULT_PSMS,
    oem: int = 3,
    timeout: int = 120,
) -> OcrResult:
    """Try several page-segmentation modes and keep the most confident result.

    Licence layouts vary a lot between jurisdictions; no single PSM wins for all
    of them, and a wrong PSM can drop half the fields. Trying a few and scoring
    them is far cheaper than hand-tuning per document.
    """
    best: OcrResult | None = None
    errors: list[str] = []
    for psm in psms:
        try:
            result = run_once(image_path, lang=lang, psm=psm, oem=oem, timeout=timeout)
        except TesseractFailed as exc:
            errors.append(str(exc))
            continue
        # Score on total confident signal, not mean alone: a PSM that finds two
        # crisp words should not beat one that finds forty good ones.
        if best is None or _score(result) > _score(best):
            best = result

    if best is None:
        raise TesseractFailed(f"every PSM failed for {image_path}: " + " | ".join(errors))
    return best


def _score(result: OcrResult) -> float:
    return result.mean_confidence * (result.word_count**0.5)
