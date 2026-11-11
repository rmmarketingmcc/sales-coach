#!/bin/bash
# Publishes a new Sales Coach version.
#   ./release.sh 0.2.1 "Uret viser rigtig tid"   notes in Danish, one per line: they go on the page under "Nyt"
#   ./release.sh 0.2.1                          prints the commit subjects since the last release to write from
# Friends get it from "Ny version: opdater" in the menu bar (the app looks once a day), or the install line again.
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
# a fork with no private data of its own can skip it: SALES_COACH_NO_LEAK_CHECK=1
if [[ -n "${SALES_COACH_LEAK_CHECK:-}" ]]; then
  "$SALES_COACH_LEAK_CHECK" "$PWD" || { echo "Leak check failed: nothing released"; exit 1; }
elif [[ -z "${SALES_COACH_NO_LEAK_CHECK:-}" ]]; then
  echo "SALES_COACH_LEAK_CHECK is not set: no release without the check for private data."
  echo "(No private data to protect? SALES_COACH_NO_LEAK_CHECK=1 ./release.sh ...)"
  exit 1
fi

if [[ -z "$NOTES" ]]; then
  # the page and the changelog are Danish, the commit subjects English: the notes are written by hand
  PREV=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)
  echo "Write the notes in Danish (they go on the page under \"Nyt\"), e.g.:"
  echo "  ./release.sh $VERSION \"Coachen skriver selv i Meet-chatten, at du optager"
  echo "Din egen dagsorden bliver krydset af\""
  echo "Changes since ${PREV:-the start}:"
  git log --no-merges --pretty='  - %s' ${PREV:+$PREV..}HEAD | grep -vE '^  - (Release |Guide:|Changelog|Housekeeping)' || true
  exit 1
fi
NOTES=$(printf '%s\n' "$NOTES" | sed -e '/^[[:space:]]*$/d' -e 's/^[-*] //' -e 's/^/- /')

echo "$VERSION" > VERSION
{ head -2 CHANGELOG.md; printf '## %s (%s)\n\n%s\n\n' "$VERSION" "$(date '+%-d %b %Y')" "$NOTES"; tail -n +3 CHANGELOG.md; } > CHANGELOG.tmp
mv CHANGELOG.tmp CHANGELOG.md
python3 tests/changelog_json.py   # the guide page's version and "What's new" follow the release
./build.sh
git add -A
git commit -q -m "Release $VERSION" -m "$NOTES"
git tag "v$VERSION"
git push -q origin HEAD --tags
gh release create "v$VERSION" build/Sales-Coach.zip --title "Sales Coach $VERSION" --notes "$NOTES"
echo "Released $VERSION:"
printf '%s\n' "$NOTES"
echo "The page shows $VERSION within a minute. Window changed? Retake its pictures: .venv/bin/python tests/page_shots.py"
