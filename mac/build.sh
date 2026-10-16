#!/bin/bash
# Builds "VALR.app" (bundle id com.haciensus.valr.mac), signs it, and packages
# VALR-<version>.zip and VALR-<version>.dmg in mac/dist/. scripts/release_mac.sh puts the DMG
# where the VALR site serves it (Settings › Phone call recordings › VALR for Mac).
#
#   mac/build.sh                 # free path: ad-hoc signature (users right-click › Open once)
#   SIGN_ID="Developer ID Application: Your Name (TEAMID)" mac/build.sh   # proper path, see TODOs
#
# Needs only the Xcode Command Line Tools (xcode-select --install).
set -euo pipefail

VERSION="${VERSION:-1.0.0}"
BUILD="${BUILD:-$(date +%Y%m%d%H%M)}"
SIGN_ID="${SIGN_ID:--}"            # "-" = ad-hoc
APP_NAME="VALR"
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$HERE/VALRRecorderSync"
DIST="$HERE/dist"
APP="$DIST/$APP_NAME.app"

echo "==> Building release binary"
cd "$PKG"
if swift build -c release --arch arm64 --arch x86_64 >/dev/null 2>&1; then
  BIN="$(swift build -c release --arch arm64 --arch x86_64 --show-bin-path)/VALR"
  echo "    universal (Apple Silicon + Intel)"
else
  # Universal builds need full Xcode; with only the Command Line Tools build for this Mac.
  swift build -c release
  BIN="$(swift build -c release --show-bin-path)/VALR"
  echo "    $(uname -m) only (install full Xcode for a universal build)"
fi

echo "==> Assembling $APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/VALR"
sed -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$BUILD/" "$PKG/Resources/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

echo "==> Icons"
# AppIcon.icns from the 1024 px master (scripts/make_icons.py --mac draws it from VALR's orb mark).
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size "$PKG/Resources/AppIcon-1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$PKG/Resources/AppIcon-1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"
# The menu-bar mark: a template image, so macOS tints it for light and dark menu bars.
cp "$PKG/Resources/MenuBarIconTemplate.png" "$PKG/Resources/MenuBarIconTemplate@2x.png" "$APP/Contents/Resources/"

echo "==> Signing ($([ "$SIGN_ID" = "-" ] && echo ad-hoc || echo "$SIGN_ID"))"
TS_FLAG="--timestamp=none"
[ "$SIGN_ID" != "-" ] && TS_FLAG="--timestamp"
codesign --force --options runtime $TS_FLAG \
  --entitlements "$PKG/Resources/VALRRecorderSync.entitlements" \
  --sign "$SIGN_ID" "$APP"
codesign --verify --strict --verbose=2 "$APP"

echo "==> Packaging"
ZIP="$DIST/VALR-$VERSION.zip"
DMG="$DIST/VALR-$VERSION.dmg"
rm -f "$ZIP" "$DMG"
ditto -c -k --keepParent "$APP" "$ZIP"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

# ---------------------------------------------------------------------------
# TODO (proper path, needs the Apple Developer Program, US$99/year):
#  1. Build with a Developer ID certificate:
#       SIGN_ID="Developer ID Application: Your Name (TEAMID)" mac/build.sh
#  2. Store notary credentials once (an app-specific password from appleid.apple.com):
#       xcrun notarytool store-credentials valr-notary \
#         --apple-id "you@example.com" --team-id TEAMID --password "app-specific-password"
#  3. Sign the DMG too, then submit and wait:
#       codesign --sign "$SIGN_ID" --timestamp "$DMG"
#       xcrun notarytool submit "$DMG" --keychain-profile valr-notary --wait
#  4. Staple the ticket so it opens offline, and check Gatekeeper accepts it:
#       xcrun stapler staple "$DMG"
#       xcrun stapler staple "$APP"
#       spctl --assess --type open --context context:primary-signature -v "$DMG"
#       spctl --assess --type execute -v "$APP"
#  5. Auto-updates: add Sparkle (appcast.xml + EdDSA-signed updates). Not implemented.
# ---------------------------------------------------------------------------

echo
echo "Done:"
echo "  $APP"
echo "  $ZIP"
echo "  $DMG"
if [ "$SIGN_ID" = "-" ]; then
  echo
  echo "Ad-hoc signed: on another Mac, right-click the app › Open the first time,"
  echo "or run: xattr -dr com.apple.quarantine \"/Applications/$APP_NAME.app\""
fi
