"""Reading and saving one file on a server for the built-in editor: over SFTP as your login, or with sudo for files
only root may read or change (/etc/nginx/…). Saves never leave a half-written file: SFTP writes a temporary copy and
renames it over the original (keeping its permissions); sudo copies into the existing file, so its owner, group and
mode stay as they were. No Qt here; call these off the UI thread."""
from __future__ import annotations

import base64
import posixpath
import secrets
import shlex
import stat as st
from dataclasses import dataclass

from . import dashboard as d


@dataclass
class FileInfo:
    size: int
    mtime: float
    mode: int = 0o644


class NeedsSudo(Exception):
    """The login can't read or write it; with sudo it could (the editor asks for the password)."""


def _denied(e: Exception) -> bool:
    return isinstance(e, PermissionError) or getattr(e, "errno", None) == 13 or "ermission denied" in str(e)


class RemoteFile:
    def __init__(self, session, path: str):
        self.session = session                  # a ShellSession: .sftp() and .client
        self.path = path
        self.sudo = False                       # opened with sudo: saved with sudo too
        self.sudo_pw: str | None = None

    # ---------------------------------------------------------------- helpers
    def _runner(self) -> d.Runner:
        return d.Runner(self.session.client)

    def _root(self) -> bool:
        return getattr(self.session.server, "username", "") == "root"

    def _privileged(self, command: str, timeout: float = 60) -> d.Result:
        r = self._runner()
        if not self._root() and self.sudo_pw is None and d.needs_password(r, False):
            raise NeedsSudo("sudo needs your password")
        res = d.run_privileged(r, command, self._root(), self.sudo_pw, timeout)
        if not res.ok and ("incorrect password" in res.err.lower() or "sorry, try again" in res.err.lower()):
            self.sudo_pw = None
            raise NeedsSudo("Wrong sudo password.")
        return res

    # ---------------------------------------------------------------- read
    def info(self) -> FileInfo | None:
        if self.sudo:
            res = self._privileged(f"stat -c '%s %Y %a' {shlex.quote(self.path)}")
            p = res.out.split()
            if not res.ok or len(p) < 3:
                return None
            return FileInfo(int(p[0]), float(p[1]), int(p[2], 8))
        try:
            a = self.session.sftp().stat(self.path)
        except FileNotFoundError:
            return None
        except OSError as e:
            if getattr(e, "errno", None) == 2:
                return None
            raise
        return FileInfo(a.st_size or 0, float(a.st_mtime or 0), st.S_IMODE(a.st_mode or 0o644))

    def read(self, limit: int) -> tuple[FileInfo, bytes]:
        """(info, content). NeedsSudo when only root may read it (and this login could use sudo)."""
        if not self.sudo:
            try:
                info = self.info()
                if info is None:
                    raise FileNotFoundError(2, "File not found", self.path)
                if info.size > limit:
                    raise ValueError(f"It's {d.human_kb(info.size / 1024)}: too big to open here. Download it instead.")
                with self.session.sftp().open(self.path, "rb") as f:
                    return info, f.read()
            except OSError as e:
                if not _denied(e):
                    raise
                raise NeedsSudo(f"{self.path} can only be read by root") from e
        info = self.info()
        if info is None:
            raise FileNotFoundError(2, "File not found", self.path)
        if info.size > limit:
            raise ValueError(f"It's {d.human_kb(info.size / 1024)}: too big to open here.")
        res = self._privileged(f"base64 {shlex.quote(self.path)}", timeout=120)
        if not res.ok:
            raise OSError(res.err.strip() or f"exit {res.code}")
        return info, base64.b64decode("".join(res.out.split()))

    # ---------------------------------------------------------------- write
    def write(self, data: bytes, mode: int | None = None) -> FileInfo | None:
        """Save. NeedsSudo when the login may not change it."""
        if self.sudo:
            return self._write_sudo(data)
        sftp = self.session.sftp()
        folder, name = posixpath.split(self.path)
        tmp = posixpath.join(folder, f".{name}.blamixshell-{secrets.token_hex(4)}")
        try:
            with sftp.open(tmp, "wb") as f:     # a full copy first, then one rename: never half a file
                f.write(data)
            if mode is not None:
                sftp.chmod(tmp, mode)
            try:
                sftp.posix_rename(tmp, self.path)
            except OSError:
                sftp.rename(tmp, self.path)     # servers without the posix-rename extension
        except OSError as e:
            try:
                sftp.remove(tmp)
            except OSError:
                pass
            if not _denied(e):
                raise
            try:                                # the folder isn't writable, the file may be: write it in place
                with sftp.open(self.path, "r+b") as f:
                    f.truncate(0)
                    f.write(data)
            except OSError as e2:
                if _denied(e2):
                    raise NeedsSudo(f"{self.path} can only be changed by root") from e2
                raise
        return self.info()

    def _write_sudo(self, data: bytes) -> FileInfo | None:
        """Upload to a private temporary file, then let root copy it INTO the file (owner and mode stay)."""
        sftp = self.session.sftp()
        tmp = f"/tmp/.blamixshell-edit-{secrets.token_hex(6)}"
        with sftp.open(tmp, "wb") as f:
            f.write(data)
        sftp.chmod(tmp, 0o600)
        q, qt = shlex.quote(self.path), shlex.quote(tmp)
        try:
            res = self._privileged(f"sh -c {shlex.quote(f'cat {qt} > {q}')}", timeout=120)
            if not res.ok:
                raise OSError(res.err.strip() or f"exit {res.code}")
        finally:
            try:
                sftp.remove(tmp)
            except OSError:
                pass
        return self.info()
