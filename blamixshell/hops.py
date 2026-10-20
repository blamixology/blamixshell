"""Noticing that a terminal went on to another machine: you typed `ssh linxstress` in a server's shell, and the
prompt now shows another host. BlamixShell's own connection (files, editor, dashboard, health strip) is still to the
first server, so the Files panel says so and offers to open that machine properly, through the first one as its jump
host. Only what you typed and the prompts are used; nothing is sent anywhere. No Qt here."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# ssh options that take a value (the destination is the first word that is neither an option nor its value)
_WITH_VALUE = set("BbcDEeFIiJLlmOoPpQRSWw")
_PROMPT_HOST = re.compile(r"([A-Za-z0-9._-]+)@([A-Za-z0-9._-]+)")


@dataclass
class Hop:
    host: str                 # as typed: what the first server resolves (a name, an IP, an ~/.ssh/config alias)
    user: str = ""
    port: int = 22
    typed: str = ""           # the command, for the message
    via: str = ""             # the prompt's host where it was typed
    seen_as: str = ""         # the prompt's host after the hop (e.g. LinxStress)

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
    while words and words[0] in ("sudo", "exec", "command", "env"):
        words = words[1:]
    if not words or words[0] not in ("ssh", "autossh") and not words[0].endswith("/ssh"):
        return None
    user, port, dest, rest = "", 22, "", []
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
            # a group like -tt or -4A: the last letter may take the value that follows
            j = 0
            while j < len(flag):
                c = flag[j]
                if c in _WITH_VALUE:
                    value = flag[j + 1:] or (words[i + 1] if i + 1 < len(words) else "")
                    if not flag[j + 1:]:
                        i += 1
                    if c == "p" and value.isdigit():
                        port = int(value)
                    elif c == "l":
                        user = value
                    break
                j += 1
            i += 1
            continue
        dest = w
        i += 1
    if not dest or rest:
        return None
    if dest.startswith("ssh://"):
        dest = dest[6:].rstrip("/")
        if dest.count(":") == 1 and dest.rsplit(":", 1)[1].isdigit():
            dest, p = dest.rsplit(":", 1)
            port = int(p)
    if "@" in dest:
        user, dest = dest.rsplit("@", 1)
    if not dest or not re.fullmatch(r"[A-Za-z0-9._:-]+", dest):
        return None
    return Hop(dest, user, port, command.strip())


def prompt_host(prompt: str) -> str:
    """'safemobile@LinxStress:~$' or '[root@web-1 ~]#' -> 'LinxStress' / 'web-1'; '' when the prompt doesn't say."""
    m = _PROMPT_HOST.search(prompt or "")
    return m.group(2) if m else ""


def prompt_user(prompt: str) -> str:
    m = _PROMPT_HOST.search(prompt or "")
    return m.group(1) if m else ""


class HopWatch:
    """Fed every command line (prompt, command) of one terminal. `hop` is set while the shell is on another machine
    than the first prompt's, after an interactive ssh was typed; `update` says whether that changed."""

    def __init__(self):
        self.home = ""                # the host in the first prompt we saw
        self.hop: Hop | None = None
        self._pending: Hop | None = None

    def update(self, prompt: str, command: str) -> bool:
        before = self.hop
        host = prompt_host(prompt)
        if host and not self.home:
            self.home = host
        if host:
            if host == self.home:
                self.hop = None                       # back on the first machine
            elif self._pending is not None and (self.hop is None or self.hop.seen_as != host):
                self._pending.seen_as = host
                if not self._pending.user:
                    self._pending.user = prompt_user(prompt)
                self.hop, self._pending = self._pending, None
        new = parse_ssh(command)
        if new is not None:
            new.via = host
            self._pending = new
        return (before is None) != (self.hop is None) or (before is not None and self.hop is not None
                                                          and before.seen_as != self.hop.seen_as)
