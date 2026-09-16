#!/usr/bin/env bash
#
# Build the double-clickable Tessy application for the CURRENT platform.
#
#   macOS   -> dist/Tessy.app
#   Linux   -> dist/tessy          (single file)
#   Windows -> dist/Tessy.exe      (run this from Git Bash, or use the CI job)
#
# PyInstaller cannot cross-compile: a macOS .app must be built on macOS and a
# Windows .exe on Windows. CI builds all three on native runners.
#
# By default the app bundles Tesseract and its language data, so the result runs
# on a machine with nothing installed. Set TESSY_BUNDLE_TESSERACT=0 to build a
# smaller app that uses the operator's own Tesseract.
#
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT="$PWD"
VENV="${VENV:-.venv}"
PY="$VENV/bin/python"
[ -x "$PY" ] || PY="$VENV/Scripts/python.exe"   # Windows layout

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || die "no virtualenv at $VENV - run 'make install-dev' first"

log "Checking build dependencies"
"$PY" -c "import PyInstaller" 2>/dev/null || {
  log "Installing PyInstaller"
  "$PY" -m pip install --quiet pyinstaller
}
"$PY" -c "import tkinter" 2>/dev/null || die \
  "tkinter is missing from $PY - install python3-tk (Debian/Ubuntu) or python3-tkinter (Fedora)"

if [ "${TESSY_BUNDLE_TESSERACT:-1}" != "0" ] && ! command -v tesseract >/dev/null \
   && [ -z "${TESSERACT_BIN:-}" ]; then
  warn "No tesseract found to bundle. The app will require one on the target machine."
  warn "Run scripts/build_tesseract.sh first for a fully self-contained build."
fi

log "Cleaning previous build"
rm -rf build/pyi dist

log "Building (this takes a couple of minutes)"
PYTHONPATH="$PROJECT/src" "$PY" -m PyInstaller packaging/tessy.spec \
  --noconfirm --distpath dist --workpath build/pyi --log-level WARN

# --------------------------------------------------------------------------
# Verify the artefact actually works. A build that produces a file which cannot
# OCR is worse than no build, and this has already caught one real packaging
# fault (missing tessdata/configs made every document come back empty).
# --------------------------------------------------------------------------
case "$(uname -s)" in
  Darwin) APP="dist/Tessy.app/Contents/MacOS/Tessy" ;;
  MINGW*|MSYS*|CYGWIN*) APP="dist/Tessy.exe" ;;
  *) APP="dist/tessy" ;;
esac

[ -e "$APP" ] || die "expected artefact not found at $APP"

log "Verifying the packaged app"
"$APP" --doctor || die "packaged app failed its diagnostics"
"$APP" --selftest || die "packaged app could not perform OCR"

SIZE="$(du -sh "$(ls -d dist/Tessy.app 2>/dev/null || echo "$APP")" | cut -f1)"
cat <<EOF

Built and verified: $APP  ($SIZE)

Hand it over as-is, or zip it:
    cd dist && zip -r Tessy-$(uname -s | tr '[:upper:]' '[:lower:]').zip $(basename "$(ls -d Tessy.app 2>/dev/null || basename "$APP")")

The person receiving it needs nothing installed - no Python, no Tesseract.
On first run they can confirm it works with:
    $APP --selftest
EOF
