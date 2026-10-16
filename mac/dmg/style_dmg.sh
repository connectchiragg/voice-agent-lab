#!/bin/bash
# Packs a staged folder (VALR.app + an Applications symlink) into a compressed DMG whose Finder
# window is styled like a well-made Mac installer: the night-sky background (background.tiff,
# drawn by make_background.swift), the app on the left, Applications on the right, 128 pt icons,
# no toolbar or status bar, and VALR's orb as the volume icon.
#
#   mac/dmg/style_dmg.sh STAGE_DIR OUT.dmg VOLNAME background.tiff AppIcon.icns
#
# Called by mac/build.sh. It drives Finder with AppleScript (the only way to write the window's
# .DS_Store), so the first run asks for permission to control Finder. Exits non-zero on any
# failure, after detaching; build.sh then falls back to a plain DMG.
#
# The geometry must match make_background.swift: a 660x400 pt window, icons centred at
# (170, 190) and (490, 190).
set -euo pipefail

STAGE="$1"; OUT="$2"; VOL="$3"; BG="$4"; ICNS="$5"
WIN_W=660; WIN_H=400
WIN_X=200; WIN_Y=140
APP_POS="170, 190"; APPS_POS="490, 190"

WORK="$(mktemp -d)"
RW="$WORK/rw.dmg"
DEV=""; MP=""

cleanup() {
  if [ -n "$DEV" ]; then
    hdiutil detach "$DEV" -quiet 2>/dev/null || hdiutil detach "$DEV" -force -quiet 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

# 1. A read-write image with some headroom for the background, volume icon and .DS_Store.
SIZE_MB=$(( $(du -sm "$STAGE" | cut -f1) + 16 ))
hdiutil create -quiet -srcfolder "$STAGE" -volname "$VOL" -fs HFS+ -format UDRW -size "${SIZE_MB}m" "$RW"

# 2. Mount it where Finder can see it, and wait for the mount point.
ATTACH="$(hdiutil attach "$RW" -readwrite -noverify -noautoopen)"
DEV="$(echo "$ATTACH" | awk '/Apple_HFS/ {print $1; exit}')"
MP="$(echo "$ATTACH" | awk -F'\t' '/Apple_HFS/ {print $NF; exit}' | sed 's/^ *//')"
for _ in $(seq 1 20); do [ -d "$MP" ] && break; sleep 0.5; done
[ -n "$DEV" ] && [ -d "$MP" ] || { echo "style_dmg: the image did not mount" >&2; exit 1; }

# 3. Hidden background folder (the volume icon comes after Finder: its "update" deletes a
#    .VolumeIcon.icns it finds, at least on macOS 26).
mkdir "$MP/.background"
cp "$BG" "$MP/.background/background.tiff"
command -v SetFile >/dev/null 2>&1 && SetFile -a V "$MP/.background" 2>/dev/null || true

# 4. Style the window with Finder. The window is found by its target's mount path, never by
#    volume name, so another mounted volume called
#    "VALR" (say, last week's download) can't get in the way.
#    WIN_H + TITLE: Finder's bounds include the title bar (32 pt on macOS 26, 28 pt before), and
#    the background must fill the 400 pt content area; a few points cropped at the bottom on
#    older systems only trims empty sky.
TITLE=28
style_window() {
  osascript - "$MP" <<OSA
on run argv
  set mp to item 1 of argv
  tell application "Finder"
    activate
    open (POSIX file mp as alias)
    delay 1.5
    set w to missing value
    repeat with i from 1 to (count Finder windows)
      set t to target of Finder window i
      if POSIX path of (t as alias) is (mp & "/") then
        set w to Finder window i
        exit repeat
      end if
    end repeat
    if w is missing value then error "no Finder window for " & mp
    set current view of w to icon view
    set toolbar visible of w to false
    set statusbar visible of w to false
    set bounds of w to {$WIN_X, $WIN_Y, $((WIN_X + WIN_W)), $((WIN_Y + WIN_H + TITLE))}
    set opts to icon view options of w
    set arrangement of opts to not arranged
    set icon size of opts to 128
    set text size of opts to 13
    set label position of opts to bottom
    set shows item info of opts to false
    set shows icon preview of opts to false
    set background picture of opts to file ".background:background.tiff" of t
    set position of item "VALR.app" of t to {$APP_POS}
    set position of item "Applications" of t to {$APPS_POS}
    update t without registering applications
    delay 1
    -- Closing the window makes Finder write the layout to .DS_Store.
    close w
  end tell
end run
OSA
}
if ! style_window; then
  echo "style_dmg: Finder AppleScript failed, retrying once" >&2
  sleep 2
  style_window
fi

# Wait for Finder to flush the window settings to .DS_Store.
for _ in $(seq 1 20); do [ -f "$MP/.DS_Store" ] && break; sleep 0.5; done
[ -f "$MP/.DS_Store" ] || { echo "style_dmg: Finder never wrote .DS_Store" >&2; exit 1; }

# The volume icon: VALR's app icon, flagged so Finder uses it.
cp "$ICNS" "$MP/.VolumeIcon.icns"
if command -v SetFile >/dev/null 2>&1; then
  SetFile -a V "$MP/.VolumeIcon.icns" 2>/dev/null || true
  SetFile -a C "$MP"
else
  # No SetFile: set the "has custom icon" Finder flag directly.
  xattr -wx com.apple.FinderInfo "0000000000000000040000000000000000000000000000000000000000000000" "$MP" || true
fi
chmod -Rf go-w "$MP" 2>/dev/null || true
rm -rf "$MP/.fseventsd" "$MP/.Trashes" 2>/dev/null || true
sync

# 5. Detach (retrying, Finder sometimes holds it briefly) and compress.
for i in 1 2 3 4 5; do
  hdiutil detach "$DEV" -quiet && { DEV=""; break; }
  sleep 1
  [ "$i" = 5 ] && { hdiutil detach "$DEV" -force -quiet; DEV=""; }
done
rm -f "$OUT"
hdiutil convert -quiet "$RW" -format UDZO -imagekey zlib-level=9 -o "$OUT"
