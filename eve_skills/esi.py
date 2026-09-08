"""Minimal ESI (EVE Swagger Interface) HTTP client. Standard library only."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import __version__

BASE = "https://esi.evetech.net"
# Pinned response-shape version; see GET /meta/compatibility-dates.
COMPAT_DATE = "2026-08-18"

# Only /markets/{region_id}/orders carries X-Ratelimit-* (verified 2026-09-07: group
# `market-order`, X-Ratelimit-Limit `12000/15m`, 2 tokens per 2XX, 1 per 304, 5 per 4XX).
# That window has no reset header, so instead of parking until it rolls over we slow to about
# its refill rate once it is nearly spent: a short pause per response is far cheaper than the
# 429 that burning the window would cost (a full Retry-After each time).
RATELIMIT_LOW_FRACTION = 0.10
RATELIMIT_HOLD_SECONDS = 1.0
WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def default_user_agent(cfg: dict) -> str:
    return cfg.get("user_agent") or f"eve-skills/{__version__} (EVE tools; contact unset - set user_agent in config.json)"


class EsiError(RuntimeError):
    pass


def _read_name_cache(path: str) -> dict[int, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return {int(k): v for k, v in json.load(fh).items()}
    except (FileNotFoundError, ValueError):
        return {}


def resolve_names(client: "Esi", ids: set[int], cache_dir: str | None = None) -> dict[int, str]:
    """Resolve any universe ids (skills, stations, structures, systems, item types) to names.
    Cached on disk - these never change."""
    from . import paths, storage

    path = os.path.join(cache_dir or paths.cache_dir(), "names.json")
    cache = _read_name_cache(path)
    missing = sorted(i for i in ids if i not in cache)
    resolved: dict[int, str] = {}
    for i in range(0, len(missing), 1000):
        chunk = missing[i:i + 1000]
        try:
            for entry in client.post("/universe/names", chunk):
                resolved[entry["id"]] = entry["name"]
        except EsiError:
            pass  # private citadels etc.: unresolved ids stay out of the cache
    if resolved:
        # Merge under the lock: another process may have resolved other ids since we read
        # the file, and publishing our own view would silently drop its freshly named ids.
        with storage.file_lock(os.path.join(os.path.dirname(path) or ".", "names.lock")):
            cache = {**_read_name_cache(path), **resolved}
            storage.atomic_write(path, json.dumps(cache))
    return {i: cache[i] for i in ids if i in cache}


class _NotModified(EsiError):
    """Internal signal for a 304 conditional-GET response. An EsiError because a 304 that reaches
    a caller (one asked for with `cache=False`) is a failed request, and the CLI only prints clean
    messages for those."""

    def __init__(self, headers):
        super().__init__("HTTP 304 not modified")
        self.headers = headers


class AuthError(EsiError):
    """401/403 from ESI: token missing, expired or lacking scope."""


def _error_message(body: bytes) -> str:
    try:
        return json.loads(body.decode("utf-8", "replace")).get("error", "?")
    except Exception:
        text = body.decode("utf-8", "replace").strip()
        return text[:200] or "(empty response)"


def _header_epoch(headers, name: str) -> float | None:
    """Epoch seconds of an HTTP-date header (`Expires`, `Last-Modified`); None when absent or broken."""
    raw = headers.get(name)
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        return None


def _header_expiry(headers) -> float | None:
    expires = _header_epoch(headers, "Expires")
    if expires is not None:
        return expires
    for part in (headers.get("Cache-Control") or "").split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                return time.time() + int(part[8:])
            except ValueError:
                pass
    return None


def _page_count(headers) -> int:
    try:
        return max(int(headers.get("X-Pages", "1")), 1)
    except (TypeError, ValueError):
        return 1


@dataclass(frozen=True)
class Meta:
    """Cache metadata of one ESI response; every field may be None when absent.

    `last_modified` is when CCP generated the payload - the number a freshness line is about,
    and deliberately not the moment we fetched: a cache hit or a 304 revalidation hands back
    data that is exactly as old as the response it reuses.
    """

    expires: float | None = None        # epoch, from Expires (or Cache-Control: max-age)
    last_modified: float | None = None  # epoch, from Last-Modified: when CCP generated the payload
    etag: str | None = None
    pages: int = 1


def fold_meta(metas: Iterable[Meta]) -> Meta:
    """Collapse a fan-out's Metas into the one that describes the whole batch.

    The oldest `last_modified` wins: after merging 70 regional books, quoting the freshest of
    them would present stale regions as current. `expires` takes the earliest TTL so a folded
    result is never cached longer than its shortest-lived member allowed."""
    metas = list(metas)
    if not metas:
        return Meta()
    stamps = [m.last_modified for m in metas if m.last_modified is not None]
    expiries = [m.expires for m in metas if m.expires is not None]
    etags = {m.etag for m in metas if m.etag}
    return Meta(
        expires=min(expiries) if expiries else None,
        last_modified=min(stamps) if stamps else None,
        # One ETag only means something when every response agreed on it.
        etag=etags.pop() if len(etags) == 1 else None,
        pages=max(m.pages for m in metas),
    )


def _meta_of(headers) -> Meta:
    return Meta(
        expires=_header_expiry(headers),
        last_modified=_header_epoch(headers, "Last-Modified"),
        etag=headers.get("ETag"),
        pages=_page_count(headers),
    )


def _ratelimit_tokens(raw: str | None) -> int | None:
    """Token budget of a rate-limit window: `12000/15m` -> 12000 (a bare `12000` works too)."""
    head = (raw or "").partition("/")[0].strip()
    return int(head) if head.isdigit() else None


class Esi:
    def __init__(self, user_agent: str, compat_date: str = COMPAT_DATE):
        self.user_agent = user_agent
        self.compat_date = compat_date
        # (server_now - local_now) in seconds, tracked from response Date headers.
        self.server_offset = 0.0
        # In-process GET cache keyed by token+path; TTLs come only from server headers.
        self._cache: dict[str, dict] = {}
        # ESI error window: hold requests back until this epoch time when near the limit.
        self._blocked_until = 0.0
        # One lock for all of it. `get_many` runs requests on several threads, and none of
        # "read a cache entry, maybe replace it", "max the backoff deadline" or "publish a clock
        # offset" is atomic on its own: unlocked, one thread's fresh entry can vanish under
        # another's, and a deadline can be lowered by a slower sibling. Held only for these
        # bookkeeping steps - never across a network wait, which would serialise the fan-out.
        self._lock = threading.Lock()

    def get(self, path: str, token: str | None = None, cache: bool = True):
        """GET with server-driven caching (Expires TTL + ETag revalidation)."""
        value, _ = self.get_meta(path, token=token, cache=cache)
        return value

    def get_meta(self, path: str, token: str | None = None, cache: bool = True) -> tuple[object, Meta]:
        """`get` plus the response's Meta.

        The cache stores the Meta together with the payload, because a hit and a 304
        revalidation both hand back bytes ESI generated earlier: reporting `now` for them would
        present an hours-old order book as instantaneous. A 304 keeps the stored Meta verbatim -
        same bytes, same generation.
        """
        if not cache:
            value, headers = self._request("GET", path, token=token)
            return value, _meta_of(headers)
        key = hashlib.sha1(f"{token or 'public'}\n{path}".encode()).hexdigest()
        with self._lock:
            entry = self._cache.get(key)
        if entry and entry["expires"] > time.time():
            return entry["value"], entry["meta"]
        extra = {"If-None-Match": entry["etag"]} if entry and entry.get("etag") else None
        try:
            value, headers = self._request("GET", path, token=token, extra=extra)
        except _NotModified as nm:
            # ESI always sends Expires alongside a 304; fall back to revalidating next call.
            if entry is None:  # cannot happen against real ESI (we only get 304 for our own ETag)
                raise EsiError(f"HTTP 304 on {path} with nothing cached") from None
            with self._lock:
                entry["expires"] = _header_expiry(nm.headers) or 0.0
            return entry["value"], entry["meta"]
        meta = _meta_of(headers)
        with self._lock:
            if meta.expires is not None:
                self._cache[key] = {"value": value, "expires": meta.expires, "etag": meta.etag, "meta": meta}
            elif entry:
                # The response says it is not cacheable; drop the stale entry rather than keep serving it.
                self._cache.pop(key, None)
        return value, meta

    def post(self, path: str, payload, token: str | None = None):
        return self._request("POST", path, body=payload, token=token)[0]

    def get_all(self, path: str, token: str | None = None, max_pages: int = 100) -> list:
        """Concatenated pages of a paginated GET (follows the X-Pages header)."""
        rows, _ = self.get_all_meta(path, token=token, max_pages=max_pages)
        return rows

    def get_all_meta(self, path: str, token: str | None = None, max_pages: int = 100) -> tuple[list, Meta]:
        """`get_all` through the cache-aware path, plus the Meta describing what was read.

        The pages are separate responses and can straddle one of ESI's refreshes, so the
        freshness of a collection is that of its oldest page - never the last one fetched."""
        sep = "&" if "?" in path else "?"
        out: list = []
        metas: list[Meta] = []
        page, pages = 1, 1
        while page <= min(pages, max_pages):
            value, meta = self.get_meta(f"{path}{sep}page={page}", token=token)
            out.extend(value)
            metas.append(meta)
            pages = meta.pages
            page += 1
        return out, fold_meta(metas)

    def get_many(self, paths: Sequence[str], token: str | None = None, workers: int = 8,
                 paginated: bool = False) -> dict[str, object]:
        """Concurrently GET every path; the value is the payload or that path's Exception.

        A whole-cluster market scan touches ~70 regions and must not lose the other 69 because
        one of them 500s, so failures come back as data instead of aborting the pool. Every path
        still goes through the cache-aware GET (pages concatenated when `paginated`), which is
        also what makes a repeated scan cheap. Duplicate paths are fetched once.
        """
        return {path: (item if isinstance(item, Exception) else item[0])
                for path, item in self.get_many_meta(paths, token=token, workers=workers,
                                                     paginated=paginated).items()}

    def get_many_meta(self, paths: Sequence[str], token: str | None = None, workers: int = 8,
                      paginated: bool = False) -> dict[str, tuple[object, Meta] | Exception]:
        """`get_many` keeping each path's Meta: `(payload, Meta)`, or the Exception.

        The caller folds the Metas (`fold_meta`) because only the caller knows what the batch
        means - a cluster quote is as old as its oldest region."""
        ordered = list(dict.fromkeys(paths))
        if not ordered:
            return {}
        fetch = self.get_all_meta if paginated else self.get_meta
        results: dict[str, tuple[object, Meta] | Exception] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(ordered)))) as pool:
            futures = {pool.submit(fetch, path, token): path for path in ordered}
            for future, path in futures.items():
                try:
                    results[path] = future.result()
                except Exception as err:  # noqa: BLE001 - one bad region must not sink the scan
                    results[path] = err
        return results

    def _request(self, method: str, path: str, body=None, token: str | None = None, extra=None):
        with self._lock:
            wait = self._blocked_until - time.time()
        if wait > 0:
            # Always slept outside the lock: parking here would stall the other workers, and
            # a fan-out is exactly when a backoff has to let siblings drain their own requests.
            time.sleep(min(wait, 120))
        url = BASE + path
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "X-Compatibility-Date": self.compat_date,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        attempts = 3
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._note_server_time(resp.headers.get("Date"))
                    self._note_error_limit(resp.headers)
                    self._note_rate_limit(resp.headers)
                    return json.loads(resp.read().decode()), resp.headers
            except urllib.error.HTTPError as err:
                self._note_error_limit(err.headers)
                self._note_rate_limit(err.headers)
                if err.code == 304:
                    raise _NotModified(err.headers) from None
                if err.code in (420, 429, 502, 503) and attempt < attempts - 1:
                    retry_after = err.headers.get("Retry-After")
                    time.sleep(min(int(retry_after) if retry_after and retry_after.isdigit() else 2 * (attempt + 1), 30))
                    continue
                msg = _error_message(err.read())
                if err.code in (401, 403):
                    raise AuthError(f"HTTP {err.code}: {msg}") from None
                raise EsiError(f"HTTP {err.code} on {path}: {msg}") from None
            except urllib.error.URLError as err:
                raise EsiError(f"network error calling {path}: {err.reason}") from None

    def _note_server_time(self, date_header: str | None):
        """Track clock skew so queue math survives a skewed system clock."""
        if not date_header:
            return
        try:
            server_now = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            return
        with self._lock:
            self.server_offset = (server_now - datetime.now(timezone.utc)).total_seconds()

    def _hold_until(self, epoch: float):
        """Push the shared pause deadline out, never pull it back in."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, epoch)

    def _note_error_limit(self, headers):
        """Back off before ESI's error window bites: X-ESI-Error-Limit-Remain/Reset."""
        try:
            remain = int(headers.get("X-ESI-Error-Limit-Remain", "100"))
            reset = int(headers.get("X-ESI-Error-Limit-Reset", "0"))
        except (TypeError, ValueError):
            return
        if remain < 25 and reset > 0:
            self._hold_until(time.time() + reset)

    def _note_rate_limit(self, headers):
        """Throttle while a token window is nearly spent: X-Ratelimit-Remaining vs -Limit.

        The error window above says when to stop; this one only exists on the market-order
        group and carries no reset timestamp, so there is nothing to sleep *until*. Instead a
        near-empty window adds one short pause per response, which costs far less than the 429
        (and its Retry-After) that spending the last tokens outright would produce."""
        budget = _ratelimit_tokens(headers.get("X-Ratelimit-Limit"))
        if not budget:
            return
        try:
            remaining = int(headers.get("X-Ratelimit-Remaining", str(budget)))
        except (TypeError, ValueError):
            return
        if remaining < budget * RATELIMIT_LOW_FRACTION:
            self._hold_until(time.time() + RATELIMIT_HOLD_SECONDS)

    def now(self) -> datetime:
        """Current time as ESI servers understand it."""
        return datetime.now(timezone.utc) + timedelta(seconds=self.server_offset)
