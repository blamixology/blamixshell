"""AWS SSM / SSO support, against a fake AWS CLI (tests/fake_aws.py) and the in-process SSH server."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sshserver import USER, TestSSHServer, host_key  # noqa: E402

from blamixshell import aws  # noqa: E402
from blamixshell.models import Server  # noqa: E402
from blamixshell import ssh_core  # noqa: E402
from blamixshell.ssh_core import exec_command, open_client, trust_host_key  # noqa: E402

FAKE = str(Path(__file__).parent / "fake_aws.py")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "aws"
    d.mkdir()
    monkeypatch.setenv("BLAMIXSHELL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_AWS_DIR", str(d))
    monkeypatch.setenv("BLAMIXSHELL_AWS", json.dumps([sys.executable, FAKE]))
    cfg = tmp_path / "config"
    cfg.write_text("[default]\nregion = eu-central-1\n\n[profile dev]\nsso_session = corp\n"
                   "sso_account_id = 123456789012\nsso_role_name = Dev\nregion = eu-west-1\n\n"
                   "[sso-session corp]\nsso_start_url = https://example.awsapps.com/start\n"
                   "sso_region = eu-central-1\n\n[profile keys]\nregion = us-east-1\n")
    (tmp_path / "credentials").write_text("[keys]\naws_access_key_id = AKIAEXAMPLE\n[ci]\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    return d


def calls(d) -> list[list[str]]:
    p = d / "calls.log"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_profiles_and_regions(fake):
    assert aws.profiles() == ["default", "ci", "dev", "keys"]
    assert aws.profile_region("dev") == "eu-west-1" and aws.profile_region("") == "eu-central-1"
    assert aws.is_sso_profile("dev") and not aws.is_sso_profile("keys")


@pytest.mark.parametrize("text,kind", [
    ("Error when retrieving token from sso: Token has expired and refresh failed", aws.LoginRequired),
    ("The SSO session associated with this profile has expired or is otherwise invalid. To refresh this "
     "SSO session run aws sso login with the corresponding profile.", aws.LoginRequired),
    ("Unable to locate credentials. You can configure credentials by running \"aws configure\".", aws.LoginRequired),
    ("\nSessionManagerPlugin is not found. Please refer to SessionManager Documentation here: "
     "http://docs.aws.amazon.com/console/systems-manager/session-manager-plugin-not-found\n", aws.PluginMissing),
    ("An error occurred (TargetNotConnected) when calling the StartSession operation: i-1 is not connected.",
     aws.AwsError),
])
def test_classify(fake, text, kind):
    e = aws.classify(text, "dev")
    assert type(e) is kind
    if kind is aws.LoginRequired:
        assert e.sso and "dev" in str(e)
    if "TargetNotConnected" in text:
        assert "isn't connected to Systems Manager" in str(e)


def test_login_required_for_a_key_profile_says_so(fake):
    e = aws.classify("Unable to locate credentials", "keys")
    assert isinstance(e, aws.LoginRequired) and not e.sso and "aws configure" in str(e)


def test_sso_login_flow(fake):
    (fake / "expired").touch()
    with pytest.raises(aws.LoginRequired):
        aws.check_credentials("dev")
    seen = []
    login = aws.SsoLogin("dev", seen.append)
    login.start()
    assert login.wait(30) == 0
    info = login.info()
    assert info.url == "https://device.sso.eu-central-1.amazonaws.com/" and info.code == "ABCD-EFGH"
    assert seen and "Successfully" in seen[-1]
    assert aws.check_credentials("dev").endswith("assumed-role/Dev/mike")
    assert ["sso", "login", "--profile", "dev"] in calls(fake)


def test_list_instances(fake):
    inst, note = aws.list_instances("dev", "eu-west-1")
    by = {i.id: i for i in inst}
    assert by["i-0aaa1111bbbb2222c"].name == "api-prod-1" and by["i-0aaa1111bbbb2222c"].default_user == "ubuntu"
    assert by["i-0ddd3333eeee4444f"].platform == "Windows" and not by["i-0ddd3333eeee4444f"].online
    assert by["mi-0123456789abcdef0"].label == "onprem-box"       # hybrid node: no EC2 name
    assert [i.online for i in inst] == [True, True, False]           # online first
    assert not note
    describe = [c for c in calls(fake) if c[:2] == ["ec2", "describe-instances"]][0]
    assert "mi-0123456789abcdef0" not in describe and "--profile" in describe and "eu-west-1" in describe
    (fake / "no_ec2").touch()
    inst, note = aws.list_instances("dev")
    assert "EC2 names unavailable" in note and {i.label for i in inst} >= {"ip-10-0-1-5", "EC2AMAZ-1"}


def test_server_commands():
    s = Server(host="i-0aaa1111bbbb2222c", username="ubuntu", connection="ssm-ssh", aws_profile="dev",
               aws_region="eu-west-1")
    cmd = s.ssh_command()
    assert "ProxyCommand=aws ssm start-session --target %h --document-name AWS-StartSSHSession" in cmd
    assert cmd.endswith("ubuntu@i-0aaa1111bbbb2222c") and "--profile dev --region eu-west-1" in cmd
    s.connection = "ssm-shell"
    assert s.ssh_command() == "aws ssm start-session --target i-0aaa1111bbbb2222c --profile dev --region eu-west-1"
    assert s.address == "i-0aaa1111bbbb2222c (SSM)" and not s.uses_ssh and not s.needs_password
    assert Server.from_dict({"host": "x", "connection": "bogus"}).connection == "ssh"
    assert aws.is_instance_id("i-0aaa1111bbbb2222c") and aws.is_instance_id("mi-0123456789abcdef0")
    assert not aws.is_instance_id("web-1")


def _ssm_server(srv, fake, **kw) -> Server:
    (fake / "ssh_port").write_text(str(srv.port))
    s = Server(name="api", host="i-0aaa1111bbbb2222c", port=22, username=USER, keepalive=0,
               connection="ssm-ssh", aws_profile="dev", aws_region="eu-west-1", **kw)
    trust_host_key(s.host, host_key())          # known_hosts entry is the instance id
    return s


def test_ssh_over_ssm_with_password(fake):
    with TestSSHServer("password") as srv:
        s = _ssm_server(srv, fake, auth="password", password="pw")
        client, chain = open_client(s, lambda _i: None)
        try:
            i, o, e = exec_command(client, "uptime", timeout=10)
            assert o.read().decode().strip() == "uptime"
        finally:
            client.close()
        proxy_call = [c for c in calls(fake) if c[:2] == ["ssm", "start-session"]][0]
        assert proxy_call[proxy_call.index("--document-name") + 1] == "AWS-StartSSHSession"
        assert "portNumber=22" in proxy_call and proxy_call[proxy_call.index("--target") + 1] == s.host


def test_ssh_over_ssm_with_instance_connect(fake):
    with TestSSHServer("pubkey", authorized_file=str(fake / "eic_keys")) as srv:
        s = _ssm_server(srv, fake, eic=True, auth="password")      # password ignored with EIC
        client, _ = open_client(s, lambda _i: None)
        client.close()
        eic = [c for c in calls(fake) if c[0] == "ec2-instance-connect"][0]
        assert eic[eic.index("--instance-os-user") + 1] == USER and eic[eic.index("--instance-id") + 1] == s.host
        assert (fake / "eic_keys").read_text().startswith(f"{USER} ssh-ed25519 ")


def test_ssh_over_ssm_expired_sso(fake):
    (fake / "expired").touch()
    with TestSSHServer("password") as srv:
        s = _ssm_server(srv, fake, auth="password", password="pw")
        with pytest.raises(aws.LoginRequired) as ei:
            open_client(s, lambda _i: None)
        assert ei.value.profile == "dev" and ei.value.sso
        assert "expired" in ssh_core.test_connection(s, lambda _i: None)


def test_ssm_shell_test_connection(fake):
    s = Server(host="i-0aaa1111bbbb2222c", connection="ssm-shell", aws_profile="dev")
    assert ssh_core.test_connection(s, lambda _i: None).startswith("OK. Signed in to AWS as assumed-role/Dev/mike")


def test_missing_cli(monkeypatch, fake):
    monkeypatch.delenv("BLAMIXSHELL_AWS")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(aws, "_first_existing", lambda paths: "")
    with pytest.raises(aws.CliMissing):
        aws.run(["sts", "get-caller-identity"])


def test_ssm_shell_in_a_pty(fake):
    from blamixshell.pty_process import PtyProcess
    s = Server(host="i-0aaa1111bbbb2222c", connection="ssm-shell", aws_profile="dev")
    p = PtyProcess(aws.shell_argv(s), aws.env(), 100, 30)
    out = b""
    deadline = time.time() + 20
    while b"sh-5.2$" not in out and time.time() < deadline:
        out += p.read()
    p.write(b"whoami\r")
    while b"ran: whoami" not in out and time.time() < deadline:
        out += p.read()
    p.write(b"exit\r")
    while time.time() < deadline:
        chunk = p.read()
        if not chunk:
            break
        out += chunk
    assert b"Starting session with SessionId" in out and b"ran: whoami" in out
    assert p.exit_code == 0
    p.close()


# ------------------------------------------------------------------ CLI
@pytest.fixture
def store(fake, tmp_path):
    from blamixshell.models import Store
    from blamixshell.vault import Vault
    return Store(Vault.create(tmp_path / "vault.sdv", "masterpass", n_log2=12), {})


def test_cli_aws_instances_and_import(fake, store, capsys):
    from blamixshell import cli
    p = cli.build_parser()
    assert cli.cmd_aws(None, p.parse_args(["aws", "instances", "--profile", "dev", "--online"])) == 0
    out = capsys.readouterr().out
    assert "api-prod-1" in out and "onprem-box" in out and "EC2AMAZ-1" not in out
    assert cli.cmd_aws(store, p.parse_args(["aws", "import", "--profile", "dev", "--region", "eu-west-1"])) == 0
    by = {s.name: s for s in store.servers.values()}
    assert by["api-prod-1"].connection == "ssm-ssh" and by["api-prod-1"].eic and by["api-prod-1"].username == "ubuntu"
    assert by["EC2AMAZ-1"].connection == "ssm-shell"                 # Windows: Session Manager shell
    assert not by["onprem-box"].eic                                   # no Instance Connect for mi-
    assert {s.group for s in store.servers.values()} == {"AWS/dev/eu-west-1"}
    cli.cmd_aws(store, p.parse_args(["aws", "import", "--profile", "dev", "--region", "eu-west-1"]))
    assert len(store.servers) == 3 and "Added 0, updated 3" in capsys.readouterr().out


def test_cli_add_ssm_and_skips(fake, store, capsys):
    from blamixshell import cli
    p = cli.build_parser()
    cli.cmd_add(store, p.parse_args(["add", "--name", "box", "--host", "i-0aaa1111bbbb2222c", "--ssm", "shell",
                                     "--aws-profile", "dev", "--group", "AWS"]))
    s = cli.find_server(store, "box")
    assert s.connection == "ssm-shell" and s.aws_profile == "dev" and s.address == "i-0aaa1111bbbb2222c (SSM)"
    with pytest.raises(SystemExit):
        cli.cmd_status(store, p.parse_args(["status", "box"]))
    assert "status needs SSH" in capsys.readouterr().err


def test_cli_ssm_shell_runs_the_aws_cli(fake, store, monkeypatch):
    import subprocess
    from blamixshell import cli
    ran = []
    monkeypatch.setattr(subprocess, "call", lambda argv, env=None: ran.append(argv) or 0)
    s = Server(name="box", host="i-0aaa1111bbbb2222c", connection="ssm-shell", aws_profile="dev")
    assert cli.interactive_shell(s, store) == 0
    assert ran[0][-6:] == ["ssm", "start-session", "--target", "i-0aaa1111bbbb2222c", "--profile", "dev"]


# ------------------------------------------------------------------ desktop dialogs (offscreen)
@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_server_dialog_ssm_fields(fake, store, qapp):
    from blamixshell.dialogs import ServerDialog
    s = Server(name="api", host="i-0aaa1111bbbb2222c", connection="ssm-ssh", aws_profile="dev",
               username="ubuntu", eic=True)
    d = ServerDialog(store, s)
    f = d._form
    assert f.isRowVisible(d.aws_row) and not f.isRowVisible(d.auth_stack)      # EIC: no auth needed
    assert f.labelForField(d._hp).text() == "Instance ID" and not d.jump.isEnabled()
    d.via.setCurrentIndex(d.via.findData("ssm-shell"))
    assert not f.isRowVisible(d.user) and not d._tabs.isTabEnabled(d._tabs.indexOf(d.tunnel_editor))
    d.via.setCurrentIndex(d.via.findData("ssh"))
    assert not f.isRowVisible(d.aws_row) and f.labelForField(d._hp).text() == "Host"
    d.via.setCurrentIndex(d.via.findData("ssm-ssh"))
    out = d._collect()
    assert (out.connection, out.aws_profile, out.eic) == ("ssm-ssh", "dev", True)


def test_import_dialog(fake, store, qapp):
    from blamixshell.aws_ui import AwsImportDialog
    d = AwsImportDialog(store)
    d.profile.setCurrentText("dev")
    assert d.region.currentText() == "eu-west-1" and d.login_btn.isVisibleTo(d)
    d._list()
    end = time.time() + 20
    while d.table.rowCount() == 0 and time.time() < end:
        qapp.processEvents()
        time.sleep(0.02)
    assert d.table.rowCount() == 3
    d._import()
    assert d.added == 3 and "Added 3" in d.note.text() and len(store.servers) == 3


def test_pty_keeps_output_of_a_program_that_exits_at_once():
    """The AWS CLI prints an error and exits immediately (expired SSO): that text must arrive."""
    if sys.platform == "win32":
        argv = ["cmd.exe", "/c", "echo fast-error"]
    else:
        argv = ["/bin/sh", "-c", "echo fast-error >&2; exit 3"]
    from blamixshell.pty_process import PtyProcess
    p = PtyProcess(argv, None, 80, 24)
    out, end = b"", time.time() + 15
    while time.time() < end:
        chunk = p.read()
        if not chunk:
            break
        out += chunk
    p.close()
    assert b"fast-error" in out
    if sys.platform != "win32":
        assert p.exit_code == 3
