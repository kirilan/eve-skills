"""Crash- and multi-process-safe local file writes. Standard library only.

CLI commands and a long-running ``watch`` share the same config, cache and data
directories, so every durable write goes through here: a unique temporary in the
destination directory, then an atomic rename. A crash therefore leaves either the old
file or the new one, never a truncated file, and two writers can never stomp on each
other's temporary. The rename is atomic on Windows as well; what differs there is that
it fails outright while any other process still has the destination open, so
``_replace`` gives that window a moment to clear instead of losing the write.

Files that are updated by read-modify-write (token store, SP history, name cache)
additionally hold ``file_lock`` around the whole read+write. The lock is advisory on
every platform - it serialises processes that cooperate by taking it and does not stop
an unrelated program from opening the data file - the OS releases it when the holding
process dies, and it is re-entrant within one process so a helper that already holds it
can call another that takes it too.

Two mechanisms implement it, selected once at import by which locking module this
platform actually provides (never by matching ``sys.platform`` against a string):

* POSIX - ``fcntl.flock(fd, LOCK_EX)`` on the lock file: whole-file, blocks until the
  holder is gone, dropped by the kernel when the descriptor closes or the process dies.
* Windows - ``msvcrt.locking(fd, LK_NBLCK, 1)`` on the first byte of that same file,
  retried until granted (see ``_ByteRangeBackend``), released with ``LK_UNLCK`` and
  dropped by the OS when the handle closes or the process dies.

The one genuine difference between them is that a POSIX ``flock`` stays advisory even
for the locked file itself, while an ``msvcrt`` byte-range lock denies other processes
any access to the locked region. Nothing reads or writes the lock file: it is a
zero-length sentinel sitting next to the data file precisely so the locked region can be
empty, which keeps that difference invisible to every caller.

Mode arguments (0600 for secrets, 0666 masked by umask for everything else) are honoured
on POSIX and ignored on Windows, where a new file simply inherits the ACL of its
directory - normally the private user profile. So the token store is protected there by
that inherited ACL rather than by these bits; passing them anyway costs nothing and
keeps the intent visible in the code.

Never hold two different locks at the same time: nothing here needs to, and nesting
different paths is how deadlocks get written.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import threading
import time

try:  # POSIX. Absent on Windows, which is why this must not be a hard import.
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

try:  # Windows. Absent everywhere else.
    import msvcrt
except ImportError:  # pragma: no cover - platform-dependent
    msvcrt = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# lock backends
# ---------------------------------------------------------------------------

# Contention on a byte-range lock is polled, not signalled: sleep this long after a
# refusal and double it up to the ceiling. The start is short enough that the common
# case (a peer holding the lock for milliseconds) costs little latency; the ceiling is
# low enough that a lock held for seconds still feels immediate to a waiting CLI, while
# costing ~4 wakeups a second instead of spinning a core. Blocking until granted is the
# correct semantic because POSIX ``flock`` blocks and every caller relies on it.
_LOCK_POLL_DELAY = 0.02
_LOCK_POLL_CEILING = 0.25

# Errnos meaning "someone else holds that region", as opposed to "this request can
# never work". Only the first is worth retrying: retrying the second would turn a
# closed descriptor into an endless loop. Windows reports contention as EDEADLOCK or
# EACCES; EAGAIN covers platforms that spell the refusal that way.
_LOCK_DENIED_ERRNOS = frozenset(
    code
    for code in (getattr(errno, name, None) for name in ("EACCES", "EAGAIN", "EDEADLOCK"))
    if code is not None
)


class _FlockBackend:
    """POSIX locking: one whole-file advisory lock per descriptor.

    ``LOCK_EX`` blocks until the current holder unlocks or exits, and the kernel drops
    the lock as soon as the descriptor closes - including when the process dies, which
    is what makes a crashed ``watch`` unable to wedge every later command.
    """

    def acquire(self, fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def release(self, fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class _ByteRangeBackend:
    """Windows locking: one locked byte at offset 0 of the lock file.

    ``LK_NBLCK`` asks for the region once and raises when another process holds it, so
    acquiring is a poll loop. ``LK_LOCK`` is not usable: it gives up after ten tries,
    which would convert ordinary contention into a spurious failure. Only refusals are
    retried - see ``_LOCK_DENIED_ERRNOS``.

    ``msvcrt`` locks from the current file position, so both calls seek to 0 first
    rather than depending on where the position happens to be.
    """

    def acquire(self, fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        delay = _LOCK_POLL_DELAY
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as err:
                if err.errno not in _LOCK_DENIED_ERRNOS:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, _LOCK_POLL_CEILING)

    def release(self, fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _select_lock_backend() -> _FlockBackend | _ByteRangeBackend:
    """The locking mechanism this platform provides, judged by capability.

    Preferring ``fcntl`` where both exist (Cygwin) is deliberate: a real kernel lock
    beats a polled byte range. Having neither means we cannot honour the durability
    contract at all, so say so loudly instead of locking nothing in silence.
    """
    if fcntl is not None:
        return _FlockBackend()
    if msvcrt is not None:
        return _ByteRangeBackend()
    raise ImportError(
        "no supported file-locking mechanism: this platform offers neither fcntl "
        "(POSIX) nor msvcrt (Windows)"
    )


_lock_backend = _select_lock_backend()


class _LockState:
    """Everything shared about one lock path: the in-process guard and the OS lock fd."""

    __slots__ = ("guard", "depth", "fd")

    def __init__(self):
        self.guard = threading.RLock()  # serialises threads of this process
        self.depth = 0                  # nested acquisitions while guard is held
        self.fd: int | None = None


_registry_guard = threading.Lock()
_lock_states: dict[str, _LockState] = {}


class _FileLock:
    """Advisory exclusive lock: OS-level across processes, re-entrant within one."""

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
                    _lock_backend.acquire(fd)
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
            _lock_backend.release(state.fd)
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
    # On Windows a descriptor without O_BINARY is in text mode, so every "\n" written
    # through it becomes "\r\n": JSON survives that, events.jsonl does not, because its
    # readers count lines. The flag exists nowhere else, hence the getattr.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        tmp = os.path.join(directory, f".{stem}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
        try:
            # O_EXCL makes the name collision-proof; umask still applies to shared files.
            return os.open(tmp, flags, 0o600 if private else 0o666), tmp
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create a temporary file next to {path}")


# ``os.replace`` and ``os.remove`` are atomic on both platforms, but on Windows either one raises
# PermissionError while any other process merely holds the target open - a reader in a second
# eve-skills process is enough, and so is the scan Windows runs on a file that just changed. Retry
# briefly before admitting defeat; POSIX has no such restriction, so on POSIX these loops always take
# their first iteration and the retry is dead code there. The budget (8 attempts, 0.02s doubling to
# 0.25s) is about a second: long enough for a reader or a scan to let go, short enough that a
# genuinely wedged file does not hang the CLI.
_SHARE_ATTEMPTS = 8
_SHARE_DELAY = 0.02
_SHARE_DELAY_CEILING = 0.25


def _tolerate_sharing(operation, *args) -> None:
    """``operation(*args)``, retrying a Windows sharing violation that clears on its own."""
    delay = _SHARE_DELAY
    for attempt in range(1, _SHARE_ATTEMPTS + 1):
        try:
            operation(*args)
            return
        except PermissionError:
            if attempt == _SHARE_ATTEMPTS:
                raise  # the original error, unwrapped: the window did not clear
            time.sleep(delay)
            delay = min(delay * 2, _SHARE_DELAY_CEILING)


def _replace(src: str, dst: str) -> None:
    """``os.replace``, tolerating a Windows sharing violation that clears on its own."""
    _tolerate_sharing(os.replace, src, dst)


def remove_file(path: str) -> bool:
    """Unlink ``path``; True when it is gone, False when there was nothing to remove.

    Deleting the whole token store is a logout, and on Windows an unlink fails while another
    process still holds the file - a watcher mid-read, or the antivirus scan a fresh file attracts.
    Refusing to log out over that would be a worse outcome than the two lines of patience that
    cover it, so the same budget as a replace applies. A violation that never clears still raises:
    the tokens are then genuinely still there, and the user has to be told.
    """
    try:
        _tolerate_sharing(os.remove, path)
    except FileNotFoundError:
        return False
    return True


def atomic_write(path: str, data: str | bytes, private: bool = False) -> None:
    """Replace ``path`` with ``data`` atomically, leaving no temporary behind on failure."""
    fd, tmp = _create_temp(path, private)
    try:
        if isinstance(data, str):
            # The descriptor is O_BINARY, but a text wrapper translates on its own anyway:
            # newline=None rewrites every "\n" into os.linesep (events.jsonl readers count LF),
            # and with no encoding given it falls back to the ANSI code page, which raises on a
            # non-ASCII character or asset name. Pinning both keeps the durable bytes UTF-8 with
            # LF on every platform, so a file written on Windows reads identically on Linux.
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        else:
            handle = os.fdopen(fd, "wb")
        with handle as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())  # payload durable before the rename publishes it
        _replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj, private: bool = False) -> None:
    """``atomic_write`` for a JSON document."""
    atomic_write(path, json.dumps(obj, indent=2), private=private)
