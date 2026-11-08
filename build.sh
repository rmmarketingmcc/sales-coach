#!/bin/bash
# Builds build/Sales-Coach.zip: Sales Coach.app (the window, menu bar item and calendar, with the coach's
# engine inside it) and Sales Coach Recorder.app. Apple Silicon only (the speech model needs it).
# Needs only Apple's Command Line Tools.
#   ./build.sh            build the zip (docs/install.sh downloads it from the latest GitHub release)
# Signing: ad-hoc. The installer removes the download quarantine, so there is no Gatekeeper prompt.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(cat VERSION)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/salescoach-build.XXXXXX")"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
cleanup() { "$LSREGISTER" -u "$WORK/Sales Coach.app" "$WORK/Sales Coach Recorder.app" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p build

bundle() {   # bundle <source dir> <app name> <executable>
  local app="$WORK/$2.app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  swiftc -O -swift-version 5 -target arm64-apple-macos14.4 "$1/main.swift" -o "$app/Contents/MacOS/$3"
  cp "$1/Info.plist" "$app/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$app/Contents/Info.plist"
  cp app/AppIcon.icns "$app/Contents/Resources/AppIcon.icns"
}

echo "==> Compiling"
bundle recorder "Sales Coach Recorder" SalesCoachRecorder
bundle app "Sales Coach" SalesCoach

echo "==> Engine"
ENGINE="$WORK/Sales Coach.app/Contents/Resources/engine"
mkdir -p "$ENGINE"
cp engine/*.py engine/window.html engine/requirements.txt "$ENGINE/"
cp VERSION "$WORK/Sales Coach.app/Contents/Resources/VERSION"

echo "==> Signing"
for app in "$WORK/Sales Coach Recorder.app" "$WORK/Sales Coach.app"; do
  /usr/bin/xattr -cr "$app"   # Finder/iCloud metadata breaks codesign; macOS own xattr, a pip "xattr" on PATH has no -r
  codesign --force --sign - "$app" 2>&1 | grep -v "replacing existing" || true
done

rm -f build/Sales-Coach.zip
mkdir "$WORK/zip"
mv "$WORK/Sales Coach.app" "$WORK/Sales Coach Recorder.app" "$WORK/zip/"
ditto -c -k "$WORK/zip" build/Sales-Coach.zip
echo "Built build/Sales-Coach.zip ($VERSION, $(du -h build/Sales-Coach.zip | cut -f1))"
