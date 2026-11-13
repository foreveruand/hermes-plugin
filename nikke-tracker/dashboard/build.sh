#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
esbuild="${ESBUILD:-}"
if [ -z "$esbuild" ]; then
  esbuild="$(command -v esbuild || true)"
fi
if [ -z "$esbuild" ] && [ -x "$HOME/.hermes/hermes-agent/node_modules/.bin/esbuild" ]; then
  esbuild="$HOME/.hermes/hermes-agent/node_modules/.bin/esbuild"
fi
if [ -z "$esbuild" ] || [ ! -x "$esbuild" ]; then
  echo "esbuild not found; set ESBUILD=/path/to/esbuild" >&2
  exit 1
fi
"$esbuild" "$here/src/index.jsx" \
  --bundle --format=iife --target=es2020 \
  --jsx=transform --jsx-factory=React.createElement --jsx-fragment=React.Fragment \
  --outfile="$here/dist/index.js"
cp "$here/src/styles.css" "$here/dist/style.css"
echo "built $here/dist/index.js"
