"""Generates synthetic licence-shaped images for testing.

These are deliberately plain: black text on a white card, no seals, holograms,
microprint or other security features. They exist solely to give the OCR and
parsing layers a document with known ground truth, so the test suite never needs
real licence scans (which are PII and must not live in the repo).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)
BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
)


@dataclass(frozen=True)
class LicenceSpec:
    """Ground truth for one synthetic licence."""

    state: str = "CALIFORNIA"
    licence_no: str = "I1234562"
    last_name: str = "CARDHOLDER"
    first_name: str = "ALEXANDER J"
    address_1: str = "2570 24TH STREET"
    address_2: str = "ANYTOWN, CA 95818"
    dob: str = "08/31/1977"
    expiry: str = "08/31/2028"
    issued: str = "08/31/2024"
    sex: str = "M"
    height: str = "5-08"
    eyes: str = "BRN"
    licence_class: str = "C"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _font(paths: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    for path in paths:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError(f"No usable TrueType font found among {paths}")


def render_licence(
    dst: str | Path,
    spec: LicenceSpec | None = None,
    *,
    width: int = 1012,
    height: int = 638,
    scale: float = 1.0,
) -> Path:
    """Render `spec` to an image at `dst` and return the path.

    Default size approximates a CR80 card scanned at 300 DPI.
    """
    spec = spec or LicenceSpec()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    w, h = int(width * scale), int(height * scale)
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)

    s = lambda v: int(v * scale)  # noqa: E731 - local scaling shorthand
    title_font = _font(BOLD_CANDIDATES, s(40))
    sub_font = _font(BOLD_CANDIDATES, s(26))
    label_font = _font(BOLD_CANDIDATES, s(20))
    value_font = _font(FONT_CANDIDATES, s(26))

    draw.rectangle([(0, 0), (w - 1, h - 1)], outline="black", width=s(3))
    draw.text((s(40), s(28)), spec.state, font=title_font, fill="black")
    draw.text((s(40), s(80)), "DRIVER LICENSE", font=sub_font, fill="black")
    draw.line([(s(40), s(120)), (w - s(40), s(120))], fill="black", width=s(2))

    # Label/value pairs down the left column.
    rows: list[tuple[str, str]] = [
        ("DL", spec.licence_no),
        ("LN", spec.last_name),
        ("FN", spec.first_name),
        ("", spec.address_1),
        ("", spec.address_2),
        ("DOB", spec.dob),
        ("EXP", spec.expiry),
        ("ISS", spec.issued),
    ]
    y = s(145)
    for label, value in rows:
        if label:
            draw.text((s(40), y + s(6)), label, font=label_font, fill="black")
        draw.text((s(120), y), value, font=value_font, fill="black")
        y += s(48)

    # Right-hand column of short attributes.
    right_x = s(600)
    ry = s(145)
    for label, value in (
        ("SEX", spec.sex),
        ("HGT", spec.height),
        ("EYES", spec.eyes),
        ("CLASS", spec.licence_class),
    ):
        draw.text((right_x, ry + s(6)), label, font=label_font, fill="black")
        draw.text((right_x + s(110), ry), value, font=value_font, fill="black")
        ry += s(48)

    img.save(dst)
    return dst
