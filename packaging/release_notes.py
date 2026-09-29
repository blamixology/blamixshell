"""Write release notes for a tag (used by CI and by the in-app update dialog).

Sources, in order:
  1. A section for this version in CHANGELOG.md (e.g. "## 1.0.1" or "## [v1.0.1] - 2026-09-30"),
     if you keep one.
  2. Otherwise the commit subjects since the previous tag, grouped into
     New / Fixes / Improvements.
Plus a short "which file do I download?" guide.

Usage: python packaging/release_notes.py v1.0.1 [--repo owner/name] > notes.md
"""
import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def changelog_section(version: str) -> str:
    p = ROOT / "CHANGELOG.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    pat = re.compile(rf"^##\s*\[?v?{re.escape(version)}\]?.*$", re.M)
    m = pat.search(text)
    if not m:
        return ""
    nxt = re.search(r"^##\s", text[m.end():], re.M)
    return text[m.end(): m.end() + nxt.start() if nxt else len(text)].strip()


def commit_notes(tag: str) -> tuple[str, str]:
    prev = git("describe", "--tags", "--abbrev=0", f"{tag}^")
    rng = f"{prev}..{tag}" if prev else tag
    lines = git("log", "--no-merges", "--pretty=format:%s\x1f%h", rng).splitlines()
    groups: dict[str, list[str]] = {"New": [], "Fixes": [], "Improvements": []}
    for line in lines:
        subject, _, sha = line.partition("\x1f")
        s = subject.strip()
        low = s.lower()
        if not s or low.startswith(("merge ", "bump version", "release ")):
            continue
        s = re.sub(r"^(feat|fix|chore|docs|refactor|perf|ci|build|style|test)(\(.+?\))?!?:\s*", "", s, flags=re.I)
        if re.match(r"^(fix|fixed|fixes|bug)\b", low) or re.search(r"\bfix(e[sd])?\b", low):
            key = "Fixes"
        elif re.match(r"^(feat|add|added|new|introduce|support)\b", low):
            key = "New"
        else:
            key = "Improvements"
        groups[key].append(f"- {s[0].upper() + s[1:]} ({sha})")
    body = "\n\n".join(f"### {k}\n" + "\n".join(v) for k, v in groups.items() if v)
    return body, prev


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--repo", default="blamixology/shelldeck")
    a = ap.parse_args()
    version = a.tag.lstrip("v")
    body = changelog_section(version)
    commits, prev = commit_notes(a.tag)
    if not body:
        body = commits or "_Maintenance release._"
    out = [f"## What's changed in {version}", "", body, ""]
    out += ["### Downloads", "",
            "| System | File |", "|---|---|",
            f"| Windows (installer) | `ShellDeck-{version}-x64.msi` |",
            "| Windows (portable, no install) | `ShellDeck-windows-x64.zip` |",
            "| macOS Apple Silicon / Intel | `ShellDeck-macos-arm64.zip` / `ShellDeck-macos-x64.zip` |",
            "| Linux desktop | `ShellDeck-linux-x64.tar.gz` |",
            "| Linux CLI + TUI (servers) | `shelldeck-cli-linux-x86_64.tar.gz` |", ""]
    if prev:
        out.append(f"**Full changelog**: https://github.com/{a.repo}/compare/{prev}...{a.tag}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
