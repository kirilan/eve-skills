"""`dashboard`: a local web page with the operation's profitability, industry jobs and market orders.

`serve` runs a small HTTP server (standard library only) that rebuilds its document every
`--refresh` minutes: a ledger sync when the last one is that old, a replay of the ledger, and the
live `industry status --corp`, `orders --check` and `colonies --detail` reports. In between, every
`--orders-refresh` minutes, only `orders --check` is re-run and its section replaced. The page polls
the document and redraws.
`build` writes one document as JSON and exits - the same data, for a script or a cron job.

The server is read-only: it answers GET for the page and the document, and POST /api/refresh only
asks the refresh thread to run early (at most once per `MIN_MANUAL_REFRESH_SECONDS`). Nothing it serves
needs a login, so bind it to a network you trust.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import cmd_ledger, dashboard, esi as esi_mod, ledger_db, ledger_sync, paths, price_watch, stockpile, storage

DEFAULT_PORT = 8765
DEFAULT_REFRESH_MINUTES = 60
# ESI caches a character's order book for 20 minutes, so between full rebuilds the orders panel is
# re-read on that cadence: an hourly rebuild alone showed reprices up to 80 minutes late.
DEFAULT_ORDERS_REFRESH_MINUTES = 20
MIN_MANUAL_REFRESH_SECONDS = 300
# A live report that takes longer than this has hung on ESI; the page shows the last good one.
LIVE_TIMEOUT_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: str) -> None:
    print(f"{_iso(_now())} dashboard: {message}", file=sys.stderr, flush=True)


def _self_command() -> list[str]:
    """How to start this tool again in a child process.

    From an installed package that is the interpreter plus `-m eve_skills`. In the frozen Windows
    build (PyInstaller) `sys.executable` is eve-skills.exe itself, which has no `-m`: it would read
    `-m` as an unknown flag and every live section would fail."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "eve_skills"]


def _live(*args: str) -> tuple[dict | None, str | None]:
    """Run one of our own reports with --json in a child process: (document, error)."""
    command = [*_self_command(), *args, "--json"]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=LIVE_TIMEOUT_SECONDS,
                             encoding="utf-8")
    except subprocess.TimeoutExpired:
        return None, f"eve-skills {' '.join(args)} timed out after {LIVE_TIMEOUT_SECONDS} s"
    if run.returncode or not run.stdout.strip():
        tail = (run.stderr or "").strip().splitlines()[-1:] or [f"exit {run.returncode}"]
        return None, f"eve-skills {' '.join(args)}: {tail[0]}"
    try:
        return json.loads(run.stdout), None
    except json.JSONDecodeError as err:
        return None, f"eve-skills {' '.join(args)}: unreadable JSON ({err})"


def _status_plan() -> list[tuple[tuple[str, ...], set[str] | None]]:
    """The `industry status` reads that cover every site: (arguments, characters kept or None for all).

    Without an operation file this is the corporation snapshot it always was. With one, a corp site is
    read through its first reader and a personal site from the characters' own snapshots, so an
    operation split across a high-sec corporation and a null-sec structure shows on one page."""
    from . import operation
    op = operation.load()
    if not op.sites:
        return [(("industry", "status", "--corp"), None)]
    plan, personal = [], set()
    for site in op.sites.values():
        if site.owner == "corp":
            args = ("industry", "status", "--corp") + (("--char", site.readers[0]) if site.readers else ())
            if (args, None) not in plan:
                plan.append((args, None))
        else:
            personal.update(name.casefold() for name in site.characters)
    if personal:
        plan.append((("industry", "status"), personal))
    return plan


def _merged_status(results) -> tuple[dict | None, str | None]:
    """One status document out of several reads; owners outside a personal site are dropped."""
    owners, warnings, errors = [], [], []
    for (doc, error), keep in results:
        if doc is None:
            errors.append(error)
            continue
        warnings += doc.get("warnings") or []
        for owner in doc.get("owners") or []:
            if keep is None or str(owner.get("owner_name") or "").casefold() in keep:
                owners.append(owner)
    if not owners:
        return None, "; ".join(e for e in errors if e) or "no industry status"
    return {"owners": owners, "warnings": warnings}, ("; ".join(e for e in errors if e) or None)


def _market_volume(conn, type_ids) -> dict[int, float]:
    """Median daily units traded in Jita's region over the last 30 stored days, per type that has history."""
    out = {}
    for type_id in type_ids:
        rows = ledger_db.market_history(conn, price_watch.HUB.region_id, type_id)[-30:]
        volumes = [r["volume"] for r in rows if r.get("volume") is not None]
        if volumes:
            out[type_id] = float(statistics.median(volumes))
    return out


def _orders_args(orders_char: str | None) -> list[str]:
    return ["orders", "--check"] + (["--char", orders_char] if orders_char else [])


def build_document(*, since: str | None, live: bool = True, refresh_minutes: int | None = None,
                   orders_char: str | None = None, record: bool = False) -> dict:
    """Replay the ledger and (unless `live` is off) read jobs, orders and stock from ESI. With `record`
    the live stock is also stored in the ledger as a snapshot, which is what builds the stock history."""
    now = _now()
    client = cmd_ledger._client()
    with contextlib.closing(cmd_ledger._open()) as conn:
        book, header, _recipes, _invention = cmd_ledger._state(conn)
        start = since or header["cutover"] or now.date().isoformat()
        # The planetary panel counts from the first planetary charge or poll, never from before `start`.
        planetary = {key: cmd_ledger.planetary_document(conn, book, client if live else None, day)
                     for key, day in dashboard.range_starts(now.date(), start).items()}
    status = orders = inventory = colonies = None
    status_error = orders_error = inventory_error = colonies_error = None
    if live:
        orders_args = _orders_args(orders_char)
        plan = _status_plan()
        with ThreadPoolExecutor(max_workers=3 + len(plan)) as pool:
            jobs = {key: pool.submit(_live, *cmd) for key, cmd in (
                ("orders", orders_args),
                ("inventory", ("ledger", "inventory")), ("colonies", ("colonies", "--detail")))}
            reads = [(pool.submit(_live, *args), keep) for args, keep in plan]
            status, status_error = _merged_status([(future.result(), keep) for future, keep in reads])
        orders, orders_error = jobs["orders"].result()
        inventory, inventory_error = jobs["inventory"].result()
        colonies, colonies_error = jobs["colonies"].result()
    else:
        status_error = orders_error = inventory_error = colonies_error = "live reports off (--no-live)"
    colony_planets = [c.get("planet_id") for ch in (colonies or {}).get("characters") or []
                      for c in ch.get("colonies") or []]
    planet_names = {pid: place.get("name") for pid, place in
                    cmd_ledger.planet_names(client if live else None, colony_planets).items() if place.get("name")}
    items = (inventory or {}).get("items") or []
    with contextlib.closing(ledger_db.connect()) as conn:
        if record and items:
            with conn:
                ledger_db.insert_stock_snapshot(conn, _iso(now), stockpile.stock_rows(items))
        snapshots = ledger_db.stock_snapshots(
            conn, since=(now.date() - timedelta(days=stockpile.HISTORY_DAYS - 1)).isoformat())
        volume = _market_volume(conn, book.produced_products)
    stock = stockpile.analyse(book, items, today=now.date(), market_volume=volume,
                              blueprints=[bp for owner in (status or {}).get("owners") or []
                                          for bp in owner.get("blueprints") or []],
                              snapshots=snapshots)
    stock["error"] = inventory_error
    stock["as_of"] = (inventory or {}).get("prices_as_of")
    return dashboard.document(book, header, since=start, now=_iso(now), today=now.date(),
                              names=lambda ids: cmd_ledger._names(client, ids),
                              status=status, status_error=status_error, orders=orders, orders_error=orders_error,
                              stock=stock, refresh_minutes=refresh_minutes, planetary=planetary,
                              colonies=colonies, colonies_error=colonies_error, planet_names=planet_names)


def _page() -> bytes:
    return resources.files("eve_skills").joinpath("data/dashboard.html").read_bytes()


def _cache_path() -> str:
    return os.path.join(paths.cache_dir(), "dashboard.json")


class _State:
    """The served document and the refresh thread's bookkeeping, shared with the request handlers."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.body: bytes | None = None
        self.refreshing = False
        self.last_attempt: float | None = None   # monotonic; None until the first refresh starts
        self.last_error: str | None = None
        self.next_refresh: str | None = None
        try:
            with open(_cache_path(), "rb") as fh:
                self.body = fh.read()       # the last document, so a restart serves at once
        except OSError:
            pass

    def status(self) -> dict:
        with self.lock:
            return {"refreshing": self.refreshing, "last_error": self.last_error,
                    "next_refresh": self.next_refresh, "has_document": self.body is not None}

    def refresh(self) -> None:
        args = self.args
        with self.lock:
            self.refreshing = True
            self.last_attempt = time.monotonic()
        started = time.monotonic()
        error = None
        try:
            if not args.no_sync:
                line, problems = ledger_sync.sync_if_due(cmd_ledger._client(),
                                                         interval=max(60, args.refresh * 60 - 300))
                for message in ([line] if line else []) + problems:
                    _log(message)
            doc = build_document(since=args.since, live=not args.no_live, refresh_minutes=args.refresh,
                                 orders_char=args.orders_char, record=True)
            body = json.dumps(doc).encode("utf-8")
            with contextlib.suppress(OSError):
                storage.atomic_write(_cache_path(), body)
            with self.lock:
                self.body = body
            for part in ("jobs", "orders", "stock"):
                if doc[part].get("error"):
                    _log(f"{part}: {doc[part]['error']}")
            if doc["planetary"]["colonies"].get("error"):
                _log(f"colonies: {doc['planetary']['colonies']['error']}")
            _log(f"document rebuilt in {time.monotonic() - started:.0f} s")
        except (RuntimeError, OSError, ValueError, sqlite3.Error, esi_mod.EsiError) as err:
            error = str(err)
            _log(f"refresh failed: {err}")
        with self.lock:
            self.refreshing = False
            self.last_error = error
            self.next_refresh = _iso(_now() + timedelta(minutes=args.refresh))

    def refresh_orders(self) -> None:
        """Re-run only `orders --check` and swap its section into the served document.

        A failed read keeps the previous rows, whose `esi_as_of` then says how old they are. It does
        not mark the server as refreshing: a manual refresh asked for meanwhile still runs right after."""
        with self.lock:
            if self.body is None:
                return
        started = time.monotonic()
        orders, error = _live(*_orders_args(self.args.orders_char))
        if orders is None:
            _log(f"orders: {error}")
            return
        try:
            with self.lock:
                doc = json.loads(self.body)
                doc["orders"] = dashboard.orders_section(orders)
                self.body = body = json.dumps(doc).encode("utf-8")
        except ValueError as err:
            _log(f"orders refresh failed: {err}")
            return
        with contextlib.suppress(OSError):
            storage.atomic_write(_cache_path(), body)
        _log(f"orders refreshed in {time.monotonic() - started:.0f} s")

    def loop(self) -> None:
        """Full rebuilds every --refresh minutes (or on request), orders alone in between."""
        full_every = self.args.refresh * 60
        orders_every = (getattr(self.args, "orders_refresh", 0) or 0) * 60
        if orders_every >= full_every:
            orders_every = 0
        while True:
            self.refresh()
            next_full = time.monotonic() + full_every
            while True:
                now = time.monotonic()
                due = min(next_full, now + orders_every) if orders_every else next_full
                if self.wake.wait(max(0.0, due - now)):
                    self.wake.clear()
                    break           # a manual refresh: rebuild everything now
                if time.monotonic() >= next_full - 1:
                    break
                self.refresh_orders()

    def request_refresh(self) -> bool:
        with self.lock:
            recent = (self.last_attempt is not None
                      and time.monotonic() - self.last_attempt < MIN_MANUAL_REFRESH_SECONDS)
            if self.refreshing or recent:
                return False
        self.wake.set()
        return True


def _handler(state: _State, page: bytes):
    class Handler(BaseHTTPRequestHandler):
        server_version = "eve-skills-dashboard"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self):   # noqa: N802 - the http.server API
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif route == "/api/dashboard":
                with state.lock:
                    body = state.body
                if body is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"building": True, **state.status()})
                else:
                    self._send(HTTPStatus.OK, body, "application/json")
            elif route == "/api/status":
                self._json(HTTPStatus.OK, state.status())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        do_HEAD = do_GET    # noqa: N815

        def do_POST(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/refresh":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            started = state.request_refresh()
            self._json(HTTPStatus.ACCEPTED if started else HTTPStatus.TOO_MANY_REQUESTS,
                       {"started": started, **state.status()})

        def log_message(self, fmt, *args):
            if state.args.verbose:
                _log(f"{self.address_string()} {fmt % args}")

    return Handler


def make_server(args) -> tuple[ThreadingHTTPServer, _State]:
    state = _State(args)
    server = ThreadingHTTPServer((args.host, args.port), _handler(state, _page()))
    server.daemon_threads = True
    return server, state


def cmd_serve(args) -> int:
    if args.refresh < 1:
        raise RuntimeError("--refresh must be at least 1 minute")
    if (getattr(args, "orders_refresh", 0) or 0) < 0:
        raise RuntimeError("--orders-refresh cannot be negative (0 turns it off)")
    cmd_ledger._open().close()      # fail now, not in the refresh thread, when there is no ledger
    server, state = make_server(args)
    threading.Thread(target=state.loop, name="dashboard-refresh", daemon=True).start()
    host, port = server.server_address[:2]
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    orders_every = getattr(args, "orders_refresh", 0) or 0
    _log(f"serving http://{shown}:{port}/ (refresh every {args.refresh} min"
         f"{f', orders every {orders_every} min' if 0 < orders_every < args.refresh else ''}"
         f"{', no ledger sync' if args.no_sync else ''}{', no live reports' if args.no_live else ''})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_build(args) -> int:
    doc = build_document(since=args.since, live=not args.no_live, orders_char=args.orders_char)
    text = json.dumps(doc, indent=2)
    if args.out:
        storage.atomic_write(args.out, text)
    else:
        print(text)
    return 0


def cmd_dashboard(args) -> int:
    handler = {"serve": cmd_serve, "build": cmd_build}.get(args.dashboard_action)
    if handler is None:
        print("usage: eve-skills dashboard {serve,build} [--help]", file=sys.stderr)
        return 2
    return handler(args)

