"""Time, reboot, swap, fstab and network: parsing real-looking output and building the commands."""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import collect, report, system as sy  # noqa: E402
from test_collect import FakeRunner  # noqa: E402

SYSTEM = """@@hostname
web-1
@@os
CentOS Linux 7 (Core)
@@kernel
3.10.0-1160.el7.x86_64
@@uptime
90061.12 120000.00
@@time
2026-10-06 10:00:01|EEST|+0300|1791273601
@@timedatectl
      Local time: Tue 2026-10-06 10:00:01 EEST
  Universal time: Tue 2026-10-06 07:00:01 UTC
        RTC time: Tue 2026-10-06 07:00:01
       Time zone: Europe/Bucharest (EEST, +0300)
     NTP enabled: yes
NTP synchronized: no
@@tzfile
@@ntp
active
@@reboot
yes
@@swaps
Filename				Type		Size	Used	Priority
/swapfile                               file		2097148	1500000	-2
/dev/sda2                               partition	1048572	0	-3
@@meminfo
MemTotal:        1015000 kB
SwapTotal:       3145720 kB
SwapFree:        1645720 kB
@@scheduled
"""

MOUNTS = r"""@@fstab
# /etc/fstab
UUID=aaaa-bbbb /                       xfs     defaults        0 0
UUID=cccc-dddd /boot                   xfs     defaults        0 0
/dev/sdb1      /data\040disk           ext4    defaults,nofail 0 2
/dev/sdc1      /backup                 ext4    defaults        0 2
/dev/sdd1      /mnt/usb                vfat    noauto          0 0
/swapfile      none                    swap    sw              0 0
@@mounts
/dev/sda1 / xfs rw,relatime 0 0
/dev/sda1 /boot xfs rw 0 0
/dev/sdb1 /data\040disk ext4 rw 0 0
"""

NETWORK = """@@hostname
web-1.example.com
@@link
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP mode DEFAULT group default qlen 1000\\    link/ether 52:54:00:12:34:56 brd ff:ff:ff:ff:ff:ff
3: eth1: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN mode DEFAULT group default qlen 1000\\    link/ether 52:54:00:12:34:99 brd ff:ff:ff:ff:ff:ff
@@addr
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0\\       valid_lft forever preferred_lft forever
2: eth0    inet6 fe80::5054:ff:fe12:3456/64 scope link \\       valid_lft forever preferred_lft forever
@@route
default via 10.0.0.1 dev eth0 proto dhcp metric 100
10.0.0.0/24 dev eth0 proto kernel scope link src 10.0.0.5
@@dns
nameserver 10.0.0.2
nameserver 1.1.1.1
search example.com
@@noip
no
"""


def test_system_info_is_parsed():
    i = sy.parse_system(SYSTEM)
    assert (i.hostname, i.os, i.kernel) == ("web-1", "CentOS Linux 7 (Core)", "3.10.0-1160.el7.x86_64")
    assert i.uptime_s == 90061.12 and i.local_time == "2026-10-06 10:00:01" and (i.tz_abbr, i.tz_offset) == ("EEST", "+0300")
    assert i.tz_name == "Europe/Bucharest" and i.epoch == 1791273601
    assert i.ntp_synced is False and i.ntp_active is True and i.reboot_required is True
    assert [(s.name, s.kind, s.size_kb, s.used_kb) for s in i.swaps] == [("/swapfile", "file", 2097148, 1500000),
                                                                         ("/dev/sda2", "partition", 1048572, 0)]
    assert i.mem_total_kb == 1015000 and i.swap_total_kb == 3145720 and i.scheduled == ""
    assert i.drift_s(1791273601 - 30) == 30 and sy.SystemInfo().drift_s(1.0) is None


def test_time_zone_falls_back_to_the_files_and_unknown_answers_stay_unknown():
    i = sy.parse_system("@@tzfile\n/usr/share/zoneinfo/Asia/Tokyo\nAsia/Tokyo\n@@reboot\nunknown\n@@ntp\ninactive active\n")
    assert i.tz_name == "Asia/Tokyo" and i.reboot_required is None and i.ntp_active is True and i.ntp_synced is None


def test_fstab_entries_know_what_is_mounted():
    e = {x.mount: x for x in sy.parse_mounts(MOUNTS)}
    assert e["/"].mounted is True and e["/data disk"].mounted is True          # \040 is a space
    assert e["/backup"].mounted is False and e["/mnt/usb"].mounted is False
    assert e["none"].is_swap and e["none"].mounted is None
    assert e["/data disk"].options == "defaults,nofail" and e["/data disk"].passno == "2"


def test_network_is_parsed():
    n = sy.parse_network(NETWORK)
    assert n.hostname == "web-1.example.com" and n.gateway == "10.0.0.1" and n.has_ip_tool
    assert [(i.name, i.state) for i in n.interfaces] == [("lo", "UNKNOWN"), ("eth0", "UP"), ("eth1", "DOWN")]
    eth0 = n.interfaces[1]
    assert eth0.addresses == ["10.0.0.5/24", "fe80::5054:ff:fe12:3456/64"] and eth0.mac == "52:54:00:12:34:56"
    assert eth0.mtu == "1500" and n.interfaces[0].mac == ""                       # no MAC for the loopback
    assert n.dns == ["10.0.0.2", "1.1.1.1"] and n.search == ["example.com"] and len(n.routes) == 2


def test_values_are_validated_before_any_command_exists():
    assert sy.swap_size_mb("2G") == 2048 and sy.swap_size_mb("512m") == 512 and sy.swap_size_mb("1024") == 1024
    for bad in ("", "10", "200G", "abc", "2 TB"):
        try:
            sy.swap_size_mb(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert sy.minutes_from("5") == 5 and sy.minutes_from("90 min") == 90
    for bad in ("0", "x", "20000", "-1"):
        try:
            sy.minutes_from(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert sy.valid_zone("Europe/Bucharest") and sy.valid_zone("UTC") and sy.valid_zone("America/Port-au-Prince")
    for bad in ("", "../etc/passwd", "Europe/Bu cha", "a;rm -rf /", "/etc"):
        assert not sy.valid_zone(bad), bad
    assert sy.parse_target("example.com") == ("example.com", None) and sy.parse_target("example.com:443") == ("example.com", 443)
    for bad in (("a b", None), ("x;y", 80), ("good.com", 70000)):
        try:
            sy.check_command(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    for bad in ("relative", "/a b", "/x/../y", "/dir/", "/a;b"):
        try:
            sy.swap_create_command(512, bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_commands_are_safe_and_complete():
    create = sy.swap_create_command(2048)
    for part in ("already exists", "Not enough free disk space", "fallocate -l 2048M /swapfile", "chmod 600 /swapfile",
                 "mkswap /swapfile", "swapon /swapfile", "/etc/fstab"):
        assert part in create, part
    assert create.index("already exists") < create.index("fallocate")                # checks before it writes anything
    remove = sy.swap_remove_command("/swapfile")
    assert remove.index("swapoff") < remove.index("sed -i") < remove.index("rm -f")
    assert "Bucharest" in sy.timezone_command("Europe/Bucharest") and "Unknown time zone" in sy.timezone_command("UTC")
    assert sy.reboot_command(5) == "shutdown -r +5 'Reboot scheduled from BlamixShell'"
    assert "systemctl reboot" in sy.reboot_command() and "poweroff" in sy.shutdown_command()
    assert sy.ntp_command(True) == "timedatectl set-ntp true" and sy.cancel_shutdown_command() == "shutdown -c"
    assert sy.umount_command("/data") == "umount /data" and sy.mount_command("/mnt/my usb") == "mount '/mnt/my usb'"
    for protected in ("/", "/boot", "/usr", "/var/", "/etc"):
        try:
            sy.umount_command(protected)
            raise AssertionError(protected)
        except ValueError:
            pass
    check = sy.check_command("example.com", 443)
    assert "getent hosts example.com" in check and "ping -c 3" in check and "/dev/tcp/example.com/443" in check


def test_scripts_and_commands_are_valid_shell():
    def ok(text):
        return subprocess.run(["bash", "-n"], input=text.encode(), capture_output=True).returncode == 0
    try:
        subprocess.run(["bash", "-c", "true"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return                                                                       # no bash here: nothing to check
    for text in (sy.SYSTEM_SCRIPT, sy.MOUNTS_SCRIPT, sy.NETWORK_SCRIPT, sy.swap_create_command(256),
                 sy.swap_remove_command("/swapfile"), sy.timezone_command("UTC"), sy.reboot_command(),
                 sy.verify_fstab_command(), sy.check_command("example.com", 22)):
        assert ok(text), text[:80]


def test_collected_tabs_and_their_actions():
    c = collect.Context(FakeRunner({sy.SYSTEM_SCRIPT: SYSTEM, sy.MOUNTS_SCRIPT: MOUNTS, sy.NETWORK_SCRIPT: NETWORK}))
    t = collect.load_system(c)
    rows = {r[0]: (r[1], s) for r, s in zip(t.rows, t.styles) if r[0] != "Swap"}
    assert rows["Reboot required"] == ("yes", "warn") and rows["Clock sync (NTP)"][1] == "warn"
    assert "Europe/Bucharest" in rows["Time zone"][0] and rows["Host"][0] == "web-1"
    swaps = [(r, k) for r, k in zip(t.rows, t.keys) if r[0] == "Swap"]
    assert swaps[0][1] == ("swap", "/swapfile", "file") and "72% used" in swaps[0][0][2]
    m = collect.load_mounts(c)
    flags = {r[1]: (r[4], s) for r, s in zip(m.rows, m.styles)}
    assert flags["/"] == ("yes", "") and flags["/backup"] == ("NO", "warn") and flags["/mnt/usb"] == ("NO", "dim")
    n = collect.load_network(c)
    kinds = [r[0] for r in n.rows]
    assert kinds.count("Interface") == 3 and "Gateway" in kinds and kinds.count("DNS") == 3
    eth0 = next(r for r in n.rows if r[1] == "eth0")
    assert "10.0.0.5/24" in eth0[3] and "52:54:00:12:34:56" in eth0[3]

    sysacts = collect.actions_for("system", None)
    assert [a.label for a in sysacts][:2] == ["Reboot now", "Reboot in a while…"] and not any("Remove" in a.label for a in sysacts)
    assert collect.actions_for("system", ("swap", "/swapfile", "file"))[-1].label == "Remove the swap file /swapfile"
    assert not any("Remove" in a.label for a in collect.actions_for("system", ("swap", "/dev/sda2", "partition")))
    tz = next(a for a in sysacts if a.label.startswith("Set the time zone"))
    assert "Asia/Tokyo" in tz.command_for("Asia/Tokyo")
    try:
        tz.command_for("nope; reboot")
        raise AssertionError("must reject")
    except ValueError:
        pass
    mount_acts = collect.actions_for("mounts", ("mount", "/backup", False, "ext4"))
    assert [a.label for a in mount_acts] == ["Mount /backup", "Check /etc/fstab (what mount -a would do)"] and mount_acts[1].readonly
    labels = [a.label for a in collect.actions_for("mounts", ("mount", "/data disk", True, "ext4"))]
    assert labels[0] == "Unmount /data disk"
    assert not any(a.label.startswith("Unmount") for a in collect.actions_for("mounts", ("mount", "/", True, "xfs")))
    net = collect.actions_for("network", None)
    assert all(a.readonly for a in net) and "443" in net[0].command_for("example.com:443")


def test_report_has_a_system_section():
    data = {"system": sy.parse_system(SYSTEM)}
    md = report.build("Web 1", "x", datetime(2026, 10, 6), data)
    assert "## System" in md and "Europe/Bucharest" in md and "not synchronized" in md and "yes" in md
    assert "2.0 GB file /swapfile" in md
