#!/bin/bash
# Runs the unit tests (fixture Notes database + local mock server; no network, no real Notes data).
# With only the Command Line Tools installed, swift-testing lives outside the default search
# path, so point the compiler and linker at it. With full Xcode, plain `swift test` also works.
set -euo pipefail
cd "$(dirname "$0")/VALRRecorderSync"
DEV="$(xcode-select -p)"
FW="$DEV/Library/Developer/Frameworks"
LIB="$DEV/Library/Developer/usr/lib"
if [ -d "$FW/Testing.framework" ]; then
  exec swift test -Xswiftc -F -Xswiftc "$FW" -Xlinker -F -Xlinker "$FW" \
    -Xlinker -rpath -Xlinker "$FW" -Xlinker -rpath -Xlinker "$LIB" "$@"
fi
# Full Xcode: frameworks are found automatically.
exec swift test "$@"
