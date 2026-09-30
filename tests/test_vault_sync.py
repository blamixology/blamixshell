"""Vault backups, sharing one vault between computers (merge), import, colors, location."""
import os
import time

import pytest

from blamixshell import paths
from blamixshell.models import Server, Snippet, Store, merge_vault_data
from blamixshell.vault import PasswordChangedElsewhere, Vault


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BLAMIXSHELL_HOME", str(tmp_path / "home"))
    return tmp_path


def _two_computers(tmp_path):
    """Two Stores opened on the same vault file, like two PCs sharing it via OneDrive."""
    path = tmp_path / "shared" / "vault.sdv"
    a = Store(Vault.create(path, "masterpass", n_log2=10), {}, backup_dir=tmp_path / "bk-a")
    a.upsert(Server(name="web", host="10.0.0.1"))
    v, data = Vault.open(path, "masterpass")
    b = Store(v, data, backup_dir=tmp_path / "bk-b")
    return a, b, path


def _bump(path):
    # make sure the other side's write gets a newer mtime (coarse filesystem clocks)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def test_both_computers_add_servers_nothing_lost(tmp_path):
    a, b, path = _two_computers(tmp_path)
    b.upsert(Server(name="db", host="10.0.0.2"))            # PC B saves first
    _bump(path)
    msgs = []
    a.notify = msgs.append
    a.upsert(Server(name="cache", host="10.0.0.3"))         # PC A didn't reload: must merge, not overwrite
    names = {s.name for s in a.servers.values()}
    assert names == {"web", "db", "cache"}
    assert msgs and "another computer" in msgs[0]
    _v, on_disk = Vault.open(path, "masterpass")
    assert {s["name"] for s in on_disk["servers"]} == {"web", "db", "cache"}
    assert any("before-merge" in p.name for p in a.backups())


def test_reload_picks_up_edits_and_deletions(tmp_path):
    a, b, path = _two_computers(tmp_path)
    web = next(iter(a.servers.values()))
    web_b = b.servers[web.id]
    web_b.port = 2200
    b.upsert(web_b)
    b.upsert(Server(name="db", host="10.0.0.2"))
    _bump(path)
    assert a.reload_if_changed() is True
    assert a.servers[web.id].port == 2200 and len(a.servers) == 2
    db_id = next(s.id for s in b.servers.values() if s.name == "db")
    b.delete(db_id)
    _bump(path)
    assert a.reload_if_changed() and db_id not in a.servers
    assert a.reload_if_changed() is False                   # nothing new


def test_merge_rules():
    base = {"servers": [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}], "snippets": [], "groups": ["G"]}
    ours = {"servers": [{"id": "1", "name": "a-ours"}, {"id": "2", "name": "b"}], "snippets": [], "groups": ["G", "Mine"]}
    theirs = {"servers": [{"id": "1", "name": "a-theirs"}], "snippets": [], "groups": ["Theirs"]}
    merged, conflicts = merge_vault_data(base, ours, theirs)
    got = {d["id"]: d["name"] for d in merged["servers"]}
    assert got == {"1": "a-ours"}          # both edited 1: ours wins; they deleted 2 (unchanged here): gone
    assert conflicts == 1
    assert merged["groups"] == ["Mine", "Theirs"]   # G deleted by them, kept additions from both
    # a deletion never beats an edit
    merged, _ = merge_vault_data(base, {"servers": [{"id": "1", "name": "a"}], "snippets": []},
                                 {"servers": [{"id": "1", "name": "a"}, {"id": "2", "name": "b-edited"}],
                                  "snippets": []})
    assert {d["name"] for d in merged["servers"]} == {"a", "b-edited"}


def test_password_changed_on_other_computer_is_not_overwritten(tmp_path):
    a, b, path = _two_computers(tmp_path)
    b.change_password("new-password")
    _bump(path)
    with pytest.raises(PasswordChangedElsewhere):
        a.reload_if_changed()
    with pytest.raises(PasswordChangedElsewhere):
        a.upsert(Server(name="x", host="x"))
    Vault.open(path, "new-password")                          # B's file is intact


def test_daily_backup_rotation_and_manual(tmp_path, monkeypatch):
    from blamixshell import models
    st = Store(Vault.create(tmp_path / "v.sdv", "masterpass", n_log2=10), {}, backup_dir=tmp_path / "bk")
    st.upsert(Server(host="a"))
    st.upsert(Server(host="b"))
    assert len(st.backups()) == 1                             # at most one automatic backup a day
    monkeypatch.setattr(models, "BACKUPS_KEEP", 3)
    for i in range(5):
        st.backup_now(f"manual{i}")
    assert len(st.backups()) == 3
    b = st.backups()[0]
    assert Vault.open(b, "masterpass")                        # a backup opens with the same password


def test_import_vault_adds_without_overwriting(tmp_path):
    other = Store(Vault.create(tmp_path / "other.sdv", "otherpass", n_log2=10), {}, backup_dir=tmp_path / "bk2")
    other.upsert(Server(name="new-one", host="10.9.9.9", group="Imported"))
    other.upsert(Server(name="dup", host="10.0.0.1"))
    other.snippets.append(Snippet("uptime", "uptime"))
    other.group_colors["Imported"] = "#ff6b6b"
    other.save()
    st = Store(Vault.create(tmp_path / "v.sdv", "masterpass", n_log2=10), {}, backup_dir=tmp_path / "bk")
    st.upsert(Server(name="web", host="10.0.0.1"))
    added, snips = st.import_vault(tmp_path / "other.sdv", "otherpass")
    assert added == 1 and snips == 1
    assert {s.name for s in st.servers.values()} == {"web", "new-one"}
    assert st.group_colors["Imported"] == "#ff6b6b"


def test_group_colors_inherit_and_follow_renames(tmp_path):
    st = Store(Vault.create(tmp_path / "v.sdv", "masterpass", n_log2=10), {}, backup_dir=tmp_path / "bk")
    a = Server(name="api", host="a", group="Prod/EU")
    b = Server(name="db", host="b", group="Prod/EU", color="#69db7c")
    st.upsert(a)
    st.upsert(b)
    assert st.color_for(a) == ""
    st.set_group_color("Prod", "#ff6b6b")
    assert st.color_for(a) == "#ff6b6b" and st.color_for(b) == "#69db7c"   # own color wins
    st.set_group_color("Prod/EU", "#ffa94d")
    assert st.color_for(a) == "#ffa94d"                                     # nearest group wins
    st.rename_group("Prod", "Production")
    assert st.group_colors == {"Production": "#ff6b6b", "Production/EU": "#ffa94d"}
    _v, data = Vault.open(tmp_path / "v.sdv", "masterpass")
    assert data["group_colors"]["Production"] == "#ff6b6b"                  # persisted
    st.delete_group("Production/EU")
    assert "Production/EU" not in st.group_colors


def test_vault_location_pointer(tmp_path):
    assert paths.vault_path() == paths.default_vault_path()
    elsewhere = tmp_path / "OneDrive" / "BlamixShell" / "vault.sdv"
    paths.set_vault_path(elsewhere)
    assert paths.vault_path() == elsewhere
    paths.set_vault_path(None)
    assert paths.vault_path() == paths.default_vault_path()
