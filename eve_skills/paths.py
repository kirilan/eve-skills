"""Where eve-skills keeps its files - the one place in the package that knows.

Four kinds of state, four directories, resolved by one rule on every platform:

1. ``XDG_CONFIG_HOME`` / ``XDG_CACHE_HOME`` / ``XDG_DATA_HOME`` / ``XDG_STATE_HOME``
   win on *every* platform when set and non-empty - containers, test fixtures and
   power users pin their layout with them, Windows included (it is also what makes
   the Windows branch testable from a POSIX host).
2. On POSIX the XDG defaults apply: ``~/.config``, ``~/.cache``, ``~/.local/share``
   and ``~/.local/state``, each with the ``eve-skills`` leaf appended.
3. On Windows those variables have no meaning, so the platform's own profile
   folders do: everything lives under ``%LOCALAPPDATA%\\eve-skills\\<kind>``,
   config included. Roaming was the obvious home for settings until you notice what
   the config directory actually holds - ``tokens.json``, with a live refresh token
   per character, and an optional OAuth client secret. ``%APPDATA%`` is replicated
   by domain profile sync and OneDrive Known Folder Move, so roaming it would copy
   working credentials onto file servers and cloud storage the user never chose to
   trust with them. Caches, SDE downloads and watch state stay local for the duller
   reason that replicating regenerable or machine-specific files is only a
   liability. A missing variable falls back to the documented
   ``<profile>\\AppData\\Local`` location, and if even the user profile cannot be
   located the POSIX-shaped path is returned as a last resort: a resolver here can
   name the wrong tree, but never nothing.

Every resolver keeps the same ``create`` contract: ``create=False`` resolves without
touching disk, which is what every reader - and the read-only ``doctor`` - relies on.
"""

from __future__ import annotations

import os

LEAF = "eve-skills"


def is_windows() -> bool:
    """The package's single platform judgement: does this OS keep state in a
    Windows user-profile tree rather than an XDG one?

    Locking and writes are judged by capability (see ``storage``), because the
    mechanisms differ; *layout* has to be judged by the documented OS contract -
    Windows is where ``%APPDATA%``/``%LOCALAPPDATA%`` name the profile and XDG does
    not exist, whatever else the runtime happens to provide. Tests drive the other
    branch on any host by injecting here; nothing else may re-derive the platform."""
    return os.name == "nt"


# kind -> (XDG variable, POSIX default parts under ~, leaf under the Windows root,
#          Windows profile variable, its documented fallback under the profile)
# The POSIX default is parts rather than ".local/share": joined with os.sep it stays a
# well-formed path on whichever host resolves it, instead of mixing separators when a test
# (or a Cygwin-shaped runtime) drives the POSIX branch on Windows.
_KINDS = {
    "config": ("XDG_CONFIG_HOME", (".config",), ("config",), "LOCALAPPDATA", ("AppData", "Local")),
    "cache": ("XDG_CACHE_HOME", (".cache",), ("cache",), "LOCALAPPDATA", ("AppData", "Local")),
    "data": ("XDG_DATA_HOME", (".local", "share"), ("data",), "LOCALAPPDATA", ("AppData", "Local")),
    "state": ("XDG_STATE_HOME", (".local", "state"), ("state",), "LOCALAPPDATA", ("AppData", "Local")),
}


def _profile_root(profile_var: str, fallback_parts: tuple[str, ...]) -> str | None:
    """``%APPDATA%``/``%LOCALAPPDATA%``, else the documented location under the user
    profile, else None. ``expanduser`` hands its argument back when it cannot
    identify a user (and may raise outright), so "no home" is detected, not guessed."""
    value = os.environ.get(profile_var)
    if value:
        return value
    try:
        home = os.path.expanduser("~")
    except (OSError, RuntimeError):  # no environment and no passwd entry to fall back on
        return None
    if not home or home == "~":
        return None
    return os.path.join(home, *fallback_parts)


def _resolve(kind: str, create: bool) -> str:
    xdg_var, posix_parts, windows_leaf, profile_var, profile_fallback = _KINDS[kind]
    root = os.environ.get(xdg_var)  # honoured on every platform; empty counts as unset
    if root:  # an explicit pin is taken exactly as given - no kind leaf it did not ask for
        path = os.path.join(root, LEAF)
    elif is_windows():
        # No XDG meaning here: the profile folders are the layout, each kind under its own leaf.
        root = _profile_root(profile_var, profile_fallback)
        if root:
            path = os.path.join(root, LEAF, *windows_leaf)
    if not root:  # POSIX default, and the last resort on a Windows with no locatable profile
        path = os.path.expanduser(os.path.join("~", *posix_parts, LEAF))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def config_dir(create: bool = True) -> str:
    """Client config, token store and SP history: ``$XDG_CONFIG_HOME/eve-skills`` on
    POSIX, ``%LOCALAPPDATA%\\eve-skills\\config`` on Windows - local, never roaming,
    because this directory holds live refresh tokens and an optional client secret."""
    return _resolve("config", create)


def cache_dir(create: bool = True) -> str:
    """Regenerable caches (SSO endpoints, id-to-name lookups):
    ``$XDG_CACHE_HOME/eve-skills`` on POSIX, ``%LOCALAPPDATA%\\eve-skills\\cache``."""
    return _resolve("cache", create)


def data_dir(create: bool = True) -> str:
    """Downloaded SDE documents: ``$XDG_DATA_HOME/eve-skills`` on POSIX,
    ``%LOCALAPPDATA%\\eve-skills\\data`` - re-downloadable, so it must not roam."""
    return _resolve("data", create)


def state_dir(create: bool = True) -> str:
    """Watch state and recorded events: ``$XDG_STATE_HOME/eve-skills`` on POSIX,
    ``%LOCALAPPDATA%\\eve-skills\\state`` - machine-local by definition."""
    return _resolve("state", create)
