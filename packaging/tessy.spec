# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Tessy desktop app.

Produces a double-clickable application:

* macOS   -> Tessy.app bundle
* Windows -> Tessy.exe (one file)
* Linux   -> tessy single-file executable

Language data is always bundled, because a Tesseract install without it reports
"0 languages" and every OCR call fails - exactly the trap this project exists to
avoid. The Tesseract binary itself is bundled when one can be found at build
time, so the app runs with nothing else installed; set TESSY_BUNDLE_TESSERACT=0
to build an app that uses whatever Tesseract the operator already has.

Build with scripts/build_app.sh (or `pyinstaller packaging/tessy.spec`).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Locate the project regardless of where pyinstaller was invoked from.
# --------------------------------------------------------------------------
SPEC_DIR = Path(SPECPATH).resolve()
PROJECT = SPEC_DIR.parent
SRC = PROJECT / "src"

BUNDLE_TESSERACT = os.environ.get("TESSY_BUNDLE_TESSERACT", "1") != "0"
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

binaries: list[tuple[str, str]] = []
datas: list[tuple[str, str]] = []


# --------------------------------------------------------------------------
# Language data
# --------------------------------------------------------------------------
def find_tessdata_dir() -> Path | None:
    """Locate a tessdata directory holding at least eng.traineddata."""
    explicit = os.environ.get("TESSY_TESSDATA")
    candidates = [Path(explicit)] if explicit else []

    prefix = os.environ.get("TESSDATA_PREFIX")
    if prefix:
        candidates += [Path(prefix), Path(prefix) / "tessdata"]

    binary = shutil.which("tesseract") or os.environ.get("TESSERACT_BIN")
    if binary:
        root = Path(binary).resolve().parent.parent
        candidates.append(root / "share" / "tessdata")

    if shutil.which("brew"):
        try:
            prefix = subprocess.run(
                ["brew", "--prefix"], capture_output=True, text=True, check=False
            ).stdout.strip()
            if prefix:
                candidates.append(Path(prefix) / "share" / "tessdata")
        except OSError:
            pass

    candidates += [
        Path("/usr/local/share/tessdata"),
        Path("/usr/share/tesseract-ocr/5/tessdata"),
        Path("/usr/share/tesseract-ocr/4.00/tessdata"),
        Path("/usr/share/tessdata"),
        Path("/opt/homebrew/share/tessdata"),
        Path("/usr/local/Cellar"),
        Path("C:/Program Files/Tesseract-OCR/tessdata"),
    ]

    for candidate in candidates:
        try:
            if candidate.is_dir() and (candidate / "eng.traineddata").is_file():
                return candidate
        except OSError:
            continue
    return None


tessdata = find_tessdata_dir()
if tessdata is not None:
    for model in tessdata.glob("*.traineddata"):
        datas.append((str(model), "tessdata"))

    # The configs/ directory is NOT optional. Tesseract reads its output-format
    # settings from there, and asking for "tsv" without configs/tsv does not
    # fail - it silently emits plain text and exits 0, so the TSV parser finds
    # no words and every document comes back empty at 0.0 confidence.
    config_count = 0
    for sub in ("configs", "tessconfigs"):
        source = tessdata / sub
        if source.is_dir():
            for item in source.iterdir():
                if item.is_file():
                    datas.append((str(item), f"tessdata/{sub}"))
                    config_count += 1

    models = len([d for d in datas if d[1] == "tessdata"])
    print(
        f"[tessy.spec] bundling {models} language model(s) and {config_count} "
        f"config file(s) from {tessdata}"
    )
    if config_count == 0:
        print(
            "[tessy.spec] WARNING: no tessdata/configs found - TSV output will "
            "silently degrade to plain text and OCR will return nothing."
        )
else:
    print(
        "[tessy.spec] WARNING: no tessdata found. The packaged app will need "
        "Tesseract language data installed on the target machine."
    )


# --------------------------------------------------------------------------
# Tesseract binary and its libraries
# --------------------------------------------------------------------------
# Never ship the C runtime: it must match the host kernel/loader, and bundling
# it is the classic cause of "version `GLIBC_2.x' not found" on other distros.
SYSTEM_LIBS = (
    "libc.so", "libm.so", "libpthread.so", "libdl.so", "librt.so",
    "ld-linux", "libresolv.so", "libgcc_s.so", "libstdc++.so",
)


def shared_libs_of(binary: Path) -> list[Path]:
    """Non-system shared libraries a binary needs (Linux/macOS)."""
    out: list[Path] = []
    if IS_WIN:
        return out

    tool = ["otool", "-L", str(binary)] if IS_MAC else ["ldd", str(binary)]
    try:
        text = subprocess.run(tool, capture_output=True, text=True, check=False).stdout
    except OSError:
        return out

    for line in text.splitlines():
        line = line.strip()
        path = None
        if IS_MAC:
            token = line.split(" (")[0].strip()
            if token.startswith("/") and "libSystem" not in token:
                path = Path(token)
        elif "=>" in line:
            token = line.split("=>")[1].split("(")[0].strip()
            if token.startswith("/"):
                path = Path(token)

        if path is None or not path.is_file():
            continue
        if any(marker in path.name for marker in SYSTEM_LIBS):
            continue
        out.append(path)
    return out


if BUNDLE_TESSERACT:
    binary_path = os.environ.get("TESSERACT_BIN") or shutil.which("tesseract")
    if binary_path and Path(binary_path).is_file():
        binary_path = Path(binary_path).resolve()
        binaries.append((str(binary_path), "tesseract"))

        if IS_WIN:
            # There is no ldd on Windows, and tesseract.exe needs the DLLs that
            # sit beside it (leptonica, libpng, zlib, ...). Take the lot: the
            # installer directory is self-consistent by construction.
            for dll in binary_path.parent.glob("*.dll"):
                binaries.append((str(dll), "tesseract"))
        else:
            for lib in shared_libs_of(binary_path):
                binaries.append((str(lib), "tesseract"))

        shipped = len([b for b in binaries if b[1] == "tesseract"]) - 1
        print(f"[tessy.spec] bundling tesseract from {binary_path} (+{shipped} libraries)")
    else:
        print("[tessy.spec] no tesseract binary found; the app will look for one at runtime")


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
a = Analysis(
    # Deliberately the launcher, not gui/app.py: PyInstaller runs the entry
    # script as __main__, which has no parent package, so the package's
    # relative imports would fail at startup.
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "PIL._tkinter_finder",  # ImageTk needs this or the preview fails when frozen
        "openpyxl.cell._writer",
        "tessy",
        "tessy.gui.app",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Nothing in Tessy uses these; excluding them roughly halves the bundle.
        "numpy", "scipy", "matplotlib", "pandas", "pytest", "IPython",
        "notebook", "sphinx", "setuptools", "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

if IS_MAC:
    # A .app is a directory bundle, so onedir is the right shape here.
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="Tessy",
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,  # lets a dropped file arrive as argv
        target_arch=None,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Tessy")
    app = BUNDLE(
        coll,
        name="Tessy.app",
        bundle_identifier="com.evestigate.tessy",
        info_plist={
            "CFBundleName": "Tessy",
            "CFBundleDisplayName": "Tessy",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            # No camera/mic/network usage descriptions: the app uses none.
        },
    )
else:
    # One file on Windows and Linux: a single thing to double-click or hand over.
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="Tessy" if IS_WIN else "tessy",
        console=False,
        disable_windowed_traceback=False,
        upx=False,
    )
