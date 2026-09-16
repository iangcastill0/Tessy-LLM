"""Locating resources when Tessy runs as a frozen, double-clickable app.

PyInstaller unpacks bundled data to a temporary directory exposed as
``sys._MEIPASS``. Nothing here changes behaviour for a normal source checkout:
every lookup returns None when the process is not frozen, so the ordinary
install keeps using whatever Tesseract is on PATH.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TESSERACT_EXE = "tesseract.exe" if os.name == "nt" else "tesseract"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> Path | None:
    """Directory holding bundled resources, or None when not frozen."""
    if not is_frozen():
        return None
    return Path(sys._MEIPASS)  # type: ignore[attr-defined]


def bundled_tesseract() -> Path | None:
    """Path to a Tesseract binary shipped inside the bundle, if there is one."""
    root = resource_dir()
    if root is None:
        return None
    candidate = root / "tesseract" / TESSERACT_EXE
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def bundled_tessdata() -> Path | None:
    """Path to bundled language data, if any was shipped.

    Returns the tessdata directory itself: Tesseract 5 expects TESSDATA_PREFIX
    to name that directory, not its parent. Pointing at the parent makes it
    report a language called "tessdata/eng", which then never matches `-l eng`.
    """
    root = resource_dir()
    if root is None:
        return None
    candidate = root / "tessdata"
    if candidate.is_dir() and any(candidate.glob("*.traineddata")):
        return candidate
    return None


def subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for invoking Tesseract, pointing it at bundled resources.

    An operator's own TESSDATA_PREFIX is never overridden - if they have set it
    deliberately, that is the one to use.
    """
    env = dict(os.environ if base is None else base)

    tessdata = bundled_tessdata()
    if tessdata is not None and not env.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = str(tessdata)

    binary = bundled_tesseract()
    if binary is not None and os.name != "nt":
        # A bundled binary needs its bundled shared libraries; without this it
        # would pick up the host's libtesseract/liblept or fail to start.
        lib_dir = binary.parent
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}{os.pathsep}{existing}" if existing else str(lib_dir)

    return env


def describe() -> dict[str, str | None]:
    """Human-readable summary of what the bundle provides, for `doctor`."""
    return {
        "frozen": str(is_frozen()),
        "resources": str(resource_dir()) if resource_dir() else None,
        "bundled_tesseract": str(bundled_tesseract()) if bundled_tesseract() else None,
        "bundled_tessdata": str(bundled_tessdata()) if bundled_tessdata() else None,
    }
