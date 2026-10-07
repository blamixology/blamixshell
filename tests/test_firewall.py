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


def test_iptables_can_be_edited_and_root_errors_are_detected():
    f = fw.parse(IPTABLES)
    assert f.manager == "iptables" and f.editable and not f.can_start
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
    ipt = fw.parse(IPTABLES).rules
    assert fw.remove_command("iptables", ipt[0]) is None                          # a default policy is not a rule
    assert fw.remove_command("iptables", ipt[2]) == "iptables -D INPUT -p tcp -m tcp --dport 22 -j ACCEPT"


def test_ssh_rules_are_protected():
    f = fw.parse(FIREWALLD)
    assert fw.protects_ssh(next(r for r in f.rules if r.value == "ssh"), 22)
    assert not fw.protects_ssh(next(r for r in f.rules if r.value == "http"), 22)
    u = fw.parse(UFW)
    assert fw.protects_ssh(u.rules[0], 22) and not fw.protects_ssh(u.rules[1], 22)
    assert fw.protects_ssh(fw.Rule("public", "port", "64000-65000/tcp"), 64990)      # a custom ssh port in a range


NFT = """@@manager
nftables
@@rules
table inet filter { # handle 1
\tchain input { # handle 1
\t\ttype filter hook input priority filter; policy drop;
\t\tct state established,related accept # handle 4
\t\ttcp dport 22 accept # handle 6
\t\ttcp dport { 80, 443 } accept # handle 7
\t}
\tchain forward { # handle 2
\t\ttype filter hook forward priority filter; policy drop;
\t}
}
"""


def test_nftables_rules_have_handles_and_can_be_changed():
    f = fw.parse(NFT)
    assert f.manager == "nftables" and f.editable
    rules = [r for r in f.rules if r.kind == "rule"]
    assert [(r.scope, r.number) for r in rules] == [("inet filter input", 4), ("inet filter input", 6), ("inet filter input", 7)]
    assert rules[2].value == "tcp dport { 80, 443 } accept"
    assert fw.nft_input_chain(f.rules) == "inet filter input"
    assert fw.add_port_command("nftables", "8080", "tcp", target="inet filter input") == \
        "nft insert rule inet filter input tcp dport 8080 accept"
    assert fw.add_port_command("nftables", "8000:8100", "udp", target="inet filter input").endswith("dport 8000-8100 accept")
    assert fw.remove_command("nftables", rules[2]) == "nft delete rule inet filter input handle 7"
    assert fw.remove_command("nftables", [r for r in f.rules if r.kind == "policy"][0]) is None
    for bad in ("", "inet filter", "inet fil;ter input"):
        try:
            fw.add_port_command("nftables", "80", "tcp", target=bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert fw.protects_ssh(rules[1], 22) and not fw.protects_ssh(rules[2], 22) and fw.protects_ssh(rules[2], 443)


def test_iptables_rules_are_removed_safely_and_ssh_rules_are_protected():
    f = fw.parse("@@manager\niptables\n@@rules\n-P INPUT DROP\n-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n"
                 "-A INPUT -p tcp -m comment --comment \"web; $(id)\" -m tcp --dport 80 -j ACCEPT\n")
    assert fw.protects_ssh(f.rules[1], 22) and not fw.protects_ssh(f.rules[2], 22)
    cmd = fw.remove_command("iptables", f.rules[2])
    assert cmd == "iptables -D INPUT -p tcp -m comment --comment 'web; $(id)' -m tcp --dport 80 -j ACCEPT"
    assert fw.add_port_command("iptables", "8000-8100", "udp") == "iptables -I INPUT -p udp --dport 8000:8100 -j ACCEPT"


def test_start_save_and_the_reason_the_buttons_are_off():
    assert fw.start_command("ufw", 64990) == "sh -c 'ufw allow 64990/tcp && ufw --force enable'"
    assert fw.start_command("firewalld") == "systemctl enable --now firewalld" and fw.start_command("iptables") is None
    assert "/etc/sysconfig/iptables" in fw.save_command("iptables") and "iptables-persistent" in fw.save_command("iptables")
    assert "nft list ruleset" in fw.save_command("nftables") and "flush ruleset" in fw.save_command("nftables")
    assert fw.save_command("ufw") is None and fw.action_label("nftables") == "Save rules" and fw.action_label("ufw") == "Reload rules"
    off = fw.parse("@@manager\nfirewalld\n@@state\nnot running\n@@default\npublic\n@@zones\n")
    assert not off.editable and off.can_start and "isn't running" in off.why_not_editable
    assert "No firewall tool" in fw.parse("@@manager\nnone\n").why_not_editable
    root = fw.parse("@@manager\nufw\n@@state\nERROR: You need to be root\n@@rules\n")
    assert not root.editable and not root.can_start and "needs root" in root.why_not_editable
    assert fw.parse_port_proto("8080/udp") == ("8080", "udp") and fw.parse_port_proto("443") == ("443", "tcp")
    for bad in ("", "x", "80/icmp", "70000"):
        try:
            fw.parse_port_proto(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
