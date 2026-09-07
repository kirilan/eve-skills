"""The platform seams of ``eve_skills.storage``: the Windows lock backend, binary
temporaries and the ``os.replace`` retry.

This checkout lives on Linux, so a real Windows run is impossible here. The Windows
path is therefore *executed*, not skipped: a second independent copy of ``storage.py``
is imported with ``fcntl`` masked and an ``msvcrt``-shaped fake installed - the import
that happens on Windows - and only its public surface (``file_lock``, ``atomic_write``)
is driven against it. What such a test cannot prove, namely that ``msvcrt.locking``
really refuses a held region and drops the lock when the holder dies, rests on the
stdlib contract; nothing here pretends to have verified that.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from eve_skills import storage

try:  # present on POSIX only; its absence is exactly one of the cases tested here.
    import fcntl as REAL_FCNTL
except ImportError:  # pragma: no cover - a Windows host has none to offer
    REAL_FCNTL = None


def import_storage(have_fcntl: bool, msvcrt_module: object | None) -> types.ModuleType:
    """A second copy of ``storage``, imported as it would be on a platform whose locking
    modules are exactly the ones supplied.

    ``None`` in ``sys.modules`` makes the matching ``import`` raise ImportError, which is
    how the module tells one platform from the other - so this simulates the interpreter,
    not the code under test.
    """
    spec = importlib.util.spec_from_file_location("storage_platform_probe", storage.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        "fcntl": REAL_FCNTL if have_fcntl else None,
        "msvcrt": msvcrt_module,
    }):
        spec.loader.exec_module(module)
    return module


class FakeMsvcrt:
    """``msvcrt``-shaped fake: ``locking(fd, mode, nbytes)`` locks the region at the
    descriptor's current position.

    Each ``LK_NBLCK`` consumes one entry of ``refusals`` and raises that errno; once the
    list runs out the region is granted. Every call is recorded together with the
    position it was made at, because ``msvcrt`` locks from wherever the position happens
    to be - so the caller has to seek first.
    """

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2
    LK_RLCK = 3
    LK_NBRLCK = 4

    def __init__(self, refusals: tuple[int, ...] = (), drift_after_lock: bool = False):
        self.refusals = list(refusals)
        # When set, a granted lock leaves the descriptor at a non-zero position, so an
        # unlock that does not seek first would release a region nobody locked.
        self.drift_after_lock = drift_after_lock
        self.calls: list[tuple[int, int, int, int]] = []  # mode, fd, nbytes, position

    @property
    def locks(self) -> list[tuple[int, int, int, int]]:
        return [call for call in self.calls if call[0] == self.LK_NBLCK]

    @property
    def unlocks(self) -> list[tuple[int, int, int, int]]:
        return [call for call in self.calls if call[0] == self.LK_UNLCK]

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        position = os.lseek(fd, 0, os.SEEK_CUR)  # the offset msvcrt would lock from
        self.calls.append((mode, fd, nbytes, position))
        if mode == self.LK_NBLCK:
            if self.refusals:
                raise OSError(self.refusals.pop(0), "region held by another process")
            if self.drift_after_lock:
                os.lseek(fd, 3, os.SEEK_SET)


class FakeClock:
    """Records the waits a backend asks for instead of spending them."""

    def __init__(self):
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


# ---------------------------------------------------------------------------
# Windows lock backend, driven through the public file_lock()
# ---------------------------------------------------------------------------

class WindowsLockBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-winlock-")
        self.addCleanup(self.tmp.cleanup)
        self.lock_path = os.path.join(self.tmp.name, "tokens.lock")

    def windows_storage(self, refusals: tuple[int, ...] = ()):
        """A ``storage`` copy that believes it is on Windows, with its fake clock."""
        fake = FakeMsvcrt(refusals)
        module = import_storage(have_fcntl=False, msvcrt_module=fake)
        clock = FakeClock()
        module.time = clock
        return module, fake, clock

    def test_import_without_fcntl_selects_the_msvcrt_backend(self):
        module, fake, _ = self.windows_storage()
        with module.file_lock(self.lock_path):
            pass
        self.assertEqual([call[0] for call in fake.calls], [fake.LK_NBLCK, fake.LK_UNLCK])

    @unittest.skipIf(REAL_FCNTL is None, "needs a platform that provides fcntl")
    def test_import_prefers_flock_when_both_mechanisms_exist(self):
        # Cygwin offers both; a kernel lock beats a polled byte range, and the Windows
        # path must not activate there.
        fake = FakeMsvcrt()
        module = import_storage(have_fcntl=True, msvcrt_module=fake)
        inside = False
        with module.file_lock(self.lock_path):
            inside = True
        self.assertTrue(inside)
        self.assertEqual(fake.calls, [])

    def test_import_without_any_locking_mechanism_fails_loudly(self):
        # Silent no-op locking would be worse than a CLI that will not start: every
        # read-modify-write caller would corrupt the token store believing itself locked.
        with self.assertRaises(ImportError):
            import_storage(have_fcntl=False, msvcrt_module=None)

    def test_lock_retries_while_the_region_is_refused(self):
        # Every spelling of "someone else holds it" must be tolerated, not only the one
        # Windows happens to report. LK_LOCK's give-up-after-ten is why contention here
        # is polled by us instead.
        module, fake, _ = self.windows_storage(refusals=(errno.EDEADLOCK, errno.EACCES, errno.EAGAIN))
        inside = False
        with module.file_lock(self.lock_path):
            inside = True
            self.assertEqual(len(fake.locks), 4)   # three refusals, then the grant
            self.assertEqual(fake.unlocks, [])     # and still held inside the block
        self.assertTrue(inside)
        self.assertEqual(len(fake.unlocks), 1)

    def test_lock_polling_backs_off_without_spinning(self):
        module, _, clock = self.windows_storage(refusals=(errno.EDEADLOCK,) * 6)
        with module.file_lock(self.lock_path):
            pass
        self.assertEqual(len(clock.delays), 6)                    # waits between attempts
        self.assertLess(clock.delays[0], 0.1)                     # responsive at first
        self.assertTrue(all(late >= early for early, late in zip(clock.delays, clock.delays[1:])))
        self.assertLess(max(clock.delays), 0.5)                   # but never a long stall

    def test_lock_is_reentrant_without_a_second_os_lock(self):
        """Nested acquisition must not ask msvcrt again - the region is already ours."""
        module, fake, _ = self.windows_storage()
        with module.file_lock(self.lock_path):
            with module.file_lock(self.lock_path):
                with module.file_lock(self.lock_path):
                    pass
            self.assertEqual(len(fake.locks), 1)
            self.assertEqual(fake.unlocks, [])
        self.assertEqual(len(fake.locks), 1)
        self.assertEqual(len(fake.unlocks), 1)

    def test_every_lock_call_targets_one_byte_at_offset_zero(self):
        fake = FakeMsvcrt(refusals=(errno.EDEADLOCK,), drift_after_lock=True)
        module = import_storage(have_fcntl=False, msvcrt_module=fake)
        with module.file_lock(self.lock_path):
            pass
        self.assertEqual({(nbytes, position) for _, _, nbytes, position in fake.calls}, {(1, 0)})

    def test_unusable_descriptor_is_not_retried_forever(self):
        """Only contention is retried; anything else must surface immediately."""
        module, fake, clock = self.windows_storage(refusals=(errno.EBADF,))
        with self.assertRaises(OSError):
            with module.file_lock(self.lock_path):
                self.fail("acquiring must not succeed")
        self.assertEqual(len(fake.locks), 1)          # no second attempt...
        self.assertEqual(clock.delays, [])            # ...and no waiting


# ---------------------------------------------------------------------------
# binary temporaries: a text-mode fd would rewrite the payload
# ---------------------------------------------------------------------------

class OsFlagProbe:
    """``os`` shim for a platform that *does* define ``O_BINARY``.

    The host kernel has never heard of the bit, so it is masked out before the real
    ``open``; what is under test is which flags ``storage`` asked for.
    """

    def __init__(self, binary_flag: int):
        self.O_BINARY = binary_flag
        self.flags: list[int] = []

    def __getattr__(self, name):
        return getattr(os, name)  # anything but open() is the real os

    def open(self, path: str, flags: int, mode: int) -> int:
        self.flags.append(flags)
        return os.open(path, flags & ~self.O_BINARY, mode)


class BinaryTemporaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-binary-")
        self.addCleanup(self.tmp.cleanup)

    def test_temporary_is_opened_binary_when_the_platform_defines_it(self):
        probe = OsFlagProbe(0o100_000)
        target = os.path.join(self.tmp.name, "events.jsonl")
        with mock.patch.object(storage, "os", probe):
            fd, tmp = storage._create_temp(target, private=False)
        os.close(fd)
        os.remove(tmp)
        self.assertEqual(len(probe.flags), 1)
        self.assertTrue(probe.flags[0] & probe.O_BINARY,
                        "a text-mode descriptor turns every \\n into \\r\\n on Windows")

    def test_newlines_survive_atomic_write_byte_for_byte(self):
        """The events.jsonl regression: its readers count lines, so \\r\\n is corruption."""
        path = os.path.join(self.tmp.name, "events.jsonl")
        payload = '{"event": "skill_done", "id": 1}\n{"event": "skill_done", "id": 2}\n'
        storage.atomic_write(path, payload)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw, payload.encode())
        self.assertNotIn(b"\r", raw)


# ---------------------------------------------------------------------------
# os.replace: atomic everywhere, but Windows refuses a destination that is open
# ---------------------------------------------------------------------------

class ReplaceSharingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-replace-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.clock = FakeClock()
        patcher = mock.patch.object(storage, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, path: str, obj) -> str:
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    def read(self, path: str):
        with open(path) as fh:
            return json.load(fh)

    def temp_leftovers(self) -> list[str]:
        return sorted(name for name in os.listdir(self.dir) if name.endswith(".tmp"))

    def test_replace_retries_a_sharing_violation_that_clears(self):
        path = self.seed(os.path.join(self.dir, "watch-state.json"), {"phase": "old"})
        real_replace = os.replace
        attempts: list[str] = []

        def held_then_free(src: str, dst: str) -> None:
            """Refuses once like a peer that still has the destination open, then renames."""
            attempts.append(src)
            if len(attempts) == 1:
                raise PermissionError(errno.EACCES, "another process has the file open")
            real_replace(src, dst)

        with mock.patch.object(storage.os, "replace", side_effect=held_then_free):
            storage.atomic_write_json(path, {"phase": "new"})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.clock.delays), 1)          # waited, did not spin
        self.assertEqual(self.read(path), {"phase": "new"})
        self.assertEqual(self.temp_leftovers(), [])

    def test_replace_that_never_clears_reraises_and_leaves_no_temporary(self):
        path = self.seed(os.path.join(self.dir, "watch-state.json"), {"phase": "old"})
        denied = PermissionError(errno.EACCES, "another process has the file open")
        with mock.patch.object(storage.os, "replace", side_effect=denied) as replaced:
            with self.assertRaises(PermissionError):
                storage.atomic_write_json(path, {"phase": "new"})
        self.assertEqual(self.read(path), {"phase": "old"})   # old file untouched
        self.assertEqual(self.temp_leftovers(), [])
        self.assertGreater(replaced.call_count, 1)            # it did retry...
        self.assertLess(replaced.call_count, 100)             # ...within a bounded window

    def test_replace_does_not_retry_an_unrelated_failure(self):
        path = self.seed(os.path.join(self.dir, "watch-state.json"), {"phase": "old"})
        full = OSError(errno.ENOSPC, "disk full")
        with mock.patch.object(storage.os, "replace", side_effect=full) as replaced:
            with self.assertRaises(OSError) as caught:
                storage.atomic_write_json(path, {"phase": "new"})
        self.assertIs(caught.exception, full)                 # the original error, unwrapped
        self.assertEqual(replaced.call_count, 1)              # no point waiting out a full disk
        self.assertEqual(self.temp_leftovers(), [])


if __name__ == "__main__":
    unittest.main()
