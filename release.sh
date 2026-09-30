#!/usr/bin/env bash
# Make a release: update CHANGELOG.md locally, commit it, tag, and push both at once.
# CI then builds the tag and publishes the GitHub Release (it never commits to main,
# so your pushes are never rejected by a bot commit again).
#
#   ./release.sh v1.3.0             release
#   ./release.sh --dry-run v1.3.0   show the release notes + CHANGELOG diff, change nothing
#
# Works in Git Bash on Windows, and on macOS/Linux.
set -euo pipefail
cd "$(dirname "$0")"

dry=0
if [ "${1:-}" = "--dry-run" ]; then dry=1; shift; fi
tag="${1:-}"
if ! [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: ./release.sh [--dry-run] vX.Y.Z   (last release: $(git tag --list 'v*' --sort=-v:refname | head -1))"
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
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "Tag $tag already exists."; exit 1
fi
if [ -z "$(git log --oneline "$(git tag --list 'v*' --sort=-v:refname | head -1)..HEAD" 2>/dev/null)" ]; then
  echo "Nothing new since the last release."; exit 1
fi

# The generator reads tags, so tag HEAD first; if anything below fails, drop the tag again.
git tag "$tag"
cleanup() { git tag -d "$tag" >/dev/null 2>&1 || true; git checkout -- CHANGELOG.md 2>/dev/null || true; }
trap cleanup ERR

$py packaging/release_notes.py --changelog > CHANGELOG.md

if [ "$dry" = 1 ]; then
  echo; echo "===== Release notes for $tag ====="
  $py packaging/release_notes.py "$tag"
  echo; echo "===== CHANGELOG.md changes ====="
  git --no-pager diff --stat -- CHANGELOG.md
  git --no-pager diff -- CHANGELOG.md | head -60
  cleanup
  echo; echo "(dry run: nothing committed, tagged or pushed)"
  exit 0
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
echo
echo "Done. GitHub Actions is now building $tag:"
echo "  https://github.com/blamixology/blamixshell/actions"
