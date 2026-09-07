"""Crash- and multi-process-safe local file writes. Standard library only.

CLI commands and a long-running ``watch`` share the same $XDG_* directories, so every
durable write goes through here: a unique temporary in the destination directory, then
an atomic rename. A crash therefore leaves either the old file or the new one, never a
truncated file, and two writers can never stomp on each other's temporary.

Files that are updated by read-modify-write (token store, SP history, name cache)
additionally hold ``file_lock`` around the whole read+write. The lock is advisory
(``flock``), released by the kernel if the holding process dies, and re-entrant within
one process so a helper that already holds it can call another that takes it too.

Never hold two different locks at the same time: nothing here needs to, and nesting
different paths is how deadlocks get written.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import threading


class _LockState:
    """Everything shared about one lock path: the in-process guard and the flock fd."""

    __slots__ = ("guard", "depth", "fd")

    def __init__(self):
        self.guard = threading.RLock()  # serialises threads of this process
        self.depth = 0                  # nested acquisitions while guard is held
        self.fd: int | None = None


_registry_guard = threading.Lock()
_lock_states: dict[str, _LockState] = {}


class _FileLock:
    """Advisory exclusive lock: ``flock`` across processes, re-entrant within one."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self) -> "_FileLock":
        state = self._state()
        state.guard.acquire()
        try:
            if state.depth == 0:
                # Lock files are never deleted (a removed inode would let a second
                # process lock a different file and both would proceed).
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                state.fd = fd
            state.depth += 1
        except BaseException:
            state.guard.release()
            raise
        return self

    def __exit__(self, *exc) -> bool:
        state = self._state()
        state.depth -= 1
        if state.depth == 0 and state.fd is not None:
            fcntl.flock(state.fd, fcntl.LOCK_UN)
            os.close(state.fd)
            state.fd = None
        state.guard.release()
        return False

    def _state(self) -> _LockState:
        with _registry_guard:
            state = _lock_states.get(self.path)
            if state is None:
                state = _lock_states[self.path] = _LockState()
            return state


def file_lock(path: str) -> _FileLock:
    """The advisory lock guarding the data file next to ``path`` (created on first use)."""
    return _FileLock(path)


def _create_temp(path: str, private: bool) -> tuple[int, str]:
    """Open a unique temporary beside ``path``; 0600 up front when the payload is secret."""
    directory = os.path.dirname(path) or "."
    stem = os.path.basename(path)
    for _ in range(100):
        tmp = os.path.join(directory, f".{stem}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
        try:
            # O_EXCL makes the name collision-proof; umask still applies to shared files.
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666), tmp
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create a temporary file next to {path}")


def atomic_write(path: str, data: str | bytes, private: bool = False) -> None:
    """Replace ``path`` with ``data`` atomically, leaving no temporary behind on failure."""
    fd, tmp = _create_temp(path, private)
    try:
        with os.fdopen(fd, "w" if isinstance(data, str) else "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())  # payload durable before the rename publishes it
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj, private: bool = False) -> None:
    """``atomic_write`` for a JSON document."""
    atomic_write(path, json.dumps(obj, indent=2), private=private)
