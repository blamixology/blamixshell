__version__ = "1.0.0"

_UNSTAMPED = "1.0.0"     # release builds stamp the real version above (packaging/stamp_version.py)


def _git_version() -> str:
    """Running from a source checkout: the latest release tag, e.g. "1.4.1-dev"."""
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists(os.path.join(root, ".git")):
        return ""
    try:
        out = subprocess.run(["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"], cwd=root,
                             capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL,
                             creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        return ""
    tag = out.stdout.strip().lstrip("v")
    return f"{tag}-dev" if out.returncode == 0 and tag else ""


if __version__ == _UNSTAMPED:
    __version__ = _git_version() or __version__
