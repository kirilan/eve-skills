"""`dashboard`: a local web page with the operation's profitability, industry jobs and market orders.

`serve` runs a small HTTP server (standard library only) that rebuilds its document every
`--refresh` minutes: a ledger sync when the last one is that old, a replay of the ledger, and the
live `industry status --corp` and `orders --check` reports. The page polls the document and redraws.
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
MIN_MANUAL_REFRESH_SECONDS = 300
# A live report that takes longer than this has hung on ESI; the page shows the last good one.
LIVE_TIMEOUT_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: str) -> None:
    print(f"{_iso(_now())} dashboard: {message}", file=sys.stderr, flush=True)


def _live(*args: str) -> tuple[dict | None, str | None]:
    """Run one of our own reports with --json in a child process: (document, error)."""
    command = [sys.executable, "-m", "eve_skills", *args, "--json"]
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


def _market_volume(conn, type_ids) -> dict[int, float]:
    """Median daily units traded in Jita's region over the last 30 stored days, per type that has history."""
    out = {}
    for type_id in type_ids:
        rows = ledger_db.market_history(conn, price_watch.HUB.region_id, type_id)[-30:]
        volumes = [r["volume"] for r in rows if r.get("volume") is not None]
        if volumes:
            out[type_id] = float(statistics.median(volumes))
    return out


def build_document(*, since: str | None, live: bool = True, refresh_minutes: int | None = None,
                   orders_char: str | None = None, record: bool = False) -> dict:
    """Replay the ledger and (unless `live` is off) read jobs, orders and stock from ESI. With `record`
    the live stock is also stored in the ledger as a snapshot, which is what builds the stock history."""
    now = _now()
    client = cmd_ledger._client()
    with contextlib.closing(cmd_ledger._open()) as conn:
        book, header, _recipes, _invention = cmd_ledger._state(conn)
    start = since or header["cutover"] or now.date().isoformat()
    status = orders = inventory = None
    status_error = orders_error = inventory_error = None
    if live:
        orders_args = ["orders", "--check"] + (["--char", orders_char] if orders_char else [])
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {key: pool.submit(_live, *cmd) for key, cmd in (
                ("status", ("industry", "status", "--corp")), ("orders", orders_args),
                ("inventory", ("ledger", "inventory")))}
        status, status_error = jobs["status"].result()
        orders, orders_error = jobs["orders"].result()
        inventory, inventory_error = jobs["inventory"].result()
    else:
        status_error = orders_error = inventory_error = "live reports off (--no-live)"
    items = (inventory or {}).get("items") or []
    with contextlib.closing(ledger_db.connect()) as conn:
        if record and items:
            with conn:
                ledger_db.insert_stock_snapshot(conn, _iso(now), stockpile.stock_rows(items))
        snapshots = ledger_db.stock_snapshots(
            conn, since=(now.date() - timedelta(days=stockpile.HISTORY_DAYS - 1)).isoformat())
        volume = _market_volume(conn, book.produced_products)
    stock = stockpile.analyse(book, items, today=now.date(), market_volume=volume,
                              blueprints=((status or {}).get("owners") or [{}])[0].get("blueprints") or [],
                              snapshots=snapshots)
    stock["error"] = inventory_error
    stock["as_of"] = (inventory or {}).get("prices_as_of")
    return dashboard.document(book, header, since=start, now=_iso(now), today=now.date(),
                              names=lambda ids: cmd_ledger._names(client, ids),
                              status=status, status_error=status_error, orders=orders, orders_error=orders_error,
                              stock=stock, refresh_minutes=refresh_minutes)


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
            _log(f"document rebuilt in {time.monotonic() - started:.0f} s")
        except (RuntimeError, OSError, ValueError, sqlite3.Error, esi_mod.EsiError) as err:
            error = str(err)
            _log(f"refresh failed: {err}")
        with self.lock:
            self.refreshing = False
            self.last_error = error
            self.next_refresh = _iso(_now() + timedelta(minutes=args.refresh))

    def loop(self) -> None:
        while True:
            self.refresh()
            self.wake.wait(self.args.refresh * 60)
            self.wake.clear()

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
    cmd_ledger._open().close()      # fail now, not in the refresh thread, when there is no ledger
    server, state = make_server(args)
    threading.Thread(target=state.loop, name="dashboard-refresh", daemon=True).start()
    host, port = server.server_address[:2]
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    _log(f"serving http://{shown}:{port}/ (refresh every {args.refresh} min"
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

