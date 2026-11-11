#!/bin/bash
# Builds build/Sales-Coach.zip: Sales Coach.app (the window, menu bar item and calendar, with the coach's
# engine inside it) and Sales Coach Recorder.app. Apple Silicon only (the speech model needs it).
# Needs only Apple's Command Line Tools.
#   ./build.sh            build the zip (docs/install.sh downloads it from the latest GitHub release):
#                         Sales Coach.app, Sales Coach Recorder.app and engine/ (the coach, with VERSION)
# Signing: ad-hoc. The installer removes the download quarantine, so there is no Gatekeeper prompt.
#
# Weekly updates keep macOS's permissions (microphone, call audio, Calendar). Those follow an ad-hoc signed app's
# exact bytes, so a rebuilt app is a new app to macOS and asks again. So: the engine ships next to the apps
# (the installer puts it in ~/Library/Application Support/Sales Coach/engine), and the two apps are only rebuilt
# when their own code changes (APPS_ID, a hash of app/ and recorder/). Otherwise this reuses the exact apps from
# build/apps/<APPS_ID>, or from the latest release when that has the same APPS_ID, and the updater leaves the
# installed apps alone.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(cat VERSION)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/salescoach-build.XXXXXX")"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
cleanup() { "$LSREGISTER" -u "$WORK/Sales Coach.app" "$WORK/Sales Coach Recorder.app" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p build
APPS_REV=1   # bump to force new apps (a change in how they are built, not in their code)
APPS_ID="$(cat app/main.swift app/Info.plist app/AppIcon.icns recorder/main.swift recorder/Info.plist | shasum -a 256 | cut -c1-16)-$APPS_REV"
CACHE="build/apps/$APPS_ID"

bundle() {   # bundle <source dir> <app name> <executable>
  local app="$WORK/$2.app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  swiftc -O -swift-version 5 -target arm64-apple-macos14.4 "$1/main.swift" -o "$app/Contents/MacOS/$3"
  cp "$1/Info.plist" "$app/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$app/Contents/Info.plist"
  cp app/AppIcon.icns "$app/Contents/Resources/AppIcon.icns"
  echo "$APPS_ID" > "$app/Contents/Resources/APPS_ID"
}

if [[ ! -f "$CACHE/Sales Coach.app/Contents/Resources/APPS_ID" ]]; then
  # not built here before: the latest release's apps, if they are the same code
  if command -v gh >/dev/null && gh release download --repo rmmarketingmcc/sales-coach --pattern Sales-Coach.zip \
       --dir "$WORK/prev" >/dev/null 2>&1; then
    ditto -x -k "$WORK/prev/Sales-Coach.zip" "$WORK/prev/x"
    if [[ "$(cat "$WORK/prev/x/Sales Coach.app/Contents/Resources/APPS_ID" 2>/dev/null)" == "$APPS_ID" ]]; then
      mkdir -p "$CACHE"
      ditto "$WORK/prev/x/Sales Coach.app" "$CACHE/Sales Coach.app"
      ditto "$WORK/prev/x/Sales Coach Recorder.app" "$CACHE/Sales Coach Recorder.app"
      echo "==> Apps unchanged: the latest release's ($APPS_ID)"
    fi
  fi
fi
if [[ -f "$CACHE/Sales Coach.app/Contents/Resources/APPS_ID" ]]; then
  echo "==> Apps unchanged since they were last built ($APPS_ID): reusing them, so macOS keeps their permissions"
else
  echo "==> Compiling (new apps: $APPS_ID)"
  bundle recorder "Sales Coach Recorder" SalesCoachRecorder
  bundle app "Sales Coach" SalesCoach
  echo "==> Signing"
  for app in "$WORK/Sales Coach Recorder.app" "$WORK/Sales Coach.app"; do
    /usr/bin/xattr -cr "$app"   # Finder/iCloud metadata breaks codesign; macOS own xattr, a pip "xattr" on PATH has no -r
    codesign --force --sign - "$app" 2>&1 | grep -v "replacing existing" || true
  done
  mkdir -p "$CACHE"
  ditto "$WORK/Sales Coach.app" "$CACHE/Sales Coach.app"
  ditto "$WORK/Sales Coach Recorder.app" "$CACHE/Sales Coach Recorder.app"
fi

echo "==> Engine"
mkdir -p "$WORK/zip/engine"
cp engine/*.py engine/window.html engine/requirements.txt VERSION "$WORK/zip/engine/"

rm -f build/Sales-Coach.zip
ditto "$CACHE/Sales Coach.app" "$WORK/zip/Sales Coach.app"
ditto "$CACHE/Sales Coach Recorder.app" "$WORK/zip/Sales Coach Recorder.app"
ditto -c -k "$WORK/zip" build/Sales-Coach.zip
echo "Built build/Sales-Coach.zip ($VERSION, $(du -h build/Sales-Coach.zip | cut -f1))"
