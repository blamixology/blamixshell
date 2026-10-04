"""Firewall tab: parsing real command output and building the rule commands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import firewall as fw  # noqa: E402

FIREWALLD = """@@manager
firewalld
@@state
running
@@default
public
@@zones
## public
public (active)
  target: default
  icmp-block-inversion: no
  interfaces: eth0
  sources:
  services: ssh dhcpv6-client http
  ports: 8080/tcp 5000-5100/udp
  protocols:
  masquerade: no
  forward-ports:
  source-ports:
  icmp-blocks:
  rich rules:
\trule family="ipv4" source address="10.0.0.5" port port="3306" protocol="tcp" accept
"""

UFW = """@@manager
ufw
@@state
Status: active
Logging: on (low)
@@rules
Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 8080/tcp                   DENY IN     10.0.0.0/8
[ 3] 443                        LIMIT IN    Anywhere (v6)
"""

IPTABLES = """@@manager
iptables
@@rules
-P INPUT DROP
-P FORWARD DROP
-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT
"""


def test_firewalld_rules_are_listed_per_zone():
    f = fw.parse(FIREWALLD)
    assert (f.manager, f.state, f.default_zone, f.editable) == ("firewalld", "running", "public", True)
    got = [(r.scope, r.kind, r.value) for r in f.rules]
    assert ("public", "service", "http") in got and ("public", "port", "8080/tcp") in got
    assert ("public", "port", "5000-5100/udp") in got and ("public", "interface", "eth0") in got
    assert [r.value for r in f.rules if r.kind == "rich rule"][0].startswith('rule family="ipv4"')


def test_ufw_rules_have_numbers():
    f = fw.parse(UFW)
    assert (f.manager, f.state, f.editable) == ("ufw", "active", True)
    assert [(r.number, r.kind, r.value) for r in f.rules] == [(1, "allow", "22/tcp"), (2, "deny", "8080/tcp"),
                                                              (3, "limit", "443")]
    assert f.rules[1].detail == "from 10.0.0.0/8 (IN)"


def test_iptables_is_read_only_and_root_errors_are_detected():
    f = fw.parse(IPTABLES)
    assert f.manager == "iptables" and not f.editable
    assert [(r.scope, r.kind) for r in f.rules] == [("INPUT", "policy"), ("FORWARD", "policy"), ("INPUT", "rule")]
    denied = "@@manager\nufw\n@@state\nERROR: You need to be root to run this script\n@@rules\n"
    assert fw.parse(denied).needs_root and not fw.parse(denied).editable
    assert fw.needs_root("iptables v1.4.21: can't initialize iptables table `filter': Permission denied")


def test_ports_and_commands():
    assert fw.normalize_port("8080", "ufw") == "8080"
    assert fw.normalize_port("8000-8100", "ufw") == "8000:8100"
    assert fw.normalize_port("8000:8100", "firewalld") == "8000-8100"
    for bad in ("", "0", "70000", "80;rm", "9-3", "a"):
        assert fw.normalize_port(bad, "ufw") == "", bad
    assert fw.add_port_command("ufw", "8080", "tcp") == "ufw allow 8080/tcp"
    cmd = fw.add_port_command("firewalld", "8080", "tcp", "public")
    assert cmd.startswith("sh -c ") and "--zone=public --permanent --add-port=8080/tcp" in cmd and "--reload" in cmd
    f = fw.parse(FIREWALLD)
    http = next(r for r in f.rules if r.value == "http")
    assert "--remove-service=http" in fw.remove_command("firewalld", http)
    assert fw.remove_command("firewalld", next(r for r in f.rules if r.kind == "interface")) is None
    assert fw.remove_command("ufw", fw.parse(UFW).rules[1]) == "ufw --force delete 2"
    assert fw.remove_command("iptables", fw.parse(IPTABLES).rules[2]) is None


def test_ssh_rules_are_protected():
    f = fw.parse(FIREWALLD)
    assert fw.protects_ssh(next(r for r in f.rules if r.value == "ssh"), 22)
    assert not fw.protects_ssh(next(r for r in f.rules if r.value == "http"), 22)
    u = fw.parse(UFW)
    assert fw.protects_ssh(u.rules[0], 22) and not fw.protects_ssh(u.rules[1], 22)
    assert fw.protects_ssh(fw.Rule("public", "port", "64000-65000/tcp"), 64990)      # a custom ssh port in a range
