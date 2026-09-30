#!/usr/bin/env bash
# Make a release: update CHANGELOG.md locally, commit it, tag, and push both at once.
# CI then builds the tag and publishes the GitHub Release. It never commits to main,
# so your pushes are never rejected by a bot commit.
#
#   ./release.sh v1.3.0              release (shows the notes and asks before pushing)
#   ./release.sh v1.3.0 -y           release without the question
#   ./release.sh --dry-run v1.3.0    show the notes + CHANGELOG diff, change nothing
#   ./release.sh v1.3.0 --no-tests   skip the local test run
#
# Works in Git Bash on Windows, and on macOS/Linux.
set -euo pipefail
cd "$(dirname "$0")"

dry=0; yes=0; tests=1; tag=""
for a in "$@"; do
  case "$a" in
    --dry-run) dry=1 ;;
    -y|--yes) yes=1 ;;
    --no-tests) tests=0 ;;
    -*) echo "unknown option: $a"; exit 2 ;;
    *) tag="$a" ;;
  esac
done
last="$(git tag --list 'v*' --sort=-v:refname | head -1)"
if ! [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: ./release.sh [--dry-run] [-y] [--no-tests] vX.Y.Z   (last release: ${last:-none})"
  exit 2
fi

py=""
for c in python3 python "py -3"; do
  if $c -c "import sys; sys.exit(sys.version_info < (3, 8))" >/dev/null 2>&1; then py="$c"; break; fi
done
[ -n "$py" ] || { echo "Python 3 not found"; exit 1; }

[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "Switch to main first."; exit 1; }
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "You have uncommitted changes: commit or stash them first."; exit 1
fi

echo "Syncing with GitHub…"
git fetch --tags --quiet origin
git pull --rebase --quiet origin main
last="$(git tag --list 'v*' --sort=-v:refname | head -1)"
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "Tag $tag already exists."; exit 1
fi
if [ -n "$last" ]; then
  newest="$(printf '%s\n%s\n' "$last" "$tag" | sort -V | tail -1)"
  if [ "$newest" != "$tag" ]; then echo "$tag isn't newer than the last release ($last)."; exit 1; fi
  if [ -z "$(git log --oneline "$last..HEAD")" ]; then echo "Nothing new since $last."; exit 1; fi
fi

echo
echo "Commits in $tag:"
git --no-pager log --oneline --no-merges "${last:+$last..}HEAD"

# Local tests catch a broken build before the tag is public (CI runs them again).
if [ "$tests" = 1 ] && [ "$dry" = 0 ]; then
  if $py -c "import pytest, paramiko, cryptography" >/dev/null 2>&1; then
    echo; echo "Running tests…"
    QT_QPA_PLATFORM=offscreen $py -m pytest -q tests || { echo "Tests failed: nothing was tagged or pushed."; exit 1; }
  else
    echo; echo "(Skipping local tests: pytest/paramiko not installed for $py. CI still runs them.)"
  fi
fi

# The generator reads tags, so tag HEAD first; if anything below fails, drop the tag again.
git tag "$tag"
cleanup() { git tag -d "$tag" >/dev/null 2>&1 || true; git checkout -- CHANGELOG.md 2>/dev/null || true; }
trap cleanup ERR

$py packaging/release_notes.py --changelog > CHANGELOG.md
echo; echo "===== Release notes for $tag ====="
$py packaging/release_notes.py "$tag"

if [ "$dry" = 1 ]; then
  echo; echo "===== CHANGELOG.md changes ====="
  git --no-pager diff --stat -- CHANGELOG.md
  git --no-pager diff -- CHANGELOG.md | head -60
  cleanup
  echo; echo "(dry run: nothing committed, tagged or pushed)"
  exit 0
fi

if [ "$yes" = 0 ]; then
  echo
  read -r -p "Publish $tag? [y/N] " answer
  if ! [[ "$answer" =~ ^[Yy] ]]; then cleanup; echo "Cancelled: nothing committed, tagged or pushed."; exit 1; fi
fi

if ! git diff --quiet -- CHANGELOG.md; then
  git add CHANGELOG.md
  # no [skip ci] here: the tag points at this commit and GitHub would skip the release build
  git commit --quiet -m "Update CHANGELOG for $tag"
  git tag -f "$tag" >/dev/null          # the release includes the updated CHANGELOG
fi
trap - ERR

echo "Pushing main + $tag…"
if ! git push --atomic origin main "$tag"; then
  echo "Push failed: the tag $tag is still local. Fix the problem, then run:"
  echo "  git push --atomic origin main $tag"
  exit 1
fi
repo="$(git remote get-url origin | sed -E 's#(git@github\.com:|https://github\.com/)##; s#\.git$##')"
echo
echo "Done. GitHub Actions is now building $tag (about 15 minutes):"
echo "  https://github.com/$repo/actions"
echo "The release will appear at:"
echo "  https://github.com/$repo/releases/tag/$tag"
