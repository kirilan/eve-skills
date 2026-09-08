"""The single seam where a test states which platform its subject's semantics belong to.

The suite runs on Linux, macOS and Windows runners, and three different situations look
identical from a failure message but must not be fixed identically:

* The subject is POSIX-only machinery - a ``0600`` mode bit, an advisory ``flock``, a hint
  meant to be pasted into ``sh``. Use :func:`posix_only` with a reason that names the
  semantic. A skip without a reason is how a platform quietly stops being covered at all.
* The test merely *spells* itself in POSIX - a ``/`` inside an expected path suffix, ``$HOME``
  inside a string it built by hand. That is not platform-dependent behaviour, so it gets
  fixed: build the expectation with :func:`os.path.join`, or drive
  ``eve_skills.paths.is_windows`` the way the product branches on it, and both hosts check
  the same thing.
* Only one clause of a test needs to deny access to a file. Windows ``chmod`` toggles the
  read-only attribute and nothing else, so a ``0o000`` file stays readable there and a
  permission verdict cannot be provoked; guard that clause with :data:`POSIX_MODE_BITS` and
  let the rest of the test run everywhere.

Every guard here is written so that POSIX keeps asserting everything it always did: nothing
in this module can add a skip on Linux. Where Windows genuinely behaves differently the
product says so out loud (``doctor`` reports a documented SKIP naming the ACL), and the
Windows-side assertion belongs in the test, not in a hole.
"""

from __future__ import annotations

import os
import unittest

#: True on the one platform with POSIX permission bits, ``os.replace`` semantics that tolerate
#: an open destination, and advisory locking. Used for whole tests; :data:`POSIX_MODE_BITS`
#: is for the clause inside a test that still has portable assertions left.
IS_POSIX = os.name == "posix"

#: True where ``chmod`` can actually deny a read, which is the clause-level question inside a
#: test that still has portable assertions left. On Windows it cannot: the call only sets or
#: clears the read-only attribute, ``stat`` reports 666/444, and the real protection is the ACL
#: inherited from the user profile - which is what :mod:`eve_skills.doctor` asserts there.
#: (Locking needs no flag of its own: Windows ``msvcrt.locking`` is mandatory on the locked bytes
#: rather than advisory, but ``storage`` picks that backend itself and
#: ``tests/test_storage_platform.py`` drives both against fakes, so no test asks the host.)
POSIX_MODE_BITS = IS_POSIX


def posix_only(reason: str):
    """Skip the decorated test off POSIX, with the semantic that cannot hold elsewhere named.

    ``reason`` is load-bearing: it must say *what* about the subject is POSIX (a mode bit, an
    advisory lock, a shell verb), not merely "needs POSIX". Read it from the skip list of a
    Windows run and you should be able to tell whether something real went untested.
    """
    if not reason.strip():  # pragma: no cover - guards the seam itself
        raise ValueError("a platform skip must name the POSIX semantic it protects")
    return unittest.skipUnless(IS_POSIX, reason)


def home_variables(home: str) -> dict[str, str]:
    """The variables each OS consults for ``~``, pinned to one fake directory.

    ``os.path.expanduser`` reads ``HOME`` on POSIX and ``USERPROFILE`` on Windows (falling back
    to ``HOMEDRIVE``+``HOMEPATH``) - never ``HOME``. A fixture that pins only ``HOME`` keeps
    the runner's real profile in play on Windows, so every path under test lands outside the
    fake home and collapses into ``~`` for the wrong reason.
    """
    return {"HOME": home, "USERPROFILE": home}
