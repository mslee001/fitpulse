#!/usr/bin/env bash
# Build FitPulse's CSS with the Tailwind standalone binary + daisyUI (no Node, no npm).
#
#   scripts/css.sh install   # download the pinned binary to bin/ and the daisyUI plugin files
#   scripts/css.sh build     # assets/css/app.css → static/css/app.css (minified)
#   scripts/css.sh watch     # rebuild on change while developing (unminified —
#                            #   run `build` before committing)
#   scripts/css.sh check     # exit 1 if the committed static/css/app.css is stale
set -euo pipefail

TAILWIND_VERSION=4.3.3
DAISYUI_VERSION=5.7.47

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BIN=bin/tailwindcss
SRC=assets/css/app.css
OUT=static/css/app.css
VENDOR=assets/css/vendor

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

need_binary() {
  if [ ! -x "$BIN" ]; then
    echo "run scripts/css.sh install first" >&2
    exit 1
  fi
}

download() {  # url dest
  echo "downloading $1"
  curl -fsSL -o "$2" "$1"
}

install() {
  local os arch asset
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os $arch" in
    "Darwin arm64")   asset=tailwindcss-macos-arm64 ;;
    "Darwin x86_64")  asset=tailwindcss-macos-x64 ;;
    "Linux x86_64")   asset=tailwindcss-linux-x64 ;;
    "Linux aarch64")  asset=tailwindcss-linux-arm64 ;;
    *) echo "unsupported platform: $os $arch" >&2; exit 1 ;;
  esac

  mkdir -p bin "$VENDOR"
  if [ -x "$BIN" ] && "$BIN" --help 2>&1 | head -1 | grep -q "v$TAILWIND_VERSION"; then
    echo "tailwindcss v$TAILWIND_VERSION already installed"
  else
    download "https://github.com/tailwindlabs/tailwindcss/releases/download/v$TAILWIND_VERSION/$asset" "$BIN"
    chmod +x "$BIN"
    xattr -d com.apple.quarantine "$BIN" 2>/dev/null || true
  fi

  for f in daisyui.mjs daisyui-theme.mjs; do
    download "https://github.com/saadeghi/daisyui/releases/download/v$DAISYUI_VERSION/$f" "$VENDOR/$f"
  done
  "$BIN" --help 2>&1 | head -1
}

build() {
  need_binary
  "$BIN" -i "$SRC" -o "$OUT" --minify
}

watch() {
  need_binary
  "$BIN" -i "$SRC" -o "$OUT" --watch
}

check() {
  need_binary
  CHECK_TMP="$(mktemp)"
  trap 'rm -f "$CHECK_TMP"' EXIT
  "$BIN" -i "$SRC" -o "$CHECK_TMP" --minify 2>/dev/null
  if ! cmp -s "$CHECK_TMP" "$OUT"; then
    echo "static/css/app.css is stale — run scripts/css.sh build" >&2
    exit 1
  fi
  echo "static/css/app.css is up to date"
}

case "${1:-}" in
  install) install ;;
  build)   build ;;
  watch)   watch ;;
  check)   check ;;
  *)       usage ;;
esac
