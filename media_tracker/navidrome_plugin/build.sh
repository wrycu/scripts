#!/usr/bin/env bash
#
# Build the like-dislike Navidrome plugin into a .ndp package.
#
# Requires: git, tinygo (recommended) or Go 1.25+ with the wasip1 target, and zip.
#
# The plugin's go.mod uses a relative `replace` pointing at Navidrome's PDK
# (../../pdk/go), matching the bundled example plugins. So we clone Navidrome and
# stage this plugin under plugins/examples/like-dislike/ before building.
#
# Env overrides:
#   NAV_REF  git ref of navidrome to build against (default: master)
#   WORK     scratch dir for the checkout (default: ./.build)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAV_REF="${NAV_REF:-master}"
WORK="${WORK:-"$HERE/.build"}"
NAV="$WORK/navidrome"
OUT="$HERE/like-dislike.ndp"

mkdir -p "$WORK"

if [ ! -d "$NAV/.git" ]; then
  echo ">> Cloning navidrome ($NAV_REF) for the plugin PDK..."
  git clone --depth 1 --branch "$NAV_REF" https://github.com/navidrome/navidrome "$NAV"
fi

DST="$NAV/plugins/examples/like-dislike"
mkdir -p "$DST"
cp "$HERE/main.go" "$HERE/go.mod" "$HERE/manifest.json" "$DST/"

cd "$DST"
echo ">> Resolving dependencies..."
go mod tidy

echo ">> Compiling to WebAssembly..."
if command -v tinygo >/dev/null 2>&1; then
  tinygo build -target wasip1 -buildmode=c-shared -o plugin.wasm .
else
  echo ">> tinygo not found; falling back to 'go build' (larger binary)"
  GOOS=wasip1 GOARCH=wasm go build -buildmode=c-shared -o plugin.wasm .
fi

echo ">> Packaging $OUT..."
rm -f "$OUT"
zip -j "$OUT" manifest.json plugin.wasm

echo ">> Done: $OUT"
