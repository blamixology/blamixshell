"""Command history for terminal autocomplete."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell.cmd_history import MAX_PER_SERVER, CommandHistory  # noqa: E402


def test_add_dedupes_and_orders(tmp_path):
    h = CommandHistory(tmp_path / "h.json")
    for c in ("ls", "git status", "ls"):
        assert h.add("a", c)
    assert h.get("a") == ["git status", "ls"]
    assert h.get("b") == []


def test_persists_and_clears(tmp_path):
    p = tmp_path / "h.json"
    CommandHistory(p).add("a", "uptime")
    h = CommandHistory(p)
    assert h.get("a") == ["uptime"]
    h.clear("a")
    assert CommandHistory(p).get("a") == []


def test_skips_empty_and_huge_and_caps(tmp_path):
    h = CommandHistory(tmp_path / "h.json")
    assert not h.add("a", "   ")
    assert not h.add("a", "x" * 1000)
    for i in range(MAX_PER_SERVER + 20):
        h.add("a", f"cmd {i}")
    assert len(h.get("a")) == MAX_PER_SERVER
    assert h.get("a")[-1] == f"cmd {MAX_PER_SERVER + 19}"


def test_corrupt_file_is_ignored(tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{not json", encoding="utf-8")
    assert CommandHistory(p).get("a") == []
