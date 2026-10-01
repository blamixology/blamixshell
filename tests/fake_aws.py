"""A stand-in for the AWS CLI in tests (BLAMIXSHELL_AWS="python fake_aws.py").

State lives in $FAKE_AWS_DIR:
  expired        present = the SSO session has expired (removed by `sso login`)
  calls.log      one JSON argv per call
  eic_keys       "user keytype base64" lines added by ec2-instance-connect (the test SSH server reads it)
  ssh_port       where `ssm start-session --document-name AWS-StartSSHSession` connects
  no_ec2         present = ec2:DescribeInstances is denied
"""
import json
import os
import socket
import sys
import threading

D = os.environ["FAKE_AWS_DIR"]
EXPIRED = ("Error when retrieving token from sso: Token has expired and refresh failed\n")


def path(name):
    return os.path.join(D, name)


def arg(args, name, default=""):
    return args[args.index(name) + 1] if name in args else default


def fail(msg, code=255):
    sys.stderr.write(msg)
    sys.stderr.flush()
    sys.exit(code)


def proxy(port):
    s = socket.create_connection(("127.0.0.1", port))
    out = sys.stdout.buffer
    inp = sys.stdin.buffer

    def down():
        while True:
            d = s.recv(65536)
            if not d:
                break
            out.write(d)
            out.flush()
        os._exit(0)
    threading.Thread(target=down, daemon=True).start()
    while True:
        d = inp.read1(65536) if hasattr(inp, "read1") else inp.read(1)
        if not d:
            break
        s.sendall(d)
    s.close()


def shell(target):
    sys.stdout.write(f"\r\nStarting session with SessionId: tester-0123456789 on {target}\r\n")
    while True:
        sys.stdout.write("sh-5.2$ ")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line or line.strip() == "exit":
            break
        sys.stdout.write(f"ran: {line.strip()}\r\n")
    sys.stdout.write("\r\nExiting session with sessionId: tester-0123456789.\r\n")
    sys.stdout.flush()


def main():
    args = sys.argv[1:]
    with open(path("calls.log"), "a") as f:
        f.write(json.dumps(args) + "\n")
    expired = os.path.exists(path("expired"))
    cmd = [a for a in args if not a.startswith("-")][:2]
    if cmd == ["sso", "login"]:
        print("Attempting to automatically open the SSO authorization page in your default browser.")
        print("If the browser does not open or you wish to use a different device to authorize this "
              "request, open the following URL:\n\nhttps://device.sso.eu-central-1.amazonaws.com/\n\n"
              "Then enter the code:\n\nABCD-EFGH")
        if os.path.exists(path("expired")):
            os.remove(path("expired"))
        print("Successfully logged into Start URL: https://example.awsapps.com/start")
        return
    if expired:
        fail(EXPIRED)
    if cmd == ["sts", "get-caller-identity"]:
        print(json.dumps({"Arn": "arn:aws:sts::123456789012:assumed-role/Dev/mike"}))
    elif cmd == ["ssm", "start-session"]:
        if "--document-name" in args:
            proxy(int(open(path("ssh_port")).read()))
        else:
            shell(arg(args, "--target"))
    elif cmd == ["ssm", "describe-instance-information"]:
        print(json.dumps({"InstanceInformationList": [
            {"InstanceId": "i-0aaa1111bbbb2222c", "PingStatus": "Online", "PlatformType": "Linux",
             "PlatformName": "Ubuntu", "PlatformVersion": "24.04", "IPAddress": "10.0.1.5",
             "ComputerName": "ip-10-0-1-5"},
            {"InstanceId": "i-0ddd3333eeee4444f", "PingStatus": "ConnectionLost", "PlatformType": "Windows",
             "PlatformName": "Microsoft Windows Server 2022 Datacenter", "IPAddress": "10.0.2.9",
             "ComputerName": "EC2AMAZ-1"},
            {"InstanceId": "mi-0123456789abcdef0", "PingStatus": "Online", "PlatformType": "Linux",
             "PlatformName": "Amazon Linux", "PlatformVersion": "2023", "ComputerName": "onprem-box"}]}))
    elif cmd == ["ec2", "describe-instances"]:
        if os.path.exists(path("no_ec2")):
            fail("An error occurred (UnauthorizedOperation) when calling the DescribeInstances operation: "
                 "You are not authorized to perform this operation.\n", 254)
        print(json.dumps({"Reservations": [{"Instances": [
            {"InstanceId": "i-0aaa1111bbbb2222c", "State": {"Name": "running"},
             "Tags": [{"Key": "Name", "Value": "api-prod-1"}, {"Key": "env", "Value": "prod"}]},
            {"InstanceId": "i-0ddd3333eeee4444f", "State": {"Name": "stopped"}, "Tags": []}]}]}))
    elif cmd == ["ec2-instance-connect", "send-ssh-public-key"]:
        key = arg(args, "--ssh-public-key").split()
        with open(path("eic_keys"), "a") as f:
            f.write(f"{arg(args, '--instance-os-user')} {key[0]} {key[1]}\n")
        print(json.dumps({"RequestId": "r-1", "Success": True}))
    else:
        fail(f"usage: aws [options] <command> <subcommand>\naws: error: unknown {cmd}\n", 252)


main()
