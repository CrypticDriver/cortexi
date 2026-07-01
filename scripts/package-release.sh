#!/usr/bin/env bash
# Package the Mac client into a release tarball.
# Usage: ./scripts/package-release.sh [version]
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:-$(cat VERSION 2>/dev/null || echo 0.1.0)}"
OUT="dist"
NAME="cortexi-mac-v${VERSION}"
rm -rf "$OUT/$NAME" "$OUT/$NAME.tar.gz"
mkdir -p "$OUT/$NAME"
# ship the mac client (never ship config.json with secrets)
cp -r mac-app "$OUT/$NAME/mac-app"
rm -f "$OUT/$NAME/mac-app/config.json"
cp README.md LICENSE "$OUT/$NAME/"
cp -r deploy "$OUT/$NAME/deploy"
( cd "$OUT" && tar -czf "$NAME.tar.gz" "$NAME" )
echo "built $OUT/$NAME.tar.gz"
shasum -a 256 "$OUT/$NAME.tar.gz" 2>/dev/null || sha256sum "$OUT/$NAME.tar.gz"
