"""Docker / Podman troubleshooting: images, volumes and networks, a readable `inspect`, the read-only views and the
clean-up commands, and the Docker tab's buttons."""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import docker  # noqa: E402
from test_features3 import PS  # noqa: E402

FULL = PS + (
    '@@images\n'
    '{"Containers":"N/A","CreatedSince":"3 weeks ago","ID":"sha256:9e1b2c3d4e5f60","Repository":"nginx","Tag":"1.25",'
    '"Size":"187MB"}\n'
    '{"CreatedSince":"2 months ago","ID":"7a8b9c0d1e2f","Repository":"alpine","Tag":"latest","Size":"7.8MB"}\n'
    '{"CreatedSince":"5 days ago","ID":"0f0f0f0f0f0f","Repository":"<none>","Tag":"<none>","Size":"412MB"}\n'
    '{"CreatedSince":"1 year ago","ID":"abcabcabcabc","Repository":"redis","Tag":"7","Size":"130MB"}\n'
    '@@volumes\n{"Driver":"local","Name":"pgdata","Mountpoint":"/var/lib/docker/volumes/pgdata/_data"}\n'
    '@@networks\n{"ID":"1111aaaa2222bbbb","Name":"bridge","Driver":"bridge","Scope":"local"}\n'
    '{"ID":"3333cccc4444","Name":"app_default","Driver":"bridge","Scope":"local"}\n'
    '{"ID":"5555","Name":"host","Driver":"host","Scope":"local"}\n')

INSPECT = json.dumps([{
    "Name": "/worker", "RestartCount": 7,
    "State": {"Status": "exited", "ExitCode": 137, "OOMKilled": True, "Error": "", "StartedAt": "2026-10-08T10:00:00Z",
              "FinishedAt": "2026-10-08T10:05:00Z",
              "Health": {"Status": "unhealthy", "FailingStreak": 3,
                         "Log": [{"ExitCode": 1, "Output": "curl: (7) Failed to connect"}]}},
    "Config": {"Image": "acme/worker:2", "Cmd": ["python", "run.py"], "Entrypoint": None, "WorkingDir": "/app",
               "Env": ["PATH=/usr/bin", "DB_PASSWORD=hunter2", "API_TOKEN=abc123", "MODE=prod"]},
    "HostConfig": {"Memory": 268435456, "NanoCpus": 1500000000, "RestartPolicy": {"Name": "unless-stopped"}},
    "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]},
                        "Networks": {"app_default": {"IPAddress": "172.18.0.4"}}},
    "Mounts": [{"Type": "volume", "Name": "pgdata", "Destination": "/data", "RW": True},
               {"Type": "bind", "Source": "/etc/acme", "Destination": "/config", "RW": False}],
}])


def test_images_volumes_networks_and_who_uses_an_image():
    c = docker.parse(FULL)
    assert [(i.repository, i.tag, i.id) for i in c.images] == [
        ("alpine", "latest", "7a8b9c0d1e2f"), ("nginx", "1.25", "9e1b2c3d4e5f"), ("redis", "7", "abcabcabcabc"),
        ("<none>", "<none>", "0f0f0f0f0f0f")]                                     # dangling last
    used = {i.ref: i.used_by for i in c.images}
    assert used["nginx:1.25"] == ["web"] and used["alpine:latest"] == ["old-job"] and used["redis:7"] == []
    assert c.images[-1].dangling and c.images[-1].ref == "0f0f0f0f0f0f"
    assert c.volumes[0].name == "pgdata" and c.volumes[0].mountpoint.endswith("_data")
    assert [(n.name, n.builtin) for n in c.networks] == [("bridge", True), ("app_default", False), ("host", True)]
    assert docker.parse(PS).images == []                                          # older output: nothing breaks


def test_podman_image_shape():
    podman = ('@@engine\npodman\n@@ps\n@@images\n'
              '{"Id":"sha256:deadbeefcafe11","Names":["docker.io/library/nginx:1.25"],"Size":187000000,'
              '"CreatedAt":"2026-09-01"}\n')
    img = docker.parse(podman).images[0]
    assert (img.repository, img.tag, img.id) == ("docker.io/library/nginx", "1.25", "deadbeefcafe") and "MB" in img.size


def test_inspect_summary_says_why_it_stopped_and_hides_secrets():
    text = docker.summarize_inspect(INSPECT)
    head, _, raw = text.partition("──── full inspect output ────")
    assert "OOM-killed" in head and "137  (killed (SIGKILL: out of memory, or docker kill))" in head
    assert "Restarts          7" in head and "unless-stopped" in head and "unhealthy (3 failing in a row)" in head
    assert "curl: (7) Failed to connect" in head and "Memory limit      256 MB" in head and "1.5 CPUs" in head
    assert "0.0.0.0:18080 → 8080/tcp" in head and "app_default (172.18.0.4)" in head
    assert "bind /etc/acme → /config (read-only)" in head and "Command           python run.py" in head
    assert "MODE=prod" in head and "DB_PASSWORD=•••• (hidden)" in head and "API_TOKEN=•••• (hidden)" in head
    assert "hunter2" not in text and "abc123" not in text                            # not in the raw JSON either
    assert '"OOMKilled": true' in raw
    assert docker.summarize_inspect("Error: No such container: x") == "Error: No such container: x"
    image = docker.summarize_inspect(json.dumps([{"Id": "sha256:1", "RepoTags": ["nginx:1.25"]}]))
    assert image.startswith("{") and '"nginx:1.25"' in image                       # images etc.: indented JSON


def test_read_only_and_clean_up_commands():
    assert docker.inspect_command("docker", "container", "web") == "docker container inspect web 2>&1"
    assert docker.top_command("podman", "web") == "podman top web 2>&1"
    assert docker.history_command("docker", "nginx:1.25") == "docker history nginx:1.25 2>&1"
    assert docker.disk_usage_command("docker") == "docker system df -v 2>&1"
    assert "docker events --since 60m --until" in docker.events_command("docker")
    assert "--stream=false" in docker.events_command("podman")
    shell = docker.shell_command("docker", "web", sudo=True)
    assert shell.startswith("sudo docker exec -it web sh -c ") and "exec bash || exec sh" in shell
    assert docker.prune_command("docker", "images") == "docker image prune -f"
    assert docker.prune_command("docker", "unused-images") == "docker image prune -a -f"
    assert docker.remove_image_command("docker", "nginx:1.25") == "docker rmi nginx:1.25"
    assert docker.pull_command("docker", "nginx:1.25") == "docker pull nginx:1.25 2>&1"
    assert docker.remove_network_command("docker", "app_default") == "docker network rm app_default"
    for bad in (lambda: docker.prune_command("podman", "build-cache"), lambda: docker.prune_command("docker", "all"),
                lambda: docker.remove_network_command("docker", "bridge"), lambda: docker.pull_command("docker", "<none>"),
                lambda: docker.pull_command("docker", "x; reboot"), lambda: docker.inspect_command("docker", "secret", "x"),
                lambda: docker.top_command("rkt", "x")):
        try:
            bad()
            raise AssertionError(bad)
        except ValueError:
            pass


def test_the_docker_tab():
    from test_dashboard_fw_details import make
    w, calls = make()
    typed = []
    w.send_to_terminal = typed.append
    jobs = []
    w._job = lambda key, fn: jobs.append(key)
    w._show_docker((FULL, False))
    assert w.dk_tabs.tabText(2) == "Images (4), 1 dangling" and w.dk_tabs.tabText(4) == "Networks (3)"
    w.dk_table.selectRow(0)                                                        # web, running
    b = w.dk_buttons
    assert b["details"].isEnabled() and b["top"].isEnabled() and b["shell"].isEnabled()
    w._dk_details()
    w._dk_shell()
    assert jobs == ["dkview"] and typed and typed[0].startswith("docker exec -it web ")
    w.dk_table.selectRow(2)                                                        # old-job, exited
    assert not b["top"].isEnabled() and not b["shell"].isEnabled() and b["details"].isEnabled()
    names = [w.dk_img_table.item(i, 0).text() for i in range(w.dk_img_table.rowCount())]
    w.dk_img_table.selectRow(names.index("nginx"))
    ib = w.dk_img_buttons
    assert not ib["remove"].isEnabled() and "Used by web" in ib["remove"].toolTip() and ib["pull"].isEnabled()
    w._dk_pull()
    assert calls[-1] == ("Pull nginx:1.25", "docker pull nginx:1.25 2>&1")
    w.dk_img_table.selectRow(names.index("redis"))
    assert ib["remove"].isEnabled()
    w._dk_remove_image()
    assert calls[-1] == ("Remove image redis:7", "docker rmi redis:7")
    w.dk_img_table.selectRow(names.index("<none>"))
    assert not ib["pull"].isEnabled()
    nets = [w.dk_net_table.item(i, 0).text() for i in range(w.dk_net_table.rowCount())]
    w.dk_net_table.selectRow(nets.index("bridge"))
    assert not w.dk_net_buttons["remove"].isEnabled() and w.dk_net_buttons["details"].isEnabled()
    w.dk_net_table.selectRow(nets.index("app_default"))
    assert w.dk_net_buttons["remove"].isEnabled()
    from PySide6.QtWidgets import QMenu
    menu = QMenu()
    w._dk_fill_clean(menu)
    labels = [a.text() for a in menu.actions()]
    assert "Delete the build cache…" in labels and len(labels) == 6
    w._dk_prune("volumes", "Delete volumes no container uses (their data is lost)")
    assert calls[-1][1] == "docker volume prune -f"
