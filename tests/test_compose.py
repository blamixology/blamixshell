"""Docker Compose: projects found from container labels, the commands (run in the project's folder with its own
files), the checked save of a compose file, and the Compose tab."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import compose, docker  # noqa: E402


def row(cid, name, image, status, state, project="", service="", folder="/srv/shop",
        files="/srv/shop/docker-compose.yml,/srv/shop/docker-compose.prod.yml"):
    labels = ""
    if project:
        labels = (f"com.docker.compose.project={project},com.docker.compose.service={service},"
                  f"com.docker.compose.project.working_dir={folder},com.docker.compose.project.config_files={files},"
                  "com.docker.compose.version=2.35.1")
    return json.dumps({"ID": cid, "Names": name, "Image": image, "Status": status, "State": state, "Labels": labels})


PS = "\n".join([
    "@@engine", "docker", "@@ps",
    row("aaaaaaaaaaaa", "shop-web-1", "nginx:1.25", "Up 2 hours", "running", "shop", "web"),
    row("bbbbbbbbbbbb", "shop-db-1", "postgres:16", "Up 2 hours", "running", "shop", "db"),
    row("cccccccccccc", "shop-worker-1", "shop/worker", "Exited (1) 5 minutes ago", "exited", "shop", "worker"),
    row("dddddddddddd", "shop-worker-2", "shop/worker", "Up 3 minutes", "running", "shop", "worker"),
    row("eeeeeeeeeeee", "blog-app-1", "ghost:5", "Exited (0) 1 day ago", "exited", "blog", "app",
        folder="/home/me/blog", files="/home/me/blog/compose.yaml"),
    row("ffffffffffff", "lonely", "alpine", "Up 1 minute", "running"),
    "@@compose", "docker compose", ""])


def test_projects_and_services_from_labels():
    c = docker.parse(PS)
    assert c.compose == "docker compose"
    ps = compose.projects(c.items)
    assert [p.name for p in ps] == ["shop", "blog"]                                  # running ones first
    shop, blog = ps
    assert shop.folder == "/srv/shop" and shop.files == ["/srv/shop/docker-compose.yml",
                                                         "/srv/shop/docker-compose.prod.yml"]
    assert shop.state == "partly running (3 of 4)" and blog.state == "stopped"
    svc = {s.name: s for s in shop.services}
    assert list(svc) == ["db", "web", "worker"]
    assert svc["web"].state == "running" and svc["worker"].state == "1 of 2 running" and svc["db"].image == "postgres:16"
    assert blog.files == ["/home/me/blog/compose.yaml"] and blog.manageable


def test_podman_labels_and_old_or_missing_compose():
    podman = ('@@engine\npodman\n@@ps\n{"ID":"1","Names":["web"],"Image":"nginx","Status":"Up","State":"running",'
              '"Labels":{"com.docker.compose.project":"site","com.docker.compose.service":"web"}}\n'
              '@@compose\npodman-compose\n')
    c = docker.parse(podman)
    p = compose.projects(c.items)[0]
    assert c.compose == "podman-compose" and p.name == "site" and not p.manageable      # no folder/files labels
    try:
        compose.action_command(c.compose, p, "up")
        raise AssertionError("must refuse")
    except ValueError as e:
        assert "can't be managed" in str(e)
    assert docker.parse("@@engine\ndocker\n@@ps\n@@compose\nrm -rf /\n").compose == ""   # only known tools


def test_commands_run_in_the_project_folder_with_its_files():
    shop = compose.projects(docker.parse(PS).items)[0]
    up = compose.action_command("docker compose", shop, "up")
    assert up == ("sh -c 'cd /srv/shop && docker compose -p shop -f /srv/shop/docker-compose.yml "
                  "-f /srv/shop/docker-compose.prod.yml up -d 2>&1'")
    upd = compose.action_command("docker-compose", shop, "update")
    assert "docker-compose -p shop" in upd and "pull 2>&1 && cd /srv/shop" in upd and upd.endswith("up -d 2>&1'")
    assert compose.action_command("docker compose", shop, "restart", "worker").endswith("restart worker 2>&1'")
    assert "up -d --force-recreate web" in compose.recreate_command("docker compose", shop, "web")
    assert "logs --no-color --timestamps --tail 300 db" in compose.logs_command("docker compose", shop, "db")
    assert compose.config_command("docker compose", shop).endswith(" config 2>&1'")
    for bad in (lambda: compose.action_command("docker compose", shop, "rm"),
                lambda: compose.action_command("sudo rm", shop, "up"),
                lambda: compose.action_command("docker compose", shop, "restart", "x; reboot")):
        try:
            bad()
            raise AssertionError(bad)
        except ValueError:
            pass


def test_files_are_read_and_saved_only_when_compose_accepts_them():
    shop = compose.projects(docker.parse(PS).items)[0]
    assert compose.read_files_command(shop).count("cat ") == 2
    files = compose.parse_files("@@/srv/shop/docker-compose.yml\nservices:\n  web: {}\n@@/srv/shop/docker-compose.prod.yml\n"
                                "services: {}\n")
    assert files == {"/srv/shop/docker-compose.yml": "services:\n  web: {}\n",
                     "/srv/shop/docker-compose.prod.yml": "services: {}\n"}
    cmd = compose.save_file_command("docker compose", shop, "/srv/shop/docker-compose.prod.yml", "services: {}\n", "T")
    assert "-f /srv/shop/docker-compose.yml -f /srv/shop/docker-compose.prod.yml.blamixshell-new config -q" in cmd
    assert "docker-compose.prod.yml.bak-T" in cmd and "Not saved" in cmd
    try:
        compose.save_file_command("docker compose", shop, "/etc/passwd", "x")
        raise AssertionError("only the project's own files")
    except ValueError:
        pass


def test_the_compose_tab():
    from PySide6.QtWidgets import QDialog
    from blamixshell import dashboard_ui as ui
    from test_dashboard_fw_details import make
    w, calls = make()
    jobs = []
    w._job = lambda key, fn: jobs.append(key)
    w._show_docker((PS, False))
    assert w.dk_tabs.tabText(1) == "Compose (2)" and "Using “docker compose”" in w.dk_cmp_note.text()
    w.dk_cmp_table.selectRow(0)                                                   # shop
    assert w.dk_svc_table.rowCount() == 3 and all(b.isEnabled() for b in w.dk_cmp_buttons.values())
    assert not w.dk_svc_buttons["logs"].isEnabled()                               # no service picked yet
    w._cmp_action("update")
    assert calls[-1][0] == "Update shop" and "pull 2>&1 &&" in calls[-1][1]
    w.dk_svc_table.selectRow(2)                                                   # worker
    w._cmp_action("restart", service=True)
    assert calls[-1][0] == "Restart shop / worker"
    w._cmp_view("logs", service=True)
    assert jobs == ["dkview"]
    w._cmp_files()
    assert jobs[-1] == "cmpfiles"

    text = "@@/srv/shop/docker-compose.yml\nservices:\n  web: {}\n@@/srv/shop/docker-compose.prod.yml\nservices: {}\n"

    def edit_and_save(dlg):
        dlg.edits["/srv/shop/docker-compose.prod.yml"].setPlainText("services:\n  web:\n    restart: always\n")
        return QDialog.Accepted
    with mock.patch.object(ui._ComposeFileDialog, "exec", edit_and_save):
        w._show_cmpfiles(("shop", text))
    what, cmd = calls[-1]
    assert what == "Save docker-compose.prod.yml and apply"
    assert cmd.startswith("sh -c ") and "config -q" in cmd and "up -d" in cmd             # one command for sudo

    w._show_docker((PS.replace("@@compose\ndocker compose", "@@compose\n"), False))       # no compose tool
    w.dk_cmp_table.selectRow(0)
    assert not any(b.isEnabled() for b in w.dk_cmp_buttons.values()) and "no compose command" in w.dk_cmp_note.text()
