#!/usr/bin/env bash
#
# Build Tesseract OCR from source and install the language data it needs.
#
# Building from source gets you the binary but NOT the trained language models;
# a fresh build reports "0 languages" and every OCR call fails. This script
# installs the models too, which is the step most instructions omit.
#
# Usage:
#   scripts/build_tesseract.sh                     # build 5.5.3 into /usr/local
#   TESSERACT_VERSION=5.5.0 scripts/build_tesseract.sh
#   PREFIX="$HOME/.local" scripts/build_tesseract.sh   # no sudo needed
#   TESSDATA_LANGS="eng osd deu" scripts/build_tesseract.sh
#
set -euo pipefail

TESSERACT_VERSION="${TESSERACT_VERSION:-5.5.3}"
PREFIX="${PREFIX:-/usr/local}"
BUILD_DIR="${BUILD_DIR:-${TMPDIR:-/tmp}/tesseract-build}"
TESSDATA_LANGS="${TESSDATA_LANGS:-eng osd}"
# "best" = most accurate (~15MB/lang); "fast" = quickest (~2MB/lang).
TESSDATA_FLAVOUR="${TESSDATA_FLAVOUR:-best}"
JOBS="${JOBS:-$( (command -v nproc >/dev/null && nproc) || sysctl -n hw.ncpu 2>/dev/null || echo 2)}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  case "$PREFIX" in
    "$HOME"/*) SUDO="" ;;
    *) command -v sudo >/dev/null && SUDO="sudo" || die "need root or sudo to install into $PREFIX" ;;
  esac
fi

# --------------------------------------------------------------------------
# 1. Dependencies
# --------------------------------------------------------------------------
install_deps() {
  if [ "${SKIP_DEPS:-0}" = "1" ]; then
    log "SKIP_DEPS=1, not installing build dependencies"
    return
  fi

  if command -v apt-get >/dev/null; then
    log "Installing build dependencies (apt)"
    $SUDO apt-get update -qq
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq \
      build-essential pkg-config autoconf automake libtool autoconf-archive \
      libleptonica-dev libpng-dev libjpeg-turbo8-dev libtiff-dev zlib1g-dev \
      libwebp-dev libopenjp2-7-dev libgif-dev libarchive-dev libcurl4-openssl-dev \
      libicu-dev libpango1.0-dev libcairo2-dev git curl ca-certificates
  elif command -v dnf >/dev/null; then
    log "Installing build dependencies (dnf)"
    $SUDO dnf install -y gcc-c++ make pkgconf autoconf automake libtool \
      autoconf-archive leptonica-devel libpng-devel libjpeg-turbo-devel \
      libtiff-devel zlib-devel libwebp-devel openjpeg2-devel giflib-devel \
      libarchive-devel libcurl-devel libicu-devel pango-devel cairo-devel git curl
  elif command -v brew >/dev/null; then
    log "Installing build dependencies (homebrew)"
    brew install automake autoconf libtool pkg-config leptonica libarchive \
      icu4c pango cairo libpng jpeg libtiff webp giflib || true
  else
    warn "No supported package manager found; assuming dependencies are present."
    warn "You need: autotools, libtool, pkg-config and leptonica development headers."
  fi
}

# --------------------------------------------------------------------------
# 2. Build
# --------------------------------------------------------------------------
build_tesseract() {
  mkdir -p "$BUILD_DIR"
  local src="$BUILD_DIR/tesseract"

  if [ -d "$src/.git" ]; then
    log "Reusing existing clone at $src"
    git -C "$src" fetch --tags --quiet origin
  else
    log "Cloning tesseract-ocr/tesseract"
    git clone --quiet https://github.com/tesseract-ocr/tesseract.git "$src"
  fi

  log "Checking out $TESSERACT_VERSION"
  git -C "$src" checkout --quiet "$TESSERACT_VERSION"

  cd "$src"
  log "Running autogen"
  ./autogen.sh >/dev/null

  log "Configuring (prefix=$PREFIX)"
  ./configure --prefix="$PREFIX" --disable-static >/dev/null

  log "Compiling with $JOBS job(s) - this takes a few minutes"
  make "-j${JOBS}" >/dev/null

  log "Installing into $PREFIX"
  $SUDO make install >/dev/null
  if command -v ldconfig >/dev/null && [ "$(uname -s)" = "Linux" ]; then
    $SUDO ldconfig || true
  fi
}

# --------------------------------------------------------------------------
# 3. Language data - the step that is easy to miss
# --------------------------------------------------------------------------
install_tessdata() {
  local tessdata="$PREFIX/share/tessdata"
  $SUDO mkdir -p "$tessdata"

  local repo_best="https://github.com/tesseract-ocr/tessdata_best"
  local repo_fast="https://github.com/tesseract-ocr/tessdata_fast"
  local repo_std="https://github.com/tesseract-ocr/tessdata"

  for lang in $TESSDATA_LANGS; do
    if [ -s "$tessdata/$lang.traineddata" ]; then
      log "Language '$lang' already installed"
      continue
    fi

    # osd (orientation detection) only exists in the standard repo.
    local repo="$repo_best"
    case "$TESSDATA_FLAVOUR" in fast) repo="$repo_fast" ;; standard) repo="$repo_std" ;; esac
    [ "$lang" = "osd" ] && repo="$repo_std"

    log "Fetching $lang.traineddata from $(basename "$repo")"
    local tmp; tmp="$(mktemp -d)"
    if git clone --quiet --depth 1 --filter=blob:none --no-checkout "$repo" "$tmp/repo" 2>/dev/null \
       && git -C "$tmp/repo" sparse-checkout init --no-cone >/dev/null 2>&1 \
       && git -C "$tmp/repo" sparse-checkout set "/$lang.traineddata" >/dev/null 2>&1 \
       && git -C "$tmp/repo" checkout --quiet main 2>/dev/null \
       && [ -s "$tmp/repo/$lang.traineddata" ]; then
      $SUDO cp "$tmp/repo/$lang.traineddata" "$tessdata/"
    else
      warn "git fetch failed for '$lang'; trying a direct download"
      if ! $SUDO curl -fsSL --retry 3 \
            -o "$tessdata/$lang.traineddata" \
            "$repo/raw/main/$lang.traineddata"; then
        $SUDO rm -f "$tessdata/$lang.traineddata"
        die "could not obtain $lang.traineddata - download it manually into $tessdata"
      fi
    fi
    rm -rf "$tmp"

    # A truncated or HTML error page would be silently accepted by tesseract's
    # language listing but fail at OCR time, so size-check it here.
    local size; size=$(wc -c < "$tessdata/$lang.traineddata")
    if [ "$size" -lt 100000 ]; then
      $SUDO rm -f "$tessdata/$lang.traineddata"
      die "$lang.traineddata is only ${size} bytes - the download did not return a model"
    fi
    log "Installed $lang.traineddata (${size} bytes)"
  done
}

# --------------------------------------------------------------------------
# 4. Verify
# --------------------------------------------------------------------------
verify() {
  local bin="$PREFIX/bin/tesseract"
  [ -x "$bin" ] || die "tesseract was not installed at $bin"

  log "Installed: $("$bin" --version 2>&1 | head -1)"
  local langs; langs="$("$bin" --list-langs 2>&1 | tail -n +2 | tr '\n' ' ')"
  [ -n "$langs" ] || die "no languages installed"
  log "Languages: $langs"

  # Prove it actually reads text, not just that the binary runs. Pillow may only
  # exist in the project venv, so look there too before giving up.
  local probe="$BUILD_DIR/probe.png"
  local script_dir; script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local py=""
  for candidate in "$script_dir/../.venv/bin/python" "./.venv/bin/python" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import PIL" 2>/dev/null; then
      py="$candidate"; break
    fi
  done

  if [ -n "$py" ]; then
    "$py" - "$probe" <<'PY'
import sys
from PIL import Image, ImageDraw, ImageFont
for path in ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
    try:
        font = ImageFont.truetype(path, 48); break
    except OSError:
        continue
else:
    font = ImageFont.load_default()
img = Image.new("RGB", (700, 120), "white")
ImageDraw.Draw(img).text((20, 30), "TESSERACT OCR 12345", font=font, fill="black")
img.save(sys.argv[1])
PY
    local got; got="$("$bin" "$probe" stdout 2>/dev/null | tr -d '\n' | tr -s ' ')"
    log "OCR self-test read: '${got}'"
    case "$got" in *TESSERACT*) log "Self-test passed." ;;
      *) warn "Self-test text did not match exactly; check the install." ;;
    esac
  else
    warn "Pillow not available in any interpreter - skipping the OCR self-test."
    warn "Run 'tessy doctor' after 'make install-dev' to confirm end to end."
  fi
}

main() {
  log "Building Tesseract $TESSERACT_VERSION into $PREFIX"
  install_deps
  build_tesseract
  install_tessdata
  verify
  cat <<EOF

Done. Tesseract $TESSERACT_VERSION is installed at $PREFIX/bin/tesseract

If that is not on your PATH, add it:
    export PATH="$PREFIX/bin:\$PATH"
Or point Tessy straight at it:
    export TESSERACT_BIN="$PREFIX/bin/tesseract"

Next:
    make install-dev     # set up the Python environment
    tessy doctor         # confirm everything is wired up
EOF
}

main "$@"
