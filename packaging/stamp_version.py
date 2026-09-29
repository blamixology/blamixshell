"""Stamp the release version (from a git tag like v1.2.3) into the sources before
building, so the app's update check knows which version it is.
Usage: python packaging/stamp_version.py v1.2.3
"""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
m = re.match(r"^v?(\d+\.\d+\.\d+)", sys.argv[1] if len(sys.argv) > 1 else "")
if not m:
    print(f"stamp_version: '{sys.argv[1:]}' is not a version tag, leaving sources unchanged")
    sys.exit(0)
v = m.group(1)
init = root / "blamixshell" / "__init__.py"
init.write_text(re.sub(r'__version__ = "[^"]*"', f'__version__ = "{v}"', init.read_text()))
pp = root / "pyproject.toml"
pp.write_text(re.sub(r'(?m)^version = "[^"]*"', f'version = "{v}"', pp.read_text(), count=1))
print(f"stamp_version: {v}")
