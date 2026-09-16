"""Image clean-up applied before OCR.

Licence scans arrive at wildly different sizes and contrast levels. Tesseract is
trained on roughly 300-DPI text, so the single biggest win is upscaling small
crops; contrast normalisation and greyscaling are cheap and rarely hurt.

Deliberately PIL-only: adding OpenCV would roughly quadruple install size for
gains we cannot justify without sample data from the actual case.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

# Tesseract's sweet spot: capital letters around 30-35px tall. Licence text is
# small, so we target a generous minimum width for a full-card scan.
TARGET_MIN_WIDTH = 1600
MAX_UPSCALE = 4.0


def preprocess(
    src: str | Path,
    dst: str | Path,
    *,
    grayscale: bool = True,
    upscale: bool = True,
    autocontrast: bool = True,
    sharpen: bool = True,
) -> Path:
    """Write a cleaned-up copy of `src` to `dst` and return the destination path."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as img:
        img.load()
        out = img

        # Flatten transparency onto white; alpha confuses the binariser.
        if out.mode in ("RGBA", "LA", "P"):
            out = out.convert("RGBA")
            backdrop = Image.new("RGBA", out.size, (255, 255, 255, 255))
            out = Image.alpha_composite(backdrop, out).convert("RGB")

        if grayscale:
            out = out.convert("L")

        if upscale and out.width < TARGET_MIN_WIDTH:
            factor = min(TARGET_MIN_WIDTH / out.width, MAX_UPSCALE)
            new_size = (round(out.width * factor), round(out.height * factor))
            out = out.resize(new_size, Image.LANCZOS)

        if autocontrast:
            # Clip 1% off each tail so a dark scan background doesn't flatten the text.
            out = ImageOps.autocontrast(out.convert("L"), cutoff=1)

        if sharpen:
            out = out.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=3))

        out.save(dst)

    return dst


def binarize(src: str | Path, dst: str | Path, *, threshold: int | None = None) -> Path:
    """Hard black/white conversion — a fallback for low-contrast or noisy scans.

    With `threshold=None` the cut point is the mean intensity, which behaves like
    a cheap global Otsu and copes with evenly-lit scans.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as img:
        gray = img.convert("L")
        if threshold is None:
            histogram = gray.histogram()
            total = sum(histogram)
            threshold = int(sum(i * n for i, n in enumerate(histogram)) / total) if total else 128
        gray.point(lambda p, t=threshold: 255 if p > t else 0, mode="1").save(dst)

    return dst
