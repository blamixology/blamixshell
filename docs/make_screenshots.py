"""Makes the README screenshots from the real windows, filled with demo data (no server needed):

    python docs/make_screenshots.py            # writes docs/screens/*.png (desktop) and *.svg (terminal)

Run it on a machine with a desktop (real fonts); the windows render off-screen, nothing pops up."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
OUT = ROOT / "docs" / "screens"

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from blamixshell import dashboard as d, docker, packages, theme  # noqa: E402

SIZE = (1320, 780)


class DemoPane(QObject):
    state_changed = Signal(object)
    state = "connected"
    session = None
    server = SimpleNamespace(id="demo", label="web-1", address="deploy@web-1.example.com:22", username="deploy",
                             port=22)


def snap(widget, name: str, height: int = SIZE[1]) -> None:
    widget.resize(SIZE[0], height)
    QApplication.processEvents()
    QApplication.processEvents()
    path = OUT / f"{name}.png"
    widget.grab().save(str(path))
    print("wrote", path.relative_to(ROOT))


def dashboard():
    from blamixshell import dashboard_ui as ui
    w = ui.DashboardWindow(DemoPane(), settings={"dashboard_install_updates": True})
    w.refresh = lambda force=False: None
    w._job = lambda key, fn: None
    w.send_to_terminal = lambda text: None
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    w.updated_lbl.setText("Updated 14:32:08")
    return w, ui


def overview(w) -> None:
    disks = [d.Disk("/", "ext4", 80_000_000, 51_000_000, 29_000_000), d.Disk("/var/lib/docker", "xfs", 200_000_000,
             168_000_000, 32_000_000), d.Disk("/boot", "ext4", 1_000_000, 310_000, 690_000),
             d.Disk("/data", "xfs", 500_000_000, 460_000_000, 40_000_000)]
    for cpu, mem in ((22, 61), (35, 62), (28, 62), (41, 63), (37, 63), (55, 64), (48, 64), (31, 63), (26, 63)):
        ov = d.Overview(host="web-1", os="Debian GNU/Linux 12 (bookworm)", kernel="6.1.0-25-amd64", arch="x86_64",
                        uptime_s=36 * 86400 + 5 * 3600, load=(1.12, 0.94, 0.88), cpus=4, cpu_percent=cpu,
                        mem_total_kb=8_000_000, mem_avail_kb=int(8_000_000 * (1 - mem / 100)),
                        swap_total_kb=2_000_000, swap_free_kb=1_850_000, disks=disks, sessions=2, systemd="degraded",
                        init="systemd", failed_units=["backup-offsite.service"])
        w._show_overview(ov)


SERVICES = [d.Service(u, "loaded", a, s, desc, en, "systemd") for u, a, s, desc, en in (
    ("backup-offsite.service", "failed", "failed", "Nightly off-site backup", "enabled"),
    ("containerd.service", "active", "running", "containerd container runtime", "enabled"),
    ("cron.service", "active", "running", "Regular background program processing daemon", "enabled"),
    ("docker.service", "active", "running", "Docker Application Container Engine", "enabled"),
    ("fail2ban.service", "active", "running", "Fail2Ban Service", "enabled"),
    ("nginx.service", "active", "running", "A high performance web server and a reverse proxy server", "enabled"),
    ("php8.2-fpm.service", "active", "running", "The PHP 8.2 FastCGI Process Manager", "enabled"),
    ("postgresql@16-main.service", "active", "running", "PostgreSQL Cluster 16-main", "enabled-runtime"),
    ("redis-server.service", "active", "running", "Advanced key-value store", "enabled"),
    ("ssh.service", "active", "running", "OpenBSD Secure Shell server", "enabled"),
    ("systemd-journald.service", "active", "running", "Journal Service", "static"),
    ("ufw.service", "active", "exited", "Uncomplicated firewall", "enabled"),
    ("unattended-upgrades.service", "active", "running", "Unattended Upgrades Shutdown", "enabled"),
    ("apache2.service", "inactive", "dead", "The Apache HTTP Server", "disabled"))]

LOG = """2026-10-09T14:02:11+0300 web-1 systemd[1]: Starting backup-offsite.service - Nightly off-site backup...
2026-10-09T14:02:12+0300 web-1 backup.sh[48211]: dumping database shop (1.8 GB)
2026-10-09T14:05:40+0300 web-1 backup.sh[48211]: uploading to s3://acme-backups/web-1/2026-10-09.tar.zst
2026-10-09T14:06:03+0300 web-1 backup.sh[48211]: upload timed out after 20s, retrying (1/3)
2026-10-09T14:06:31+0300 web-1 backup.sh[48211]: upload timed out after 20s, retrying (2/3)
2026-10-09T14:07:02+0300 web-1 backup.sh[48211]: ERROR: could not reach s3.eu-central-1.amazonaws.com: connection refused
2026-10-09T14:07:02+0300 web-1 systemd[1]: backup-offsite.service: Main process exited, code=exited, status=1/FAILURE
2026-10-09T14:07:02+0300 web-1 systemd[1]: backup-offsite.service: Failed with result 'exit-code'.
2026-10-09T14:07:02+0300 web-1 systemd[1]: Failed to start backup-offsite.service - Nightly off-site backup.
2026-10-09T14:10:01+0300 web-1 CRON[48731]: (www-data) CMD (php /srv/shop/artisan schedule:run)
2026-10-09T14:12:44+0300 web-1 nginx[1022]: [warn] upstream server temporarily disabled while reading response header
2026-10-09T14:12:44+0300 web-1 sshd[48802]: Accepted publickey for deploy from 203.0.113.24 port 53122 ssh2
2026-10-09T14:13:09+0300 web-1 sshd[48840]: Failed password for invalid user admin from 198.51.100.7 port 41870 ssh2
2026-10-09T14:13:12+0300 web-1 fail2ban.actions[901]: NOTICE [sshd] Ban 198.51.100.7
2026-10-09T14:15:01+0300 web-1 CRON[48911]: (root) CMD (command -v debian-sa1 > /dev/null && debian-sa1 1 1)
2026-10-09T14:20:01+0300 web-1 systemd[1]: Started session-412.scope - Session 412 of User deploy."""

DOCKER = "\n".join(["@@engine", "docker", "@@ps"] + [
    '{"ID":"%s","Names":"%s","Image":"%s","Status":"%s","State":"%s","Ports":"%s","Labels":"%s"}' % row for row in (
        ("a1b2c3d4e5f6", "shop-web-1", "nginx:1.27-alpine", "Up 6 days", "running", "0.0.0.0:443->443/tcp",
         "com.docker.compose.project=shop,com.docker.compose.service=web,com.docker.compose.project.working_dir=/srv/shop,"
         "com.docker.compose.project.config_files=/srv/shop/compose.yaml"),
        ("b2c3d4e5f6a1", "shop-app-1", "acme/shop:4.12.0", "Up 6 days (healthy)", "running", "9000/tcp",
         "com.docker.compose.project=shop,com.docker.compose.service=app,com.docker.compose.project.working_dir=/srv/shop,"
         "com.docker.compose.project.config_files=/srv/shop/compose.yaml"),
        ("c3d4e5f6a1b2", "shop-db-1", "postgres:16", "Up 6 days", "running", "5432/tcp",
         "com.docker.compose.project=shop,com.docker.compose.service=db,com.docker.compose.project.working_dir=/srv/shop,"
         "com.docker.compose.project.config_files=/srv/shop/compose.yaml"),
        ("d4e5f6a1b2c3", "shop-worker-1", "acme/shop:4.12.0", "Exited (137) 2 hours ago", "exited", "",
         "com.docker.compose.project=shop,com.docker.compose.service=worker,com.docker.compose.project.working_dir=/srv/shop,"
         "com.docker.compose.project.config_files=/srv/shop/compose.yaml"),
        ("e5f6a1b2c3d4", "grafana", "grafana/grafana:11.2.0", "Up 3 weeks", "running", "127.0.0.1:3000->3000/tcp", ""),
        ("f6a1b2c3d4e5", "uptime-kuma", "louislam/uptime-kuma:1", "Up 3 weeks (healthy)", "running",
         "127.0.0.1:3001->3001/tcp", ""),
        ("0a1b2c3d4e5f", "certbot-renew", "certbot/certbot", "Exited (0) 9 hours ago", "exited", "", ""))] + [
    "@@stats"] + ['{"ID":"%s","CPUPerc":"%s","MemUsage":"%s / 7.6GiB"}' % r for r in (
        ("a1b2c3d4e5f6", "0.42%", "18.4MiB"), ("b2c3d4e5f6a1", "37.80%", "612MiB"), ("c3d4e5f6a1b2", "6.10%", "1.21GiB"),
        ("e5f6a1b2c3d4", "1.30%", "148MiB"), ("f6a1b2c3d4e5", "2.04%", "96MiB"))] + [
    "@@images"] + ['{"ID":"%s","Repository":"%s","Tag":"%s","Size":"%s","CreatedSince":"%s"}' % r for r in (
        ("9e1b2c3d4e5f", "nginx", "1.27-alpine", "48.3MB", "3 weeks ago"), ("7a8b9c0d1e2f", "acme/shop", "4.12.0", "412MB",
        "6 days ago"), ("6b7c8d9e0f1a", "acme/shop", "4.11.3", "409MB", "4 weeks ago"),
        ("5c6d7e8f9a0b", "postgres", "16", "435MB", "2 months ago"), ("4d5e6f7a8b9c", "grafana/grafana", "11.2.0", "476MB",
        "5 weeks ago"), ("3e4f5a6b7c8d", "louislam/uptime-kuma", "1", "502MB", "2 months ago"),
        ("2f3a4b5c6d7e", "certbot/certbot", "latest", "118MB", "7 weeks ago"), ("1a2b3c4d5e6f", "<none>", "<none>",
        "409MB", "4 weeks ago"))] + [
    "@@volumes", '{"Driver":"local","Name":"shop_pgdata","Mountpoint":"/var/lib/docker/volumes/shop_pgdata/_data"}',
    '{"Driver":"local","Name":"grafana-data","Mountpoint":"/var/lib/docker/volumes/grafana-data/_data"}',
    "@@networks", '{"ID":"11aa22bb33cc","Name":"bridge","Driver":"bridge","Scope":"local"}',
    '{"ID":"44dd55ee66ff","Name":"shop_default","Driver":"bridge","Scope":"local"}',
    '{"ID":"77aa88bb99cc","Name":"host","Driver":"host","Scope":"local"}',
    "@@host", "4", "MemTotal:        8000000 kB", "@@compose", "docker compose", ""])


UFW = """@@manager
ufw
@@state
Status: active
Logging: on (low)
@@rules
Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     LIMIT IN    Anywhere
[ 2] 80/tcp                     ALLOW IN    Anywhere
[ 3] 443/tcp                    ALLOW IN    Anywhere
[ 4] 5432/tcp                   ALLOW IN    10.20.0.0/16
[ 5] 9100/tcp                   ALLOW IN    10.20.0.15
[ 6] 3306/tcp                   DENY IN     Anywhere
[ 7] 22/tcp (v6)                LIMIT IN    Anywhere (v6)
[ 8] 80/tcp (v6)                ALLOW IN    Anywhere (v6)
[ 9] 443/tcp (v6)               ALLOW IN    Anywhere (v6)
"""


def desktop() -> None:
    w, ui = dashboard()
    overview(w)
    w._show_services((SERVICES, ""))
    w.tabs.setCurrentIndex(0)
    snap(w, "dashboard-overview", 560)

    w.tabs.setCurrentIndex(1)
    snap(w, "dashboard-services")

    w.tabs.setCurrentIndex(ui.TAB_KEYS.index("logs"))
    w.log_unit.setCurrentText("")
    w._show_logs(LOG)
    w.log_only.setCurrentIndex(w.log_only.findData("warn"))
    w.log_around.setCurrentIndex(w.log_around.findData(0))
    snap(w, "dashboard-logs", 520)

    w.tabs.setCurrentIndex(ui.TAB_KEYS.index("docker"))
    w._show_docker((DOCKER, False))
    w.dk_table.selectRow(1)
    snap(w, "dashboard-docker", 560)
    w.dk_tabs.setCurrentIndex(1)
    w.dk_cmp_table.selectRow(0)
    w.dk_svc_table.selectRow(3)
    snap(w, "dashboard-compose")

    from test_security2 import RICH
    w.tabs.setCurrentIndex(ui.TAB_KEYS.index("security"))
    w._show_security(RICH)
    snap(w, "dashboard-security")

    w.tabs.setCurrentIndex(ui.TAB_KEYS.index("updates"))
    w._show_updates(("apt", [d.Update(p, v) for p, v in (
        ("libssl3", "3.0.15-1~deb12u1"), ("openssl", "3.0.15-1~deb12u1"), ("nginx", "1.22.1-9+deb12u1"),
        ("linux-image-amd64", "6.1.112-1"), ("tzdata", "2024b-0+deb12u1"))]))
    from test_packages import APT
    w.pkg_query.setText("htop")
    w._show_packages(("htop", packages.parse_search("apt", APT, "htop")))
    w.pkg_table.selectRow(1)
    snap(w, "dashboard-updates")

    w.tabs.setCurrentIndex(ui.TAB_KEYS.index("firewall"))
    w._show_firewall(UFW)
    w.fw_table.selectRow(4)
    snap(w, "dashboard-firewall", 600)
    w.close()


def editor() -> None:
    from test_editor import FakeSftp, session, wait
    from blamixshell.editor import EditorWindow
    conf = (b"# shop: TLS front end, API behind it\nserver {\n    listen 443 ssl http2;\n    server_name shop.example.com;"
            b"\n    root /srv/shop/public;\n\n    ssl_certificate     /etc/letsencrypt/live/shop/fullchain.pem;\n"
            b"    ssl_certificate_key /etc/letsencrypt/live/shop/privkey.pem;\n\n    location /api/ {\n"
            b"        proxy_pass http://127.0.0.1:9000;\n        proxy_set_header Host $host;\n"
            b"        proxy_set_header X-Real-IP $remote_addr;\n        proxy_read_timeout 60s;\n    }\n\n"
            b"    location ~* \\.(css|js|png|svg|woff2)$ {\n        expires 30d;\n        access_log off;\n    }\n}\n")
    win = EditorWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    sess = session(FakeSftp({"/etc/nginx/sites-enabled/shop.conf": conf}))
    sess.server.label = "web-1"
    tab = win.open(sess, "/etc/nginx/sites-enabled/shop.conf")
    wait(QApplication.instance(), lambda: tab.loaded)
    tab.findbar.open(True)
    tab.findbar.find.setText("proxy_set_header")
    tab.findbar.repl.setText("proxy_set_header")
    tab.ed.setFocus()
    tab.findbar.go(False)
    snap(win, "editor", 640)
    readme = b"""# shop: runbook

The web shop on **web-1**: nginx in front, the app and PostgreSQL in Docker Compose (`/srv/shop`).

## Deploy a new version

1. Pull the image: `docker compose pull app`
2. Re-create it: `docker compose up -d app`
3. Check the health: `curl -fsS https://shop.example.com/health`

## Who to call

| What | Who | When |
|---|---|---|
| Database | Ana | 08-18 |
| Payments | Vlad | any time |

## Checklist

- [x] Backups run nightly at 02:00
- [x] Certificates renew by themselves (certbot)
- [ ] Move logs to the central server

```bash
docker compose logs --tail 100 app
```
"""
    sess2 = session(FakeSftp({"/srv/shop/README.md": readme}))
    sess2.server.label = "web-1"
    from blamixshell.editor import EditorTab
    EditorTab.md_view = "split"
    tab2 = win.open(sess2, "/srv/shop/README.md")
    wait(QApplication.instance(), lambda: tab2.loaded)
    win.resize(SIZE[0], 640)
    tab2.set_view("split")
    snap(win, "editor-markdown", 640)
    win.close()


def terminal_dashboard() -> None:
    """The terminal dashboard as SVG (Textual's own screenshot)."""
    from unittest import mock
    from test_collect import FakeRunner
    from blamixshell import collect
    from blamixshell.tui_dash import DashApp
    server = SimpleNamespace(label="web-1", address="deploy@web-1.example.com:22")
    ctx = collect.Context(FakeRunner({docker.READ_SCRIPT: DOCKER}), username="deploy", root=True)

    async def main():
        app = DashApp(server, ctx)
        async with app.run_test(size=(150, 24)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if hasattr(app.screen, "pane") and app.screen.pane("overview").loaded:
                    break
            for tab in ("compose", "docker"):
                app.screen.query_one("TabbedContent").active = f"tab-{tab}"
                for _ in range(60):
                    await pilot.pause(0.05)
                    if app.screen.pane(tab).loaded:
                        break
                app.save_screenshot(f"dash-{tab}.svg", str(OUT))
                print("wrote", f"docs/screens/dash-{tab}.svg")
    ov = d.Overview(host="web-1", os="Debian 12", cpus=4, cpu_percent=31, load=(1.1, 0.9, 0.8), mem_total_kb=8_000_000,
                    mem_avail_kb=3_000_000)
    with mock.patch.object(d, "overview", lambda r: ov), mock.patch.object(d, "services", lambda r: (SERVICES, "")):
        asyncio.run(main())


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    theme.set_theme("Midnight")
    theme.apply_palette(app)
    desktop()
    editor()
    try:
        terminal_dashboard()
    except ImportError:
        print("textual isn't installed: no terminal screenshots")
    os._exit(0)
