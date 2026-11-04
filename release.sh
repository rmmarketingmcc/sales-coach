#!/bin/bash
# Publishes a new Sales Coach version.
#   ./release.sh 0.2.1                          notes written from the commits since the last release
#   ./release.sh 0.2.1 "Fixed the clock"        or your own notes (one line, or several "- " lines)
# Friends get it by running the install line again (docs/install.sh downloads the latest release).
# Write commit subjects for users; prefix website-only commits with "Guide:" to keep them out of the notes.
# Before anything is published, SALES_COACH_LEAK_CHECK (a script outside this repo, given the repo path)
# must pass: no client names or private data in the code.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-}"
NOTES="${2:-}"
[[ -n "$VERSION" ]] || { echo "Usage: ./release.sh <version> [\"what changed\"]   e.g. ./release.sh 0.2.1"; exit 1; }
CURRENT="$(cat VERSION)"
[[ "$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | tail -1)" == "$VERSION" ]] || { echo "Version must be at least $CURRENT"; exit 1; }
if git rev-parse "v$VERSION" >/dev/null 2>&1; then echo "v$VERSION already exists"; exit 1; fi
[[ -z "$(git status --porcelain)" ]] || { echo "Commit your changes first"; exit 1; }
if [[ -n "${SALES_COACH_LEAK_CHECK:-}" ]]; then
  "$SALES_COACH_LEAK_CHECK" "$PWD" || { echo "Leak check failed: nothing released"; exit 1; }
else
  echo "Note: SALES_COACH_LEAK_CHECK is not set, so no check for private data ran."
fi

if [[ -z "$NOTES" ]]; then
  PREV=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)
  NOTES=$(git log --no-merges --pretty='%s' ${PREV:+$PREV..}HEAD | grep -vE '^(Release |Guide:|Changelog|Housekeeping)' || true)
  [[ -n "$NOTES" ]] || { echo "No changes since $PREV, nothing to release"; exit 1; }
fi
NOTES=$(printf '%s\n' "$NOTES" | sed -e '/^[[:space:]]*$/d' -e 's/^[-*] //' -e 's/^/- /')

echo "$VERSION" > VERSION
{ head -2 CHANGELOG.md; printf '## %s (%s)\n\n%s\n\n' "$VERSION" "$(date '+%-d %b %Y')" "$NOTES"; tail -n +3 CHANGELOG.md; } > CHANGELOG.tmp
mv CHANGELOG.tmp CHANGELOG.md
./build.sh
git add -A
git commit -q -m "Release $VERSION" -m "$NOTES"
git tag "v$VERSION"
git push -q origin HEAD --tags
gh release create "v$VERSION" build/Sales-Coach.zip --title "Sales Coach $VERSION" --notes "$NOTES"
echo "Released $VERSION:"
printf '%s\n' "$NOTES"
