"""A local program in a pseudo-terminal: ConPTY on Windows (pywinpty), a pty elsewhere.

Used for AWS SSM shells (`aws ssm start-session` needs a real terminal); the same layer can
carry local terminal tabs later.
"""
from __future__ import annotations

import os
import subprocess
import sys


class PtyProcess:
    """Blocking read() / write() / resize() over a program running in a terminal."""

    def __init__(self, argv: list[str], env: dict | None = None, cols: int = 120, rows: int = 32):
        self.argv = argv
        self.exit_code: int | None = None
        if sys.platform == "win32":
            self._win_spawn(argv, env, cols, rows)
        else:
            self._posix_spawn(argv, env, cols, rows)

    # ------------------------------------------------------------ POSIX
    def _posix_spawn(self, argv, env, cols, rows) -> None:
        import fcntl
        import termios
        master, slave = os.openpty()
        self._set_size(slave, cols, rows)

        def make_controlling_tty():         # runs in the child, before exec
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self._proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                          close_fds=True, preexec_fn=make_controlling_tty)
        except Exception:
            os.close(slave)
            os.close(master)
            raise
        # Keep our copy of the terminal's other side open: macOS throws away unread output
        # once the last holder closes it, so a program that exits quickly (an error message
        # from the AWS CLI) would otherwise vanish. read() notices the exit by polling.
        self._slave = slave
        self._fd = master
        self._win = None

    @staticmethod
    def _set_size(fd: int, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 2), max(cols, 2), 0, 0))

    # ------------------------------------------------------------ Windows
    def _win_spawn(self, argv, env, cols, rows) -> None:
        try:
            from winpty import PtyProcess as WinPty
        except ImportError as e:
            raise RuntimeError("Terminal support (pywinpty) is missing from this installation.") from e
        self._win = WinPty.spawn(argv, env=env, dimensions=(max(rows, 2), max(cols, 2)))
        self._proc = None
        self._fd = -1

    # ------------------------------------------------------------ io
    def read(self, size: int = 65536) -> bytes:
        """Blocks until output arrives; b"" when the program has ended."""
        if self._win is not None:
            try:
                data = self._win.read(size)
            except EOFError:
                self._reap()
                return b""
            if not data and not self._win.isalive():
                self._reap()
                return b""
            return data.encode("utf-8", "replace") if isinstance(data, str) else data
        import select
        while True:
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.2)
            except (OSError, ValueError):
                ready = []
                if self._proc.poll() is None:
                    continue
            if ready:
                try:
                    data = os.read(self._fd, size)
                except OSError:      # EIO: the terminal closed
                    data = b""
                if data:
                    return data
            if self._proc.poll() is not None:
                # the program ended: hand out whatever it printed last, then report the end
                try:
                    ready, _, _ = select.select([self._fd], [], [], 0.05)
                    if ready:
                        data = os.read(self._fd, size)
                        if data:
                            return data
                except OSError:
                    pass
                self._reap()
                return b""
            if ready:            # readable but empty and still running: avoid a busy loop
                import time
                time.sleep(0.05)

    def write(self, data: bytes) -> None:
        if self._win is not None:
            self._win.write(data.decode("utf-8", "replace"))
            return
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]

    def resize(self, cols: int, rows: int) -> None:
        try:
            if self._win is not None:
                self._win.setwinsize(max(rows, 2), max(cols, 2))
            else:
                self._set_size(self._fd, cols, rows)
        except Exception:
            pass

    def alive(self) -> bool:
        if self._win is not None:
            return self._win.isalive()
        return self._proc.poll() is None

    def _reap(self) -> None:
        if self.exit_code is not None:
            return
        try:
            if self._win is not None:
                for _ in range(20):
                    if not self._win.isalive():
                        break
                    import time
                    time.sleep(0.05)
                self.exit_code = self._win.exitstatus if self._win.exitstatus is not None else 0
            else:
                self.exit_code = self._proc.wait(5)
        except Exception:
            self.exit_code = -1

    def close(self) -> None:
        try:
            if self._win is not None:
                if self._win.isalive():
                    self._win.terminate(force=True)
            else:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(2)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                for fd in (self._fd, self._slave):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        except Exception:
            pass
