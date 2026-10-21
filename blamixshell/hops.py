"""Noticing that a terminal went on to another machine: you typed `ssh linxstress` in a server's shell, and the
prompt now shows another host. The files, editor and dashboard then follow you there through the first server's own
ssh (see relay.py); when that can't log in without a password, the Files panel offers to open the machine as a server
of its own instead. Only what you typed and the prompts are used; nothing is sent anywhere. No Qt here."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# ssh options that take a value (the destination is the first word that is neither an option nor its value)
_WITH_VALUE = set("BbcDEeFIiJLlmOoPpQRSWw")
# what a background connection must not inherit: terminal, no-command, background, forwards, multiplexing control
_DROP_FLAGS = set("tTnNfsGVMv")
_DROP_WITH_VALUE = set("LRDWwOSE")
_PROMPT_HOST = re.compile(r"([A-Za-z0-9._-]+)@([A-Za-z0-9._-]+)")


@dataclass
class Hop:
    host: str                 # as typed: what the first server resolves (a name, an IP, an ~/.ssh/config alias)
    user: str = ""
    port: int = 22
    typed: str = ""           # the command, for the message
    via: str = ""             # the prompt's host where it was typed
    seen_as: str = ""         # the prompt's host after the hop (e.g. LinxStress)
    options: list[str] = field(default_factory=list)   # the typed ssh options worth repeating (-p, -l, -i, -J, -o …)
    dest: str = ""            # the destination word as typed (user@host, an alias …)
    sudo: bool = False        # typed as `sudo ssh …`: root's keys and config on that machine

    @property
    def label(self) -> str:
        return self.seen_as or self.host


def parse_ssh(command: str) -> Hop | None:
    """`ssh [options] [user@]host` (also `ssh -p 2222 -l bob host`, `ssh ssh://bob@host:2222`, after `sudo`/`exec`)
    -> Hop. None for anything else, and for `ssh host some-command` (that runs once and comes back)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    sudo = False
    while words and words[0] in ("sudo", "exec", "command", "env"):
        sudo = sudo or words[0] == "sudo"
        words = words[1:]
    if not words or words[0] not in ("ssh", "autossh") and not words[0].endswith("/ssh"):
        return None
    user, port, dest, rest = "", 22, "", []
    keep: list[str] = []
    i = 1
    while i < len(words):
        w = words[i]
        if dest:
            rest = words[i:]
            break
        if w == "--":
            i += 1
            continue
        if w.startswith("-") and len(w) > 1:
            flag = w[1:]
            plain = ""
            j = 0
            while j < len(flag):
                c = flag[j]
                if c in _WITH_VALUE:
                    inline = flag[j + 1:]
                    value = inline or (words[i + 1] if i + 1 < len(words) else "")
                    if not inline:
                        i += 1
                    if c == "p" and value.isdigit():
                        port = int(value)
                    elif c == "l":
                        user = value
                    if c not in _DROP_WITH_VALUE:
                        keep += [f"-{c}", value]
                    break
                if c not in _DROP_FLAGS:
                    plain += c
                j += 1
            if plain:                               # e.g. -4A of -4Ai key (ssh doesn't mind the order)
                keep.append(f"-{plain}")
            i += 1
            continue
        dest = w
        i += 1
    if not dest or rest:
        return None
    host = dest
    if host.startswith("ssh://"):
        host = host[6:].rstrip("/")
        if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
            host, p = host.rsplit(":", 1)
            port = int(p)
    if "@" in host:
        user, host = host.rsplit("@", 1)
    if not host or not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
        return None
    return Hop(host, user, port, command.strip(), options=keep, dest=dest, sudo=sudo)


def prompt_host(prompt: str) -> str:
    """'safemobile@LinxStress:~$' or '[root@web-1 ~]#' -> 'LinxStress' / 'web-1'; '' when the prompt doesn't say."""
    m = _PROMPT_HOST.search(prompt or "")
    return m.group(2) if m else ""


def prompt_user(prompt: str) -> str:
    m = _PROMPT_HOST.search(prompt or "")
    return m.group(1) if m else ""


class HopWatch:
    """Fed every command line (prompt, command) of one terminal. `chain` lists the machines the shell went through
    with interactive ssh (A -> B -> C), `hop` is the last one; `update` says whether that changed."""

    def __init__(self):
        self.home = ""                # the host in the first prompt we saw
        self.chain: list[Hop] = []
        self._pending: Hop | None = None

    @property
    def hop(self) -> Hop | None:
        return self.chain[-1] if self.chain else None

    def update(self, prompt: str, command: str) -> bool:
        before = [h.seen_as for h in self.chain]
        host = prompt_host(prompt)
        if host and not self.home:
            self.home = host
        if host:
            seen = [h.seen_as for h in self.chain]
            if host == self.home:
                self.chain = []                          # back on the first machine
            elif host in seen:
                self.chain = self.chain[:seen.index(host) + 1]   # `exit` from a machine further on
            elif self._pending is not None:
                self._pending.seen_as = host
                if not self._pending.user:
                    self._pending.user = prompt_user(prompt)
                self.chain.append(self._pending)
                self._pending = None
        new = parse_ssh(command)
        if new is not None:
            new.via = host
            self._pending = new
        return [h.seen_as for h in self.chain] != before
