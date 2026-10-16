#!/bin/bash
# Rebuilds VALR for Mac and puts the DMG where the VALR site serves it:
#   downloads/VALR-<version>.dmg   the file (older VALR-*.dmg are removed)
#   downloads/mac.json             version, file name, size and SHA-256 (app/mac_release.py)
# Settings › Phone call recordings › VALR for Mac offers it at GET /valr/downloads/mac (owner only).
#
#   scripts/release_mac.sh            # version from VERSION (default 1.0.0)
#   VERSION=1.0.1 scripts/release_mac.sh
#
# Then commit downloads/: deploy/deploy-all.sh ships `git archive` of the branch, and the
# Dockerfile copies downloads/ into the app image. Keep the DMG small (it is ~1.3 MB).
set -euo pipefail
VERSION="${VERSION:-1.0.0}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "VERSION must look like 1.2.3" >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/downloads"

VERSION="$VERSION" "$REPO/mac/build.sh"

DMG="$REPO/mac/dist/VALR-$VERSION.dmg"
mkdir -p "$OUT"
find "$OUT" -maxdepth 1 -name 'VALR-*.dmg' ! -name "VALR-$VERSION.dmg" -delete
cp "$DMG" "$OUT/VALR-$VERSION.dmg"
SHA="$(shasum -a 256 "$OUT/VALR-$VERSION.dmg" | cut -d' ' -f1)"
SIZE="$(stat -f %z "$OUT/VALR-$VERSION.dmg")"
cat > "$OUT/mac.json" <<JSON
{
  "version": "$VERSION",
  "file": "VALR-$VERSION.dmg",
  "size": $SIZE,
  "sha256": "$SHA"
}
JSON
echo
echo "Released downloads/VALR-$VERSION.dmg ($SIZE bytes, SHA-256 $SHA)."
echo "Commit downloads/ to ship it with the next deploy."
