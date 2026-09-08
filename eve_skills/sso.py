"""EVE SSO: OAuth2 authorization-code flow with PKCE (native app) plus token storage.

Tokens and config live in ``paths.config_dir()`` - ``$XDG_CONFIG_HOME/eve-skills``
(default ``~/.config/eve-skills``) on POSIX, ``%LOCALAPPDATA%\\eve-skills\\config`` on
Windows, which is deliberately local: a roaming profile would replicate live tokens.
Register your application at https://developers.eveonline.com/applications with
redirect URL http://localhost:8635/callback and the scopes below.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import paths, storage

WELL_KNOWN = "https://login.eveonline.com/.well-known/oauth-authorization-server"
SCOPES = [
    "publicData",
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
]
# Extra consent is strictly opt-in: refresh tokens inherit the scopes they were minted
# with, so adding one requires re-running login for that character - never silently for
# everyone. Feature names here are what --scopes accepts.
OPTIONAL_SCOPES = {
    "attributes": ["esi-skills.read_skills.v1"],
    "standings": ["esi-characters.read_standings.v1"],
    "jobs": ["esi-industry.read_character_jobs.v1", "esi-industry.read_corporation_jobs.v1"],
    "assets": ["esi-assets.read_assets.v1", "esi-assets.read_corporation_assets.v1"],
    "location": ["esi-location.read_location.v1"],
    "clones": ["esi-clones.read_clones.v1", "esi-clones.read_implants.v1"],
    "orders": ["esi-markets.read_character_orders.v1"],
    # The corporation feature bundles the roles scope on purpose: it is what lets `orders --corp`
    # say "your character lacks the Accountant/Trader role" instead of surfacing a bare 403.
    "corp-orders": ["esi-markets.read_corporation_orders.v1",
                    "esi-characters.read_corporation_roles.v1"],
    # Not an asset scope, but inventory needs it: /universe/structures is the only way to name a
    # citadel or engineering site, and without it a player's holdings sit in `structure <id>`.
    "structures": ["esi-universe.read_structures.v1"],
}


def scopes_for(features) -> list[str]:
    """Expand feature names (or 'all') into ESI scope strings; unknown name = hard error."""
    out: list[str] = []
    for f in features or []:
        if f == "all":
            out.extend(s for scopes in OPTIONAL_SCOPES.values() for s in scopes)
        elif f in OPTIONAL_SCOPES:
            out.extend(OPTIONAL_SCOPES[f])
        else:
            raise RuntimeError(f"unknown scope feature '{f}' - choices: {', '.join(list(OPTIONAL_SCOPES) + ['all'])}")
    return sorted(set(out))


REDIRECT_HOST = "localhost"
REDIRECT_PORTS = (8635, 8636, 8637)
REFRESH_LEEWAY = 60  # a token this close to expiry is refreshed before it is used


def _load_json(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def config_file(create: bool = True) -> str:
    """config.json (client id/secret, user agent); readers pass create=False."""
    return os.path.join(paths.config_dir(create=create), "config.json")


def token_store_file(create: bool = True) -> str:
    """tokens.json (the multi-character token store); readers pass create=False."""
    return os.path.join(paths.config_dir(create=create), "tokens.json")


def endpoints_cache_file(create: bool = True) -> str:
    """Cached SSO discovery document written by _discover(); readers pass create=False."""
    return os.path.join(paths.cache_dir(create=create), "endpoints.json")


def load_config() -> dict:
    cfg = _load_json(config_file(create=False))
    env_id = os.environ.get("EVE_SKILLS_CLIENT_ID")
    if env_id:
        cfg["client_id"] = env_id
    return cfg


def peek_config() -> dict:
    """config.json exactly as it sits on disk - no writes, no defaults, no values.

    ``problem`` is None, "missing", "corrupt" or "unreadable". The file holds a client
    secret, so the caller gets presence flags rather than contents; ``secret_values`` is
    the one place the stored secret is handed out, and only so a diagnostic can strip it
    out of everything it prints."""
    path = config_file(create=False)
    info = {"file": path, "problem": None, "client_id": False, "client_secret": False,
            "user_agent": False, "env_client_id": bool(os.environ.get("EVE_SKILLS_CLIENT_ID")),
            "secret_values": []}
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        info["problem"] = "missing"
        return info
    except json.JSONDecodeError:
        info["problem"] = "corrupt"
        return info
    except OSError:
        info["problem"] = "unreadable"
        return info
    if not isinstance(raw, dict):
        info["problem"] = "corrupt"
        return info
    secret = raw.get("client_secret")
    info["client_id"] = bool(raw.get("client_id")) or info["env_client_id"]
    info["client_secret"] = bool(secret)
    info["user_agent"] = bool(raw.get("user_agent"))
    if isinstance(secret, str) and secret:
        info["secret_values"] = [secret]
    return info


def save_config(cfg: dict):
    """Save config.json under its own lock, merged over whatever another process saved
    meanwhile. Every writer here sets specific keys (client id/secret, user_agent); none
    intends to erase the other's, so a concurrent login must not lose unrelated settings."""
    with storage.file_lock(os.path.join(paths.config_dir(), "config.lock")):
        storage.atomic_write_json(config_file(), {**_load_json(config_file()), **cfg}, private=True)


def _store_lock():
    """Lock serialising every tokens.json read-modify-write (watch + manual commands race)."""
    return storage.file_lock(os.path.join(paths.config_dir(), "tokens.lock"))


def load_store() -> dict:
    """Token store: {"characters": {"<character_id>": record}}. Migrates the old single-character file.

    Reads need no lock (writes land atomically, so a reader always sees a whole file);
    only the one-time layout migration writes, and it does so under the store lock. The
    lock is re-entrant, so callers already inside `with _store_lock()` are safe."""
    raw = _load_json(token_store_file(create=False))
    if "access_token" in raw:  # legacy single-record layout
        with _store_lock():
            raw = _load_json(token_store_file())  # another process may have migrated it already
            if "access_token" in raw:
                if "character_id" not in raw:
                    raise RuntimeError(
                        f"{token_store_file(create=False)} holds a legacy login without character_id - "
                        "run: eve-skills login"
                    )
                raw = {"characters": {str(raw["character_id"]): raw}}
                storage.atomic_write_json(token_store_file(), raw, private=True)
    return raw


def _record_secrets(record: dict) -> list[str]:
    """The credential strings inside one stored record - for redaction, never for output."""
    return [v for v in (record.get("access_token"), record.get("refresh_token"), record.get("client_secret"))
            if isinstance(v, str) and v]


def peek_store() -> dict:
    """Token records exactly as on disk - no migration write, no lock, no refresh.

    ``problem`` is None, "missing", "unreadable", "corrupt", "legacy" (old single-character
    file that the next command migrates in place) or "legacy-unusable" (a legacy record with
    no character_id, which load_store refuses outright). ``records`` are the raw stored
    dicts: project whitelisted fields from them and scrub ``secret_values`` from anything a
    diagnostic renders - they carry live tokens."""
    path = token_store_file(create=False)
    info = {"file": path, "problem": None, "records": [], "secret_values": []}
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        info["problem"] = "missing"
        return info
    except json.JSONDecodeError:
        info["problem"] = "corrupt"
        return info
    except OSError:
        info["problem"] = "unreadable"
        return info
    if not isinstance(raw, dict):
        info["problem"] = "corrupt"
        return info
    if "access_token" in raw:  # legacy single-record layout
        info["secret_values"] = _record_secrets(raw)
        info["records"] = [raw] if "character_id" in raw else []
        info["problem"] = "legacy" if "character_id" in raw else "legacy-unusable"
        return info
    chars = raw.get("characters")
    if not isinstance(chars, dict):
        info["problem"] = "corrupt"
        return info
    records = [r for r in chars.values() if isinstance(r, dict)]
    info["records"] = sorted(records, key=lambda r: (str(r.get("character_name") or "").lower()))
    for record in records:
        info["secret_values"].extend(_record_secrets(record))
    return info


def _save_store(store: dict):
    """Publish the token store; caller holds _store_lock()."""
    storage.atomic_write_json(token_store_file(), store, private=True)


def _put_record(record: dict):
    with _store_lock():
        store = load_store()  # re-read under the lock: another process may have rotated tokens
        store.setdefault("characters", {})[str(record["character_id"])] = record
        _save_store(store)


def list_characters() -> list[dict]:
    """Stored token records, sorted by character name."""
    chars = load_store().get("characters", {})
    return sorted(chars.values(), key=lambda r: (r.get("character_name") or "").lower())


def clear_tokens(character_id: int | None = None):
    """Remove one character's tokens, or all of them when character_id is None.

    Both branches hold the store lock, so a logout cannot slip between another process's
    read and write and resurrect (or swallow) a record."""
    with _store_lock():
        if character_id is None:
            try:
                os.remove(token_store_file())
            except FileNotFoundError:
                pass
            return
        store = load_store()
        if not store.get("characters", {}).pop(str(character_id), None):
            raise RuntimeError(f"no stored login for character id {character_id}")
        _save_store(store)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_jwt(token: str) -> dict:
    """Decode JWT claims without signature verification (local desktop tool trusts its own token)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _discover() -> dict:
    cache_path = endpoints_cache_file()
    cached = _load_json(cache_path)
    if cached.get("fetched_at", 0) > time.time() - 86400 and "authorization_endpoint" in cached:
        return cached
    req = urllib.request.Request(WELL_KNOWN, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.loads(resp.read().decode())
    except urllib.error.URLError as err:
        raise RuntimeError(f"network error reaching EVE SSO discovery: {err.reason}") from None
    doc["fetched_at"] = time.time()
    # No lock: discovery is idempotent, and the atomic replace means concurrent fetchers
    # each publish a whole document instead of interleaving into a half-written cache.
    storage.atomic_write_json(cache_path, doc)
    return doc


def _post_form(url: str, fields: dict, basic_auth: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if basic_auth:
        headers["Authorization"] = "Basic " + base64.b64encode(basic_auth.encode()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"SSO token request failed (HTTP {err.code}): {detail}") from None
    except urllib.error.URLError as err:
        raise RuntimeError(f"network error reaching EVE SSO: {err.reason}") from None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.server.captured = params  # type: ignore[attr-defined]
        body = (
            b"<html><body style='font-family:sans-serif'><h2>eve-skills</h2>"
            b"<p>Login complete. You can close this tab and return to the terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


def login(client_id: str | None = None, client_secret: str | None = None, port: int | None = None,
          scopes=None, manual: bool = False) -> dict:
    """Run PKCE login and persist tokens; manual mode accepts a pasted callback URL."""
    extra = scopes_for(scopes)
    requested_scopes = SCOPES + [scope for scope in extra if scope not in SCOPES]
    cfg = load_config()
    client_id = client_id or cfg.get("client_id")
    if not client_id:
        raise RuntimeError(
            "No client_id configured.\n"
            "1. Register an application at https://developers.eveonline.com/applications\n"
            f"   - redirect URL: http://{REDIRECT_HOST}:{port or os.environ.get('EVE_SKILLS_SSO_PORT') or REDIRECT_PORTS[0]}/callback\n"
            f"   - scopes: {' '.join(requested_scopes)}\n"
            "2. Run: eve-skills login --client-id <your client id>\n"
        )
    client_secret = client_secret or cfg.get("client_secret")

    endpoints = _discover()
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)

    server = None
    env_port = os.environ.get("EVE_SKILLS_SSO_PORT")
    configured_port = int(port or env_port) if port or (env_port and env_port.isdigit()) else None
    if manual:
        redirect_port = configured_port or REDIRECT_PORTS[0]
    else:
        ports = (configured_port,) if configured_port else REDIRECT_PORTS
        for p in ports:
            try:
                server = http.server.HTTPServer(("127.0.0.1", p), _CallbackHandler)
                break
            except OSError:
                continue
        if server is None:
            raise RuntimeError(f"could not bind callback port(s): {', '.join(map(str, ports))}")
        redirect_port = server.server_address[1]

    redirect_uri = f"http://{REDIRECT_HOST}:{redirect_port}/callback"
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "scope": " ".join(requested_scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    auth_url = f"{endpoints['authorization_endpoint']}?{query}"
    print("Opening browser for EVE Online login...")
    print(f"If nothing opens, visit manually:\n  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    if manual:
        print("After EVE redirects to localhost, copy the full URL from the browser address bar.")
        try:
            callback_url = input("Paste callback URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("login cancelled before the callback URL was pasted") from None
        expected = urllib.parse.urlparse(redirect_uri)
        received = urllib.parse.urlparse(callback_url)
        try:
            callback_matches = (
                received.scheme == expected.scheme
                and received.hostname == expected.hostname
                and received.port == expected.port
                and received.path == expected.path
            )
        except ValueError:
            callback_matches = False
        if not callback_matches:
            raise RuntimeError(f"callback URL must start with {redirect_uri}?")
        captured = dict(urllib.parse.parse_qsl(received.query))
    else:
        deadline = time.time() + 300
        server.timeout = 1.0
        while time.time() < deadline and not getattr(server, "captured", None):
            server.handle_request()
        server.server_close()
        captured = getattr(server, "captured", None)
        if not captured:
            raise RuntimeError("timed out (5 min) waiting for the browser callback")
    if captured.get("state") != state:
        raise RuntimeError("login aborted: state mismatch (possible CSRF or stale tab)")
    if "error" in captured:
        raise RuntimeError(f"SSO returned an error: {captured.get('error')} {captured.get('error_description', '')}".strip())

    fields = {
        "grant_type": "authorization_code",
        "code": captured["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    basic = None
    if client_secret:
        # Confidential client: credentials go in the Basic header only;
        # CCP rejects them duplicated in the body.
        basic = f"{client_id}:{client_secret}"
    else:
        fields["client_id"] = client_id
    tok = _post_form(endpoints["token_endpoint"], fields, basic_auth=basic)

    claims = decode_jwt(tok["access_token"])
    sub = claims.get("sub", "")
    if not sub.startswith("CHARACTER:EVE:"):
        raise RuntimeError(f"unexpected token subject: {sub!r}")
    record = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 1199)),
        "scopes": claims.get("scp", []) if isinstance(claims.get("scp"), list) else [claims.get("scp", "")],
        "character_id": int(sub.rsplit(":", 1)[1]),
        "character_name": claims.get("name"),
    }
    _put_record(record)
    cfg["client_id"] = client_id
    if client_secret:
        cfg["client_secret"] = client_secret
    save_config(cfg)
    return record


def _stored_record(character_id: int) -> dict:
    """The record on disk for one character; caller holds _store_lock()."""
    record = load_store().get("characters", {}).get(str(character_id))
    if not record:
        raise RuntimeError(f"no stored login for character id {character_id}")
    return record


def _superseded(stored: dict, stale: dict) -> bool:
    """True when the store no longer holds exactly the record we meant to refresh."""
    return any(stored.get(key) != stale.get(key) for key in ("access_token", "refresh_token", "expires_at"))


def refresh(record: dict) -> dict:
    """Rotate one character's token pair and persist it.

    The store lock is held across the token request on purpose: refresh tokens rotate,
    so a second CLI/watch process must never POST the same (about to be consumed) one.
    Whoever loses the race re-reads under the lock and returns the rotated pair instead -
    one shared expiring token costs exactly one refresh request. If the winner's request
    failed nothing changed on disk, so the loser refreshes normally."""
    with _store_lock():
        current = _stored_record(record["character_id"])
        if _superseded(current, record):
            return current
        endpoints = _discover()
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": current["refresh_token"],
        }
        basic = None
        if current.get("client_secret"):
            basic = f"{current['client_id']}:{current['client_secret']}"
        else:
            fields["client_id"] = current["client_id"]
        tok = _post_form(endpoints["token_endpoint"], fields, basic_auth=basic)
        current["access_token"] = tok["access_token"]
        current["expires_at"] = time.time() + int(tok.get("expires_in", 1199))
        if tok.get("refresh_token"):
            current["refresh_token"] = tok["refresh_token"]
        store = load_store()  # unchanged: we have held the lock across the request
        store.setdefault("characters", {})[str(current["character_id"])] = current
        _save_store(store)
        return current


def get_access_token(character_id: int | None = None, auto_refresh: bool = True) -> dict:
    """Token record for one character, refreshed when closer than REFRESH_LEEWAY to expiry.

    With no character_id: the only stored character, or an error listing the choices.
    Concurrent callers of this function share one token request per expiring character."""
    chars = load_store().get("characters", {})
    if not chars:
        raise RuntimeError("not logged in — run: eve-skills login")
    if character_id is not None:
        record = chars.get(str(character_id))
        if not record:
            raise RuntimeError(f"no stored login for character id {character_id}")
    elif len(chars) == 1:
        record = next(iter(chars.values()))
    else:
        known = ", ".join(r.get("character_name") or cid for cid, r in sorted(chars.items()))
        raise RuntimeError(f"multiple characters stored, pick one with --char: {known}")
    if record["expires_at"] - REFRESH_LEEWAY < time.time():
        if not (auto_refresh and record.get("refresh_token")):
            raise RuntimeError("session expired — run: eve-skills login")
        record = refresh(record)
    return record


def has_scope(record: dict, scope: str) -> bool:
    """Whether the stored consent for this character includes a scope."""
    return scope in (record.get("scopes") or [])


def has_feature(record: dict, feature: str) -> bool:
    """True when the stored consent covers every scope of an optional feature."""
    granted = set(record.get("scopes") or [])
    return all(s in granted for s in OPTIONAL_SCOPES[feature])


def resolve_character(value: str) -> int:
    """Character id from an exact id, exact name, or unique name substring."""
    chars = load_store().get("characters", {})
    if value.isdigit() and value in chars:
        return int(value)
    matches = [int(cid) for cid, r in chars.items() if (r.get("character_name") or "").lower() == value.lower()]
    if not matches:
        needle = value.lower()
        matches = [int(cid) for cid, r in chars.items() if needle in (r.get("character_name") or "").lower()]
    if len(matches) != 1:
        known = ", ".join(r.get("character_name") or cid for cid, r in sorted(chars.items())) or "(none)"
        raise RuntimeError(f"character {value!r} is {'ambiguous' if matches else 'unknown'}; known: {known}")
    return matches[0]
