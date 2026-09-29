"""Changelog + release notes, generated from git history (commits between tags).

  python packaging/release_notes.py v1.0.1      release notes for one tag (used by CI,
                                                 and shown in the in-app update dialog)
  python packaging/release_notes.py --changelog  full CHANGELOG.md for every v* tag
                                                 (+ "Unreleased" for commits after the last tag)

Commit subjects are grouped into New / Fixes / Improvements. Merge commits,
version bumps and the bot's own CHANGELOG commits are left out.
"""
import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = ("merge ", "bump version", "release ", "update changelog")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def version_key(tag: str):
    return tuple(int(x) for x in re.findall(r"\d+", tag)[:3])


def tags() -> list[str]:
    """v* tags, oldest first."""
    ts = [t for t in git("tag", "--list", "v*").splitlines() if re.match(r"^v\d+\.\d+\.\d+", t)]
    return sorted(ts, key=version_key)


def previous_tag(tag: str) -> str:
    older = [t for t in tags() if version_key(t) < version_key(tag)]
    return older[-1] if older else ""


def grouped(rev_range: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {"New": [], "Fixes": [], "Improvements": []}
    for line in git("log", "--no-merges", "--pretty=format:%s\x1f%h", rev_range).splitlines():
        subject, _, sha = line.partition("\x1f")
        s = subject.strip()
        low = s.lower()
        if not s or low.startswith(SKIP):
            continue
        s = re.sub(r"^(feat|fix|chore|docs|refactor|perf|ci|build|style|test)(\(.+?\))?!?:\s*", "", s, flags=re.I)
        if re.match(r"^(fix|fixed|fixes|bug)\b", low) or re.search(r"\bfix(e[sd])?\b", low):
            key = "Fixes"
        elif re.match(r"^(feat|add|added|new|introduce|support)\b", low):
            key = "New"
        else:
            key = "Improvements"
        groups[key].append(f"- {s[0].upper() + s[1:]} ({sha})")
    return groups


def render(groups: dict[str, list[str]], level: str = "###") -> str:
    parts = [f"{level} {k}\n" + "\n".join(v) for k, v in groups.items() if v]
    return "\n\n".join(parts) if parts else "_Maintenance release._"


def release_notes(tag: str, repo: str) -> str:
    version = tag.lstrip("v")
    prev = previous_tag(tag)
    body = render(grouped(f"{prev}..{tag}" if prev else tag))
    out = [f"## What's changed in {version}", "", body, "",
           "### Downloads", "",
           "| System | File |", "|---|---|",
           f"| Windows (installer) | `BlamixShell-{version}-x64.msi` |",
           "| Windows (portable, no install) | `BlamixShell-windows-x64.zip` |",
           "| macOS Apple Silicon / Intel | `BlamixShell-macos-arm64.zip` / `BlamixShell-macos-x64.zip` |",
           "| Linux desktop | `BlamixShell-linux-x64.tar.gz` |",
           "| Linux CLI + TUI (servers) | `blamixshell-cli-linux-x86_64.tar.gz` |", ""]
    if prev:
        out.append(f"**Full changelog**: https://github.com/{repo}/compare/{prev}...{tag}")
    return "\n".join(out)


def changelog(repo: str) -> str:
    ts = tags()
    out = ["# Changelog", "",
           "_Generated automatically from the commit history between release tags. Don't edit by hand._", ""]
    if ts:
        unreleased = grouped(f"{ts[-1]}..HEAD")
        if any(unreleased.values()):
            out += ["## Unreleased", "", render(unreleased, "###"), ""]
    for i in range(len(ts) - 1, -1, -1):
        tag, prev = ts[i], (ts[i - 1] if i > 0 else "")
        date = git("log", "-1", "--format=%ad", "--date=short", tag)
        link = f"https://github.com/{repo}/releases/tag/{tag}"
        out += [f"## [{tag.lstrip('v')}]({link}) - {date}", "",
                render(grouped(f"{prev}..{tag}" if prev else tag), "###"), ""]
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?")
    ap.add_argument("--changelog", action="store_true", help="print the full CHANGELOG.md")
    ap.add_argument("--repo", default="blamixology/blamixshell")
    a = ap.parse_args()
    if a.changelog:
        print(changelog(a.repo), end="")
    elif a.tag:
        print(release_notes(a.tag, a.repo))
    else:
        ap.error("give a tag (e.g. v1.0.1) or --changelog")


if __name__ == "__main__":
    main()
