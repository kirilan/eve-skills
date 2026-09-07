"""Read-only diagnostics behind ``eve-skills doctor``.

Three promises shape everything here:

* **Nothing is written.** Files are read through the ``peek_*`` accessors and the
  ``create=False`` path resolvers, so doctor never migrates the token store, caches SSO
  endpoints, refreshes a token, updates the SDE or creates a state directory. It reports on
  an install exactly as the next real command will find it.
* **Nothing secret is printed.** Character diagnostics are built from a field whitelist, and
  every string in the report passes a :class:`Redactor` seeded with the credential values
  found on disk - so even an exception raised around a request cannot smuggle an access
  token, refresh token, client secret or authorization code into text or JSON.
* **No path gives the operator away.** Every filesystem path is reported ``~``-relative when it is
  under the home directory - an expanded ``/home/<name>/...`` would carry the OS username into a
  pasted report - and verbatim when it is not, because an explicit ``$XDG_*`` location is the
  operator's own choice and is what a diagnostic has to name. Advice meant for a shell quotes it as
  ``"$HOME/<rest>"``, because a tilde inside single quotes is literal and the command would fail.

Exit status is nonzero only when something actually blocks the tool (unreadable/corrupt
token store, an expired login that cannot refresh, a service unreachable with no cached
fallback). Everything worth knowing but survivable is a warning, and warnings exit 0.

The network probes are strictly opt-in (``--network``), unauthenticated and bounded: they
fetch the public SSO discovery document and three public ESI documents with a per-request
timeout, and classify *why* a request failed (DNS, timeout, TLS, refused, HTTP status)
instead of reporting "network error". The third is one regional order book, which is the
only ESI endpoint that publishes a rate limit - so it also reports how old CCP's copy of the
market is and how much of the request budget is left.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import (__version__, alphadata, esi as esi_mod, market, paths, render, snapshots, sso,
               watchstate)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
STATUSES = (OK, WARN, FAIL, SKIP)
REPORT_VERSION = 1

ENDPOINT_CACHE_HOURS = 24.0     # how long sso._discover trusts its cached document
NET_TIMEOUT = 10.0              # per request; a diagnostic must never hang a user
REDACTED = "[redacted]"

ESI_STATUS_PATH = "/status"
ESI_COMPAT_PATH = "/meta/compatibility-dates"

# One regional book is the cheapest way to see the two things only that endpoint publishes: how
# old CCP's copy of the market is, and the request budget every `market` query spends. Jita's region
# for Tritanium is the busiest book there is, so it answers on every probe rather than by luck.
MARKET_PROBE_REGION_ID = market.HUBS["jita"].region_id
MARKET_PROBE_TYPE_ID = 34
MARKET_PROBE_PATH = market.book_path(MARKET_PROBE_REGION_ID, MARKET_PROBE_TYPE_ID)

# The event kinds `events` splits the history by; derived from the vocabulary itself so a future
# kind is counted under the right heading without anyone remembering to edit this file.
ORDER_EVENT_KINDS = tuple(k for k in watchstate.EVENT_KINDS if k.startswith("order_"))


# ---------------------------------------------------------------------------
# secret hygiene
# ---------------------------------------------------------------------------

class Redactor:
    """Removes credential material from diagnostic text.

    Seeded with the exact values read out of config.json and tokens.json, so any accidental
    interpolation - including inside an exception message from a transport failure - is
    caught, plus shape rules for bearer/JWT traffic that no local file accounts for."""

    _PATTERNS = (
        re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}=*"),
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
    )

    def __init__(self, values=()):
        self.values = sorted({v for v in values if isinstance(v, str) and len(v) >= 6},
                             key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.values:
            if secret in value:
                value = value.replace(secret, REDACTED)
        for pattern in self._PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value

    def tree(self, node):
        """Scrub every string in a JSON-shaped structure, keys included."""
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, list):
            return [self.tree(item) for item in node]
        if isinstance(node, dict):
            return {self.text(key): self.tree(value) for key, value in node.items()}
        return node


# ---------------------------------------------------------------------------
# path hygiene
# ---------------------------------------------------------------------------

# Where a home prefix counts as standing alone inside free text: at either end of the string or
# beside a delimiter. The look-behind rejects a preceding path character, so `/backup/home/alice/x`
# (a copy of something) is left alone; the look-ahead rejects a character that would continue the
# name, so `/home/alice2/x` - another account entirely - never becomes `~2/x`. A trailing `/` is
# deliberately allowed in the second set: that is a path continuing *under* home.
_HOME_EDGE_BEFORE = r"\s'\"`,;:|=(\[{<"
_HOME_EDGE_AFTER = r"\s'\"`,;:|)\]}<>"


def _home() -> str | None:
    """The operator's home directory, resolved the way every path in this tool is, or None.

    None means there is nothing safe to strip: `expanduser` hands back its own argument when it
    cannot identify a user (and may raise outright), and a home of `/` would rewrite every absolute
    path on the machine into something false."""
    try:
        home = os.path.expanduser("~")
    except (OSError, RuntimeError):  # no $HOME and no passwd entry to fall back on
        return None
    if not home or home == "~":
        return None
    trimmed = home.rstrip(os.sep + (os.altsep or ""))
    return trimmed or None          # a HOME of nothing but separators is not a prefix


def _display_path(path: str | None) -> str | None:
    """One filesystem path as the report shows it: `~`-relative under home, verbatim elsewhere.

    `/home/alice/.config/eve-skills/tokens.json` becomes `~/.config/eve-skills/tokens.json`, so a
    pasted report does not carry the OS username. A path outside home is printed exactly as it is:
    an explicit `$XDG_*` location is the operator's own deliberate choice, and naming it is the
    whole point of the diagnostic."""
    home = _home()
    if home is None or not isinstance(path, str) or not path:
        return path
    if path == home:
        return "~"
    for sep in [s for s in (os.sep, os.altsep) if s]:
        if path.startswith(home + sep):
            return "~" + path[len(home):]
    return path


def _shell_path(path: str | None) -> str | None:
    """One path as a shell argument, so a hint can be pasted back and run as printed.

    The readable form begins with `~/`, and no POSIX shell expands a tilde inside single quotes -
    `chmod 755 '~/.local/state'` goes looking for a directory literally named `~`. Double quotes do
    expand `$HOME` and still tolerate spaces, so the home-relative form is quoted as
    `"$HOME/<rest>"`; a path outside home needs no expansion at all and keeps single quotes.

    On Windows neither tilde nor `$HOME` means anything to cmd - `%USERPROFILE%` does - so the same
    two forms become `"%USERPROFILE%"` and `"%USERPROFILE\\<rest>"`, double-quoted for spaces; a path
    outside the profile keeps its absolute form in double quotes, since Windows has no single-quote
    quoting at all."""
    shown = _display_path(path)
    if not isinstance(shown, str) or not shown:
        return shown
    if paths.is_windows():
        if shown == "~":
            return '"%USERPROFILE%"'
        if shown.startswith("~") and shown[1:2] in (os.sep, os.altsep):
            return '"%USERPROFILE%' + shown[1:] + '"'
        return f'"{shown}"'
    if shown == "~":
        return '"$HOME"'
    if shown.startswith("~") and shown[1:2] in (os.sep, os.altsep):
        return '"$HOME' + shown[1:] + '"'
    return f"'{shown}'"


def _mask_home(node):
    """Final pass over the report: rewrite any home path still spelled out in a string.

    Defence in depth for checks added later - it also catches a path interpolated into the middle
    of a detail or hint, where :func:`_display_path` would have had to be applied to the whole
    value. It is deliberately separate from :class:`Redactor`: a path is not a credential, so it
    collapses to `~` here and never to `[redacted]`. Keys are rewritten too, since a future check
    could just as easily use one as a field name."""
    home = _home()
    if home is None:
        return node
    pattern = re.compile(rf"(?<![^{_HOME_EDGE_BEFORE}]){re.escape(home)}(?![^{_HOME_EDGE_AFTER}/])")

    def rewrite(value):
        if isinstance(value, str):
            return pattern.sub("~", value)
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {rewrite(key): rewrite(item) for key, item in value.items()}
        return value

    return rewrite(node)


# ---------------------------------------------------------------------------
# small read-only primitives
# ---------------------------------------------------------------------------

def _check(name: str, status: str, detail: str, hint: str | None = None, **fields) -> dict:
    out = {"name": name, "status": status, "detail": detail}
    if hint:
        out["hint"] = hint
    out.update(fields)
    return out


def _file_state(path: str) -> dict:
    """Existence, readability, writability and permissions of ``path`` - without touching it."""
    state = {"exists": False, "directory": False, "readable": False, "writable": False, "mode": None}
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return state
    except OSError as err:  # unreadable parent directory, symlink loop, ...
        state["error"] = type(err).__name__
        return state
    state.update(exists=True, directory=os.path.isdir(path), readable=os.access(path, os.R_OK),
                 writable=os.access(path, os.W_OK), mode=f"{stat.S_IMODE(info.st_mode):03o}")
    return state


def _read_doc(path: str) -> tuple[dict | None, str | None]:
    """JSON document at ``path``; returns (document, problem) and never echoes contents."""
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError:
        return None, "corrupt"
    except OSError:
        return None, "unreadable"
    if not isinstance(doc, dict):
        return None, "corrupt"
    return doc, None


def _number(value) -> float | None:
    """``value`` as a finite number, or None.

    JSON is loose about this: ESI reports server_version as a string and a hand-edited token
    file can hold expires_at as text, so numeric strings count and non-finite ones do not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _duration(seconds: float) -> str:
    return render.format_duration(seconds)


def _os_label() -> str:
    """Which OS produced this report - the reader of a pasted report may not be its author,
    and half these checks (paths, ACLs, shells) mean different things on different ones."""
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


# ---------------------------------------------------------------------------
# offline checks
# ---------------------------------------------------------------------------

def _check_package() -> list[dict]:
    python = platform.python_version()
    fields = {"package_version": __version__, "python_version": python, "platform": _os_label(),
              "executable": _display_path(sys.executable) or "(unknown)",
              "module_dir": _display_path(os.path.dirname(os.path.abspath(__file__)))}
    if sys.version_info < (3, 11):
        return [_check("package", WARN, f"eve-skills {__version__} is running on Python {python}; this tool needs 3.11+",
                       hint="install a newer Python or reinstall eve-skills against one", **fields)]
    return [_check("package", OK, f"eve-skills {__version__} on Python {python}", **fields)]


PATH_TARGETS = (
    ("path.config", "config directory", lambda: paths.config_dir(create=False),
     "client configuration, token store and SP history", FAIL),
    ("path.cache", "cache directory", lambda: paths.cache_dir(create=False),
     "cached SSO endpoints and id-to-name lookups", WARN),
    ("path.data", "data directory", lambda: paths.data_dir(create=False),
     "downloaded SDE alpha-cap data", WARN),
    ("path.state", "state directory", lambda: paths.state_dir(create=False),
     "watch state and recorded events (machine-local, not secret)", WARN),
)


def _check_paths() -> list[dict]:
    checks = []
    for name, label, resolve, purpose, unwritable in PATH_TARGETS:
        path = resolve()
        shown = _display_path(path)
        shell = _shell_path(path)
        state = _file_state(path)
        fields = {"path": shown, "purpose": purpose, **state}
        if not state["exists"]:
            checks.append(_check(name, OK, f"{label} does not exist yet - it is created on the first write", **fields))
        elif state.get("error") or not state["readable"]:
            hint = (f"grant yourself Read & execute on {shell} through its Windows Security properties"
                    if paths.is_windows() else f"restore read access: chmod u+rX {shell}")
            checks.append(_check(name, FAIL, f"{label} {shown} cannot be read", hint=hint, **fields))
        elif not state["writable"]:
            advice = ("grant yourself Modify on {shell}" if paths.is_windows()
                      else "restore write access: chmod u+w {shell}")
            checks.append(_check(name, unwritable, f"{label} {shown} is not writable by this user",
                                 hint=advice.format(shell=shell) +
                                      " (login, token refresh and history need to write here)", **fields))
        elif paths.is_windows():
            # No group/other bit exists to judge: a file created here inherits the ACL of the
            # user profile, which is precisely what keeps it private. Every Windows stat() reports
            # mode 666; warning about it would be a phantom problem with an unrunnable fix.
            checks.append(_check(name, SKIP,
                                 f"{label} {shown} exists; privacy comes from the ACL inherited from the "
                                 "Windows user profile, and mode bits carry no meaning here", **fields))
        elif state["mode"] and int(state["mode"], 8) & 0o022:
            checks.append(_check(name, WARN, f"{label} {shown} can be written by group/other (mode {state['mode']})",
                                 hint=f"chmod 755 {shell}, or 700 to hide its contents as well", **fields))
        else:
            checks.append(_check(name, OK, f"{label} {shown} exists and only this user may write it", **fields))
    return checks


def _check_config(cfg: dict, records: list[dict]) -> list[dict]:
    state = _file_state(cfg["file"])
    shown = _display_path(cfg["file"])
    shell = _shell_path(cfg["file"])
    fields = {"path": shown, "mode": state["mode"], "client_id_present": cfg["client_id"],
              "client_secret_present": cfg["client_secret"], "from_environment": cfg["env_client_id"]}
    checks = []
    if cfg["problem"] in ("corrupt", "unreadable"):
        # One line says what is wrong with the file; a second would only contradict it.
        return [_check("config.client_id", FAIL, f"the client configuration file is {cfg['problem']}",
                       hint=f"fix or remove {shell}, then run: eve-skills login --client-id <id>", **fields)]
    if not cfg["client_id"]:
        checks.append(_check(
            "config.client_id", FAIL if not records else WARN,
            "no application client id is configured"
            + ("" if records else " and no character is logged in"),
            hint="register an application at https://developers.eveonline.com/applications, then run: "
                 "eve-skills login --client-id <id> (or export EVE_SKILLS_CLIENT_ID)", **fields))
    else:
        source = "EVE_SKILLS_CLIENT_ID" if cfg["env_client_id"] else "config.json"
        checks.append(_check("config.client_id", OK, f"application client id is configured in {source} (value not shown)", **fields))

    if cfg["problem"] == "missing":
        checks.append(_check("config.file", OK, "no config.json yet - defaults and environment variables apply", **fields))
    elif paths.is_windows():
        # Nothing here is judgable from mode bits, and a secret-bearing file must not be told to
        # `chmod 600` a platform that has no such bit: it inherits the user profile's ACL instead.
        if cfg["client_secret"]:
            checks.append(_check("config.file", SKIP,
                                 "config.json holds a client secret; on Windows its privacy comes from the "
                                 "ACL inherited from the user profile, and mode bits carry no meaning here", **fields))
        else:
            checks.append(_check("config.file", OK, "config.json is readable and holds no client secret", **fields))
    elif cfg["client_secret"] and state["mode"] and int(state["mode"], 8) & 0o077:
        checks.append(_check("config.file", WARN,
                             f"config.json is readable beyond its owner (mode {state['mode']}) and stores a client secret",
                             hint=f"chmod 600 {shell}", **fields))
    else:
        checks.append(_check("config.file", OK,
                             f"config.json is readable (mode {state['mode'] or 'unknown'})"
                             + (" and holds no client secret" if not cfg["client_secret"] else ""), **fields))

    ua_fields = {"user_agent_present": cfg["user_agent"]}
    if cfg["user_agent"]:
        checks.append(_check("config.user_agent", OK, "a custom ESI User-Agent is configured", **ua_fields))
    else:
        checks.append(_check(
            "config.user_agent", WARN, "no ESI User-Agent configured - CCP asks tools to identify themselves with a contact",
            hint='set user_agent in config.json, e.g. "eve-skills/' + __version__ + ' (you@example.com)"', **ua_fields))
    return checks


STORE_LOGIN_HINT = "run: eve-skills login"
# The move is the running platform's own verb over its own shell quoting, filled in at use time.
STORE_MOVE_HINT = 'move it aside and log in again: {move}, then run: eve-skills login'

STORE_PROBLEMS = {
    # problem -> (status, detail, hint template; a {move} placeholder is resolved by _store_hint)
    "missing": (WARN, "no token store yet", STORE_LOGIN_HINT),
    "unreadable": (FAIL, "the token store exists but cannot be read", STORE_MOVE_HINT),
    "corrupt": (FAIL, "the token store is not valid JSON", STORE_MOVE_HINT),
    "legacy": (WARN, "old single-character token file - the next eve-skills command migrates it in place, tightening it to 0600", None),
    "legacy-unusable": (FAIL, "legacy token file has no character_id, so it cannot be migrated", STORE_LOGIN_HINT),
}


def _move_command(path: str) -> str:
    """`mv src src.bak` - or `move "src" "src.bak"` on Windows, where cmd knows neither mv nor
    the POSIX quoting :func:`_shell_path` would otherwise apply."""
    verb = "move" if paths.is_windows() else "mv"
    return f"{verb} {_shell_path(path)} {_shell_path(f'{path}.bak')}"


def _store_hint(problem: str, file: str) -> str | None:
    hint = STORE_PROBLEMS[problem][2]
    if hint is None:
        return None
    return hint.format(move=_move_command(file)) if "{move}" in hint else hint


def _check_store(store: dict) -> list[dict]:
    shown = _display_path(store["file"])
    fields = {"path": shown, "characters": len(store["records"])}
    problem = store["problem"]
    if problem is None:
        detail = (f"{len(store['records'])} stored character record(s)" if store["records"]
                  else "the token store holds no characters")
        checks = [_check("tokens.store", OK, detail, **fields)]
    else:
        status, detail, _template = STORE_PROBLEMS[problem]
        checks = [_check("tokens.store", status, detail,
                         hint=_store_hint(problem, store["file"]), **fields)]
    state = _file_state(store["file"])
    perm_fields = {"path": shown, "mode": state["mode"]}
    if not state["exists"]:
        checks.append(_check("permissions.tokens", OK, "no token file to check permissions on", **perm_fields))
    elif paths.is_windows():
        checks.append(_check("permissions.tokens", SKIP,
                             "the token store's privacy comes from the ACL inherited from the Windows user "
                             "profile; mode bits carry no meaning there", **perm_fields))
    elif state["mode"] and int(state["mode"], 8) & 0o077:
        checks.append(_check("permissions.tokens", WARN,
                             f"the token store is readable beyond its owner (mode {state['mode']})",
                             hint=f"chmod 600 {_shell_path(store['file'])} - it holds live access and refresh tokens",
                             **perm_fields))
    else:
        checks.append(_check("permissions.tokens", OK, f"the token store is private to its owner (mode {state['mode'] or 'unknown'})", **perm_fields))
    return checks


def _character_view(record: dict, now: float) -> dict:
    """Consumer-safe projection of one stored record.

    Whitelist by construction: identity, expiry, refresh capability and consent. Token and
    secret fields are never copied, whatever a future record layout adds."""
    char_id = record.get("character_id")
    try:
        char_id = int(char_id)
    except (TypeError, ValueError):
        char_id = None
    expires_at = _number(record.get("expires_at"))
    return {
        "character_id": char_id,
        "name": record.get("character_name") or (str(char_id) if char_id else "(unnamed character)"),
        "client_id_present": bool(record.get("client_id")),
        "auto_refresh": bool(record.get("refresh_token")),
        "scopes_known": isinstance(record.get("scopes"), list),
        "expires_at": _iso(expires_at) if expires_at is not None else None,
        "expires_in": int(expires_at - now) if expires_at is not None else None,
    }


def _iso(moment: float | None) -> str | None:
    return datetime.fromtimestamp(moment, timezone.utc).replace(microsecond=0).isoformat() if moment is not None else None


def _check_characters(store: dict, now: float) -> list[dict]:
    records = store["records"]
    if not records:
        hint = "run: eve-skills login" + ("" if store["problem"] == "missing"
                                          else f" (after fixing {_shell_path(store['file'])})")
        return [_check("characters", FAIL, "no character is logged in", hint=hint, characters=0)]

    checks = []
    for record in records:
        view = _character_view(record, now)
        name, label = view["name"], f"character.{view['character_id'] if view['character_id'] is not None else view['name']}"
        problems, notes = [], []
        # Features the base scopes already grant are not extra consent worth reporting.
        base_scopes = set(sso.SCOPES)
        optional = [f for f in sso.OPTIONAL_SCOPES if not set(sso.OPTIONAL_SCOPES[f]) <= base_scopes]
        granted = [f for f in optional if sso.has_feature(record, f)] if view["scopes_known"] else []
        missing_core = [s for s in sso.SCOPES if not sso.has_scope(record, s)] if view["scopes_known"] else []

        if view["character_id"] is None:
            problems.append("the record has no usable character_id")
        if not view["client_id_present"]:
            problems.append("the record has no client id, so refreshing it would fail")
        if not view["scopes_known"]:
            notes.append("the stored consent is unknown (no scope list in the record)")
        elif missing_core:
            problems.append(f"consent is missing {', '.join(missing_core)}")

        expires_in = view["expires_in"]
        if expires_in is None:
            problems.append("the record has no usable expiry time")
            access = "unknown"
        elif expires_in <= 0:
            access = f"expired {_duration(-expires_in)} ago"
            if view["auto_refresh"]:
                notes.append(f"access token {access}; the next command refreshes it")
            else:
                problems.append(f"the access token {access} and there is no refresh token")
        else:
            access = f"valid for {_duration(expires_in)}"

        consent = ("base" + (" + " + ", ".join(granted) if granted else " only")) if view["scopes_known"] else "unknown"
        status = FAIL if problems else (WARN if notes else OK)
        detail = "; ".join(problems or notes) or f"access token {access}; consent: {consent}"
        hint = None
        if problems and any("refresh" in p or "client id" in p for p in problems):
            hint = f"run: eve-skills login --char '{name}' (pick '{name}' in the browser)"
        elif problems and missing_core:
            hint = f"re-consent needs a browser login: eve-skills login --char '{name}'"
        checks.append(_check(label, status, detail, hint=hint, character=name, character_id=view["character_id"],
                             access_state=access, auto_refresh="yes" if view["auto_refresh"] else "no",
                             consent=consent, granted_features=granted, missing_core_scopes=missing_core,
                             scopes_known=view["scopes_known"], expires_at=view["expires_at"],
                             expires_in=expires_in))

    broken = sum(1 for c in checks if c["status"] == FAIL)
    total = len(checks)
    if broken == total:
        aggregate = _check("characters", FAIL, f"none of the {total} stored character(s) can be used")
    elif broken:
        aggregate = _check("characters", WARN, f"{broken} of {total} stored character(s) need attention; "
                                              f"{total - broken} still work")
    else:
        aggregate = _check("characters", OK, f"{total} stored character(s), all usable")
    return [aggregate, *checks]


def _data_documents() -> list[dict]:
    """Where each SDE document actually resolves from, mirroring alphadata._read's order."""
    entries = []
    user_dir = paths.data_dir(create=False)
    for name in alphadata.DATA_FILES:
        chosen, origin = None, None
        for candidate, label in ((os.path.join(user_dir, name), "user data"),
                                 (str(alphadata.PACKAGE_DATA_DIR / name), "bundled package")):
            if os.path.isfile(candidate):
                chosen, origin = candidate, label
                break
        entry = {"name": name, "present": chosen is not None, "origin": origin, "path": _display_path(chosen),
                 "build": None, "fetched": None, "problem": None}
        if chosen:
            doc, problem = _read_doc(chosen)
            if problem:
                entry["problem"] = problem
            else:
                entry["build"] = doc.get("build")
                entry["fetched"] = doc.get("fetched")
        entries.append(entry)
    return entries


def _check_sde(now: float) -> list[dict]:
    entries = _data_documents()
    by_name = {e["name"]: e for e in entries}
    grades = by_name["clone_grades.json"]
    ages = {}
    for entry in entries:
        if entry["problem"]:
            continue
        ages[entry["name"]] = alphadata.stamp_age_days(entry["fetched"], now=now)

    builds = sorted({e["build"] for e in entries if e["present"] and e["build"] is not None})
    fields = {"files": entries, "builds": builds, "age_days": ages.get("clone_grades.json")}
    if not grades["present"]:
        return [_check("data.alpha_caps", FAIL, "no alpha-cap data is installed",
                       hint="run: eve-skills update-data (~100 MB download)", **fields)]
    if grades["problem"]:
        return [_check("data.alpha_caps", FAIL, f"the alpha-cap data file is {grades['problem']}",
                       hint="re-download it: eve-skills update-data", **fields)]

    age = ages["clone_grades.json"]
    origin = grades["origin"]
    fields["origin"] = origin
    if age is None:
        checks = [_check("data.alpha_caps", WARN, f"alpha caps from SDE build {grades['build']} ({origin}) carry no fetch date - age unknown",
                         hint="run: eve-skills update-data", **fields)]
    elif age > alphadata.STALE_DAYS:
        checks = [_check("data.alpha_caps", WARN, f"alpha caps are {age:.0f} days old (SDE build {grades['build']}, {origin})",
                         hint="run: eve-skills update-data", **fields)]
    else:
        checks = [_check("data.alpha_caps", OK, f"alpha caps from SDE build {grades['build']}, {age:.0f} days old ({origin})", **fields)]

    catalog = by_name["skill_catalog.json"]
    catalog_fields = {"path": catalog["path"], "present": catalog["present"], "build": catalog["build"]}
    if not catalog["present"]:
        checks.append(_check("data.skill_catalog", WARN, "the skill catalog is not installed - plan cannot resolve or price skills",
                             hint="run: eve-skills update-data", **catalog_fields))
    elif catalog["problem"]:
        checks.append(_check("data.skill_catalog", WARN, f"the skill catalog is {catalog['problem']}",
                             hint="re-download it: eve-skills update-data", **catalog_fields))
    else:
        checks.append(_check("data.skill_catalog", OK, f"skill catalog available (SDE build {catalog['build']})", **catalog_fields))

    if len(builds) > 1:
        checks.append(_check("data.consistency", WARN,
                             f"the local SDE documents describe different builds ({', '.join(str(b) for b in builds)})",
                             hint="re-download a matching set: eve-skills update-data", **fields))
    else:
        checks.append(_check("data.consistency", OK, "all local SDE documents come from one build", **fields))
    return checks


def _check_callback() -> list[dict]:
    urls = [f"http://{sso.REDIRECT_HOST}:{port}/callback" for port in sso.REDIRECT_PORTS]
    env_port = os.environ.get("EVE_SKILLS_SSO_PORT") or ""
    fields = {"redirect_urls": urls, "environment_port": env_port or None}
    checks = []
    if env_port and not env_port.isdigit():
        checks.append(_check("callback", WARN, f"EVE_SKILLS_SSO_PORT={env_port!r} is not a port number and is ignored",
                             hint=f"unset it, or set it to one of the ports registered for your app ({', '.join(map(str, sso.REDIRECT_PORTS))})",
                             **fields))
    elif env_port:
        checks.append(_check("callback", OK, f"logins use the fixed callback port {env_port} - the app registration must list "
                                             f"http://{sso.REDIRECT_HOST}:{env_port}/callback exactly", **fields))
    else:
        checks.append(_check(
            "callback", OK,
            f"a login binds the first free callback port of {' or '.join(urls)} - the app registration must list "
            f"at least {urls[0]}",
            hint="a confidential-type registration also needs its secret: eve-skills login --client-secret <secret>", **fields))
    checks.append(_check(
        "callback.manual", OK,
        "on a remote or containerised host (e.g. over ssh) the browser cannot reach this machine's localhost - "
        "open the printed URL there and paste the callback back in",
        hint="run: eve-skills login --manual"))
    return checks


def _check_roles() -> list[dict]:
    return [_check(
        "roles.corporation", OK,
        "corporation views are not verified here: ESI answers jobs --corp / inventory --corp only for a director "
        "or an Account-Manager holder with the matching role, and orders --corp only for a holder of the "
        "Accountant or Trader role - none of which can be seen offline",
        hint="probe them live with: eve-skills jobs --corp and eve-skills orders --corp "
             "(a 403 names whether the consent or the in-game role is what ESI refused)")]


def _check_history(now: float) -> dict:
    path = snapshots.history_file(create=False)
    rows = snapshots.load()
    chars = {row["char_id"] for row in rows}
    newest = max((row["ts"] for row in rows), default=None)
    fields = {"path": _display_path(path), "rows": len(rows), "characters": len(chars), "newest": _iso(newest)}
    if not rows:
        return _check("history.sp", OK, "no local SP history yet - skills --week needs roughly a week of runs", **fields)
    age_days = (now - newest) / 86400.0
    fields["age_days"] = round(age_days, 1)
    return _check("history.sp", OK,
                  f"SP history: {len(rows)} row(s) for {len(chars)} character(s), "
                  f"newest {fields['age_days']} days ago", **fields)


def _section(doc: dict, key: str) -> dict:
    """One mapping inside a JSON document; anything that is not a mapping counts as empty."""
    value = doc.get(key) if isinstance(doc, dict) else None
    return value if isinstance(value, dict) else {}


def _newest_update(entries) -> float | None:
    """The newest `updated` stamp of the watch state; junk entries are ignored, not fatal."""
    stamps = [_number(entry.get("updated")) for entry in entries if isinstance(entry, dict)]
    return max((stamp for stamp in stamps if stamp is not None), default=None)


def _check_watch_state(now: float) -> dict:
    """What the watchers remember: watched characters, order owners, and when each was last polled."""
    path = watchstate.state_file(create=False)
    doc, problem = _read_doc(path)
    fields = {"path": _display_path(path)}
    if problem == "missing":
        return _check("watch.state", OK,
                      "no watch state yet - the first eve-skills skills --watch or orders --watch run creates it",
                      **fields)
    if problem:
        # load_state() reads an unreadable file as an empty one, so nothing is blocked; what is lost
        # is the baseline, and with it every transition that happened since the last good poll.
        return _check("watch.state", WARN, f"the watch state file is {problem}",
                      hint=f"move it aside and let the next watch rebuild it: {_move_command(path)} "
                           "(transitions since the last readable poll will not be announced)", **fields)

    characters, owners = _section(doc, "characters"), _section(doc, "owners")
    corporations = sum(1 for key in owners if str(key).startswith("corp:"))
    open_orders = sum(len(_section(entry, "open")) for entry in owners.values())
    newest = _newest_update([*characters.values(), *owners.values()])
    fields.update(characters=len(characters), order_owners=len(owners),
                  character_order_owners=len(owners) - corporations, corporation_order_owners=corporations,
                  open_orders=open_orders, state_version=_number(doc.get("version")),
                  newest_update=_iso(newest),
                  age_days=None if newest is None else round((now - newest) / 86400.0, 1))
    detail = (f"watch state: {len(characters)} watched character(s), {len(owners)} order owner(s) "
              f"({corporations} of them corporations), {open_orders} open order(s) known")
    hint = None
    if characters and not owners:
        detail += "; no order owner yet, so nothing has been watched for fills or expiries"
        hint = ("start one poll of the order books: eve-skills orders --watch 1 "
                "(eve-skills skills --watch polls them too unless given --no-orders)")
    return _check("watch.state", OK, detail, hint=hint, **fields)


def _check_watch_events(now: float) -> dict:
    """What the watchers have announced: the history `eve-skills events` reads back."""
    path = watchstate.events_file(create=False)
    rows, skipped = watchstate.load_events()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    order_events = sum(count for kind, count in counts.items() if kind in ORDER_EVENT_KINDS)
    newest = max((row["ts"] for row in rows), default=None)
    fields = {"path": _display_path(path), "events": len(rows), "training_events": len(rows) - order_events,
              "order_events": order_events, "kinds": dict(sorted(counts.items())),
              "unreadable_lines": skipped, "newest": _iso(newest),
              "age_days": None if newest is None else round((now - newest) / 86400.0, 1)}
    try:
        fields["file_age_days"] = round((now - os.stat(path).st_mtime) / 86400.0, 1)
    except OSError:
        fields["file_age_days"] = None
    if not rows and not skipped:
        return _check("watch.events", OK,
                      "no recorded events yet - the first transition a watcher witnesses is appended here",
                      **fields)
    detail = (f"event history: {len(rows)} event(s) - {len(rows) - order_events} training, "
              f"{order_events} market order")
    if newest is not None:
        detail += f", newest {_duration(now - newest)} ago"
    if skipped:
        return _check("watch.events", WARN, f"{detail}; {skipped} unreadable line(s) were skipped",
                      hint=f"events.jsonl is append-only JSONL - delete the damaged line(s) from "
                           f"{_shell_path(path)} "
                           "to silence this; every command reads the rest of the history normally", **fields)
    return _check("watch.events", OK, detail, **fields)

# ---------------------------------------------------------------------------
# network probes (opt-in, unauthenticated, bounded)
# ---------------------------------------------------------------------------

def _classify(reason) -> tuple[str, str]:
    """(category, human reason) for a urllib failure reason."""
    if isinstance(reason, socket.gaierror):
        return "dns", "the host name could not be resolved"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls", "the TLS certificate could not be verified (missing CA bundle or a TLS-intercepting proxy)"
    if isinstance(reason, ssl.SSLError):
        return "tls", f"the TLS handshake failed ({type(reason).__name__})"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout", "the connection timed out"
    if isinstance(reason, ConnectionRefusedError):
        return "refused", "the connection was refused"
    if isinstance(reason, PermissionError):
        return "blocked", "outbound access was blocked by the operating system"
    if isinstance(reason, OSError):
        return "network", reason.strerror or type(reason).__name__
    return "network", str(reason)


def _request(url: str, user_agent: str, timeout: float,
             extra_headers: dict | None = None) -> tuple[bytes | None, dict]:
    """GET a public document with no credentials, so nothing secret can come back either.

    Returns (body, outcome); outcome carries reachable/category/http_status/reason so a caller can
    tell an outage from a captive portal from a dead DNS resolver, plus the response headers - where
    ESI keeps the two things no document in its body contains: when it generated the payload and how
    much it charges for reading it. Headers are probe material, never report material."""
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": user_agent, **(extra_headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            http_status = getattr(response, "status", None) or 200
            return body, {"reachable": True, "category": "ok", "http_status": http_status,
                          "reason": "reachable", "headers": getattr(response, "headers", None) or {}}
    except urllib.error.HTTPError as err:
        return None, {"reachable": True, "category": "http", "http_status": err.code,
                      "reason": f"HTTP {err.code}", "headers": getattr(err, "headers", None) or {}}
    except (TimeoutError, socket.timeout):
        return None, {"reachable": False, "category": "timeout", "http_status": None,
                      "reason": f"no answer within {timeout:g}s", "headers": {}}
    except urllib.error.URLError as err:
        category, reason = _classify(err.reason)
        return None, {"reachable": False, "category": category, "http_status": None, "reason": reason,
                      "headers": {}}
    except OSError as err:  # socket errors that escape urllib's wrapper
        category, reason = _classify(err)
        return None, {"reachable": False, "category": category, "http_status": None, "reason": reason,
                      "headers": {}}


def _probe(url: str, user_agent: str, timeout: float) -> tuple[object | None, dict]:
    """GET a public JSON document; see :func:`_request` for what the outcome says."""
    body, outcome = _request(url, user_agent, timeout)
    if not outcome["reachable"] or outcome["category"] != "ok":
        return None, outcome
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, {**outcome, "category": "protocol",
                      "reason": "answered with something that is not JSON"}
    return document, outcome


def _endpoint_cache_state(now: float) -> dict:
    """Age of the cached SSO discovery document (read-only; sso._discover trusts it for 24h)."""
    path = sso.endpoints_cache_file(create=False)
    state = {"path": path, "age_hours": None, "usable": False}
    doc, problem = _read_doc(path)
    if problem:
        return state
    fetched_at = _number(doc.get("fetched_at"))
    if fetched_at is None:
        return state
    age_hours = (now - fetched_at) / 3600.0
    state["age_hours"] = round(age_hours, 1)
    state["usable"] = age_hours < ENDPOINT_CACHE_HOURS and isinstance(doc.get("authorization_endpoint"), str)
    return state


def _check_sso_discovery(user_agent: str, timeout: float, now: float) -> dict:
    cache = _endpoint_cache_state(now)
    document, outcome = _probe(sso.WELL_KNOWN, user_agent, timeout)
    fields = {"url": sso.WELL_KNOWN, "reachable": outcome["reachable"], "category": outcome["category"],
              "http_status": outcome["http_status"], "endpoint_cache_age_hours": cache["age_hours"],
              "endpoint_cache_usable": cache["usable"]}
    if outcome["category"] == "ok" and isinstance(document, dict):
        missing = [key for key in ("authorization_endpoint", "token_endpoint") if not document.get(key)]
        if not missing:
            issuer = document.get("issuer") or sso.WELL_KNOWN.split("/.well-known")[0]
            return _check("network.sso", OK, f"EVE SSO reachable (issuer {issuer})", **fields)
        return _check("network.sso", WARN, "EVE SSO answered but the discovery document has no "
                                           f"{', '.join(missing)}", hint="re-run later; a partial document breaks login", **fields)
    if outcome["category"] == "http":
        return _check("network.sso", FAIL, f"EVE SSO discovery returned {outcome['reason']}",
                      hint="a proxy or captive portal may be intercepting HTTPS to login.eveonline.com", **fields)
    if cache["usable"]:
        remaining = max(ENDPOINT_CACHE_HOURS - (cache["age_hours"] or 0.0), 0.0)
        return _check("network.sso", WARN, f"EVE SSO unreachable ({outcome['reason']}); the cached discovery document "
                                           f"({cache['age_hours']}h old) still covers login and refresh for about {remaining:.0f}h more",
                      hint=f"refresh it once reachable: eve-skills doctor --network", **fields)
    return _check("network.sso", FAIL, f"EVE SSO unreachable ({outcome['reason']}) - login and token refresh cannot run",
                  hint="check DNS/proxy/VPN reachability for login.eveonline.com, then re-run: eve-skills doctor --network", **fields)


def _check_esi(user_agent: str, timeout: float, local_build: int | None) -> list[dict]:
    status_url = esi_mod.BASE + ESI_STATUS_PATH
    document, outcome = _probe(status_url, user_agent, timeout)
    fields = {"url": status_url, "reachable": outcome["reachable"], "category": outcome["category"],
              "http_status": outcome["http_status"]}
    if not outcome["reachable"]:
        return [_check("network.esi", FAIL, f"ESI unreachable ({outcome['reason']})",
                       hint="check DNS/proxy/VPN reachability for esi.evetech.net; every online command needs it", **fields),
                _check("network.esi_compat", SKIP, "not probed - ESI was unreachable",
                       url=esi_mod.BASE + ESI_COMPAT_PATH)]
    if outcome["category"] != "ok" or not isinstance(document, dict):
        return [_check("network.esi", FAIL, f"ESI returned {outcome['reason']}",
                       hint="esi.evetech.net/status is a public endpoint; an outage or interception is likely - "
                            "check https://status.eveonline.com before retrying", **fields),
                _check("network.esi_compat", SKIP, "not probed - ESI did not answer with JSON",
                       url=esi_mod.BASE + ESI_COMPAT_PATH)]

    server_build = _number(document.get("server_version"))
    server_build = int(server_build) if server_build is not None else None
    fields["server_build"] = server_build
    fields["server_start_time"] = document.get("start_time") if isinstance(document.get("start_time"), str) else None
    if server_build is not None and local_build is not None and server_build > local_build:
        status, detail = WARN, f"ESI reachable (live server build {server_build}); local SDE build {local_build} is behind"
        hint = "run: eve-skills update-data"
    else:
        note = f", live server build {server_build}" if server_build is not None else ""
        status, detail, hint = OK, f"ESI reachable and Tranquility answered{note}", None
    checks = [_check("network.esi", status, detail, hint=hint, **fields)]

    compat_url = esi_mod.BASE + ESI_COMPAT_PATH
    compat_doc, compat_outcome = _probe(compat_url, user_agent, timeout)
    compat_fields = {"url": compat_url, "reachable": compat_outcome["reachable"],
                     "category": compat_outcome["category"], "http_status": compat_outcome["http_status"],
                     "pinned_compatibility_date": esi_mod.COMPAT_DATE}
    dates = compat_doc.get("compatibility_dates") if isinstance(compat_doc, dict) else None
    if not isinstance(dates, list) or not dates:
        checks.append(_check("network.esi_compat", WARN, f"ESI compatibility-date list could not be read ({compat_outcome['reason']})",
                             **compat_fields))
        return checks
    newest = max(str(d) for d in dates)  # ISO dates sort lexicographically; don't trust response order
    compat_fields["newest_compatibility_date"] = newest
    if esi_mod.COMPAT_DATE in dates:
        checks.append(_check("network.esi_compat", OK,
                             f"pinned compatibility date {esi_mod.COMPAT_DATE} is still offered by ESI (newest {newest})",
                             **compat_fields))
    else:
        checks.append(_check("network.esi_compat", WARN,
                             f"ESI no longer offers the pinned compatibility date {esi_mod.COMPAT_DATE} (newest {newest})",
                             hint="upgrade eve-skills - ESI rejects unknown compatibility dates", **compat_fields))
    return checks


def _check_market(user_agent: str, timeout: float, now: float) -> dict:
    """Probe one regional order book: the only ESI endpoint that publishes a rate limit.

    It answers two questions nothing else can. How old is CCP's copy of the market - which is the
    floor under every price `eve-skills market` prints, and is stated as `Last-Modified` rather than
    guessed from when the request was made. And how much of the 15-minute budget is left, since a
    `--global` scan spends about 70 times what a single hub does."""
    url = esi_mod.BASE + MARKET_PROBE_PATH
    body, outcome = _request(url, user_agent, timeout, {"X-Compatibility-Date": esi_mod.COMPAT_DATE})
    headers = outcome["headers"]
    last_modified = esi_mod._header_epoch(headers, "Last-Modified")
    budget = esi_mod._ratelimit_tokens(headers.get("X-Ratelimit-Limit"))
    remaining = _number(headers.get("X-Ratelimit-Remaining"))
    fields = {"url": url, "reachable": outcome["reachable"], "category": outcome["category"],
              "http_status": outcome["http_status"], "region_id": MARKET_PROBE_REGION_ID,
              "type_id": MARKET_PROBE_TYPE_ID, "last_modified": _iso(last_modified),
              "age_seconds": None if last_modified is None else round(now - last_modified, 1),
              "ratelimit_budget": budget, "ratelimit_remaining": remaining}
    if not outcome["reachable"]:
        return _check("network.market", FAIL, f"the public order book is unreachable ({outcome['reason']})",
                      hint=f"check DNS/proxy/VPN reachability for esi.evetech.net; unlike the SDE data, "
                           f"eve-skills market and orders have no cached fallback", **fields)
    if outcome["http_status"] in (420, 429):
        retry = _number(headers.get("Retry-After"))
        return _check("network.market", WARN,
                      f"the public order book is rate limited ({outcome['reason']})"
                      + (f"; ESI asks for {retry:g}s before the next request" if retry else ""),
                      hint="wait for the window to reset - eve-skills backs off on its own; a --global market "
                           "scan spends far more of this budget than a single hub does", **fields)
    orders = None
    if outcome["category"] == "ok":
        try:
            orders = json.loads((body or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            orders = None
    if not isinstance(orders, list):
        reason = (outcome["reason"] if outcome["category"] != "ok"
                  else "answered with something that is not a list of orders")
        return _check("network.market", FAIL, f"the public order book {reason}",
                      hint="a TLS-intercepting proxy may be rewriting the response, or ESI changed the endpoint - "
                           "check https://status.eveonline.com before concluding the tool is at fault", **fields)
    fields["orders"] = len(orders)
    if last_modified is None:
        return _check("network.market", WARN,
                      f"the public order book is reachable ({len(orders)} order row(s)) but published no "
                      "Last-Modified, so its age cannot be verified", **fields)
    age = now - last_modified
    detail = f"public market data reachable: {len(orders)} order row(s), generated {market.format_age(age)} ago"
    if budget is not None and remaining is not None:
        detail += f"; rate-limit budget {remaining:g} of {budget:g} left"
    if age > 24 * 3600:
        return _check("network.market", WARN,
                      f"{detail} - that book is far older than ESI's five-minute refresh",
                      hint="an upstream generation problem rather than a local one; check "
                           "https://status.eveonline.com before trusting eve-skills market prices", **fields)
    return _check("network.market", OK, detail, **fields)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _guard(checks: list[dict], label: str, make) -> None:
    """Append one builder's checks; a crashing diagnostic becomes a failing line, never a crash."""
    try:
        produced = make()
    except Exception as err:  # noqa: BLE001 - doctor must always produce a report
        produced = [_check(f"diagnostics.{label}", FAIL,
                           f"this diagnostic could not finish: {type(err).__name__}: {err}")]
    checks.extend(produced if isinstance(produced, list) else [produced])


def collect(network: bool = False, timeout: float = NET_TIMEOUT, now: float | None = None) -> dict:
    """Run every diagnostic and return the report document. Read-only; never raises."""
    moment = time.time() if now is None else float(now)
    probe_timeout = max(float(timeout), 0.5)
    cfg = sso.peek_config()
    store = sso.peek_store()
    redactor = Redactor([*cfg["secret_values"], *store["secret_values"]])

    checks: list[dict] = []
    _guard(checks, "package", _check_package)
    _guard(checks, "paths", _check_paths)
    _guard(checks, "config", lambda: _check_config(cfg, store["records"]))
    _guard(checks, "tokens", lambda: _check_store(store))
    _guard(checks, "characters", lambda: _check_characters(store, moment))
    _guard(checks, "sde", lambda: _check_sde(moment))
    _guard(checks, "callback", _check_callback)
    _guard(checks, "roles", _check_roles)
    _guard(checks, "history", lambda: _check_history(moment))
    _guard(checks, "watch", lambda: [_check_watch_state(moment), _check_watch_events(moment)])

    if network:
        user_agent = esi_mod.default_user_agent(sso.load_config())
        caps = next((c for c in checks if c["name"] == "data.alpha_caps"), {})
        builds = [n for n in (_number(v) for v in [caps.get("build"), *(caps.get("builds") or [])]) if n is not None]
        local_build = int(max(builds)) if builds else None
        _guard(checks, "network.sso", lambda: _check_sso_discovery(user_agent, probe_timeout, moment))
        _guard(checks, "network.esi", lambda: _check_esi(user_agent, probe_timeout, local_build))

        # A second request to a host that just failed the first one would only fail the same way, so
        # the book probe rides on ESI's answer rather than reporting one outage three times.
        esi = next((c for c in checks if c["name"] == "network.esi"), {})
        if esi.get("category") == "ok":
            _guard(checks, "network.market", lambda: _check_market(user_agent, probe_timeout, moment))
        else:
            checks.append(_check("network.market", SKIP, "not probed - ESI itself did not answer",
                                 url=esi_mod.BASE + MARKET_PROBE_PATH))

    summary = {status: sum(1 for c in checks if c["status"] == status) for status in STATUSES}
    report = {
        "tool": "eve-skills",
        "doctor_version": REPORT_VERSION,
        "generated": _iso(moment),
        "read_only": True,
        "network": bool(network),
        "versions": {"package": __version__, "python": platform.python_version(),
                     "platform": _os_label(),
                     "executable": _display_path(sys.executable)},
        "checks": checks,
        "summary": summary,
        "exit_code": 1 if summary[FAIL] else 0,
    }
    # Two independent hygiene passes, deliberately separate: credentials are removed by value and
    # print as `[redacted]`, while any home path still spelled out collapses to `~` - a path is not
    # a secret, so it must never be masked as one. `_mask_home` is the safety net behind the
    # `_display_path` calls at every site above.
    return _mask_home(redactor.tree(report))


MARKS = {OK: "ok", WARN: "warn", FAIL: "FAIL", SKIP: "skip"}
CHARACTER_COLUMNS = ["status", "character", "id", "access token", "refresh", "consent"]


def render_text(report: dict) -> str:
    """Human-readable report; the character matrix carries identity/expiry/consent only."""
    lines = [f"eve-skills doctor - {'offline checks' if not report['network'] else 'offline checks + network probes'} (read-only)", ""]
    characters = [c for c in report["checks"] if c["name"].startswith("character.")]
    width = max((len(c["name"]) for c in report["checks"]), default=0)
    for check in report["checks"]:
        if check["name"].startswith("character."):
            continue
        lines.append(f"[{MARKS[check['status']]:<4}] {check['name'].ljust(width)}: {check['detail']}")
        if check.get("hint"):
            lines.append(f"       {' ' * width}  -> {check['hint']}")

    if characters:
        lines.append("")
        lines.append("Characters")
        rows = [[MARKS[c["status"]], c["character"], str(c["character_id"]), c["access_state"],
                 c["auto_refresh"], c["consent"]] for c in characters]
        lines.append(render.table(CHARACTER_COLUMNS, rows))
        for check in characters:
            if check["status"] != OK:
                lines.append(f"  {check['character']}: {check['detail']}")
                if check.get("hint"):
                    lines.append(f"    -> {check['hint']}")

    summary = report["summary"]
    lines.append("")
    parts = [f"{summary[OK]} ok", f"{summary[WARN]} warning(s)"]
    if summary[FAIL]:
        parts.append(f"{summary[FAIL]} problem(s)")
    if summary.get(SKIP):
        parts.append(f"{summary[SKIP]} skipped")
    lines.append(", ".join(parts))
    if not report["network"]:
        lines.append("run with --network to also probe EVE SSO discovery and public ESI endpoints")
    return "\n".join(lines)


def render_json(report: dict) -> str:
    """Machine-readable report; key order is fixed, so output diffs cleanly between runs."""
    return json.dumps(report, indent=2)


def cmd_doctor(args):
    """``eve-skills doctor`` handler - returns the process exit code (1 only for blockers)."""
    report = collect(network=getattr(args, "network", False), timeout=getattr(args, "timeout", NET_TIMEOUT))
    print(render_json(report) if args.json else render_text(report))
    return report["exit_code"]
