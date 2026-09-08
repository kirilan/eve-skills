"""Deterministic fake ESI environment for command-level integration tests.

Nothing here touches the network, real XDG directories or real credentials:
- $XDG_CONFIG_HOME / $XDG_CACHE_HOME / $XDG_DATA_HOME are redirected into a per-test
  temporary tree seeded with synthetic token records and alpha-cap data;
- urllib.request.urlopen is replaced by an in-process router serving canned ESI
  documents. Routes registered with `token=` verify the bearer exactly, so a bug
  that mixes up characters' tokens surfaces as a real AuthError instead of silently
  returning another character's data.

Response shapes mirror current live ESI: standings is a bare list of
{from_id, from_type, standing} and asset rows carry `type_id` (not the retired
`typeID`). Responses omit Expires/Date headers by default so nothing is cached or
clock-shifted unless a test asks for it.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from unittest import mock

from eve_skills import esi, sso


def iso(offset_seconds: float = 0.0) -> str:
    """ISO timestamp relative to the real clock; the client's `now` is the real clock too
    (no Date header is served), so queue math stays stable across a test run."""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).replace(microsecond=0).isoformat()


def http_date(offset_seconds: float = 0.0) -> str:
    """RFC 1123 stamp, the format ESI actually uses for Last-Modified/Expires.

    `iso` above is what JSON bodies carry; freshness parsing must handle the header format, so
    market fixtures serve headers with this helper rather than accidentally testing ISO parsing."""
    return format_datetime(datetime.now(timezone.utc) + timedelta(seconds=offset_seconds), usegmt=True)


def fake_headers(extra: dict | None = None) -> Message:
    msg = Message()
    for key, value in (extra or {}).items():
        msg[key] = str(value)
    return msg


class FakeResponse:
    """Stand-in for the context-managed object urllib.request.urlopen returns."""

    def __init__(self, doc, headers: dict | None = None):
        self._body = json.dumps(doc).encode()
        self.headers = fake_headers(headers)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url: str, status: int, payload, headers: dict | None = None) -> urllib.error.HTTPError:
    """HTTPError shaped like the one urllib raises: readable body + .headers for ESI limit notes."""
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    err = urllib.error.HTTPError(url, status, "transport error", fake_headers(headers), io.BytesIO(body))
    # Real HTTPErrors are closed by the http.client machinery; ours would emit a
    # ResourceWarning at GC. Pretend the body is already managed - it stays readable.
    err._closer.close_called = True
    return err


@dataclass
class Call:
    method: str
    path: str          # URL path only - ESI's BASE carries no prefix
    url: str
    query: dict        # first value per parameter
    headers: dict      # lower-cased header names
    json: object       # decoded request body, or None


class _Route:
    def __init__(self, doc=None, handler=None, token=None, headers=None, error=None):
        self.doc = doc
        self.handler = handler      # call -> doc | (doc, extra_headers)
        self.token = token          # required bearer token; None = public route
        self.headers = headers or {}
        self.error = error          # (status, payload) served on every call


class FakeEsiServer:
    """In-process ESI. Routes are keyed by (METHOD, path); unknown calls fail the test loudly."""

    def __init__(self):
        self.routes: dict[tuple[str, str], _Route] = {}
        self.calls: list[Call] = []

    def route(self, method: str, path: str, **kw):
        self.routes[(method, path)] = _Route(**kw)

    def get(self, path: str, **kw):
        self.route("GET", path, **kw)

    def post(self, path: str, **kw):
        self.route("POST", path, **kw)

    def calls_to(self, path: str) -> list[Call]:
        return [c for c in self.calls if c.path == path]

    def __call__(self, req, timeout=None):
        split = urllib.parse.urlsplit(req.full_url)
        raw = req.data.decode() if req.data else None
        headers = {k.lower(): v for k, v in req.header_items()}
        call = Call(method=req.get_method(), path=split.path, url=req.full_url,
                    query={k: v[0] for k, v in urllib.parse.parse_qs(split.query).items()},
                    headers=headers, json=json.loads(raw) if raw else None)
        self.calls.append(call)
        route = self.routes.get((call.method, call.path))
        if route is None:
            raise AssertionError(
                f"unexpected ESI request: {call.method} {call.url}\n"
                f"registered: {sorted(self.routes)}")
        if route.token is not None and headers.get("authorization") != f"Bearer {route.token}":
            # A wrong/missing bearer must look exactly like ESI rejecting the token.
            raise http_error(call.url, 401, {"error": "invalid token for this character"})
        if route.error is not None:
            status, payload = route.error
            raise http_error(call.url, status, payload)
        result = route.handler(call) if route.handler is not None else route.doc
        headers_out = dict(route.headers)
        if isinstance(result, tuple):
            result, extra = result
            headers_out.update(extra)
        return FakeResponse(result, headers_out)


# ---------------------------------------------------------------------------
# Synthetic universe: ids and names are invented here; tests pin the logic, not the SDE.
# ---------------------------------------------------------------------------

SKILL_CAPPED = 1003        # alpha cap 3 in both grades
SKILL_WIDE = 1005          # alpha cap 5
SKILL_NAV = 1011           # alpha cap 5; used by the active training item
SKILL_UNSTARTED = 1007     # trained level 0: exists only in --json, never in tables/CSV
SKILL_OMEGA_ONLY = 2000    # catalog-known (resolvable) but absent from every alpha grade
SKILL_UNSEEN = 1099        # known to the skill catalog only: ESI has never heard of it

CAPS_BY_RACE = {str(SKILL_CAPPED): 3, str(SKILL_WIDE): 5, str(SKILL_NAV): 5}

# Market fixture universe (invented like everything here except the ids, which are real so that
# `market.HUBS` and these rows agree): two regions with books, one region whose ESI shard is down,
# and the two non-trading families /universe/regions also lists - wormhole and abyssal space -
# which --global must never ask for.
MARKET_FORGE = 10000002
MARKET_DOMAIN = 10000043
MARKET_BROKEN = 10000042
WORMHOLE_REGION = 11000001
ABYSSAL_REGION = 12000001
STATION_JITA = 60003760          # Jita 4-4, the default scope
STATION_FORGE_OTHER = 60099001   # a second station in the same region
STATION_AMARR = 60008494         # Amarr VIII, the `--hub amarr` fixture
SYSTEM_FORGE = 30000142
SYSTEM_AMARR = 30002187


def _order(order_id: int, price: float, location_id: int, system_id: int, *, remain: int = 100,
           total: int | None = None, buy: bool = False, type_id: int = 34) -> dict:
    """One `/markets/{region}/orders` row with every key live ESI sends."""
    return {"order_id": order_id, "type_id": type_id, "location_id": location_id,
            "system_id": system_id, "price": price, "volume_remain": remain,
            "volume_total": remain if total is None else total, "is_buy_order": buy,
            "issued": "2026-09-01T00:00:00Z", "duration": 90, "min_volume": 1, "range": "region"}


def owner_order(order_id: int, *, type_id: int = 34, buy: bool = False, price: float = 5.5,
                remain: int = 100, total: int | None = None, location: int = STATION_JITA,
                region: int = MARKET_FORGE, issued: str | None = None, duration: int = 90,
                escrow: float | None = None, min_volume: int | None = None,
                range_: str = "station", state: str | None = None, wallet_division: int | None = None,
                issued_by: int | None = None, corp_order: bool | None = None) -> dict:
    """One row of `/characters/{id}/orders[/history]`, or of its corporation twin.

    Only the keys live ESI sends for that case appear: `escrow` belongs to buy orders,
    `wallet_division`/`issued_by` to corporation rows and `state` to history rows - a fetcher that
    read them unconditionally would pass here and break on the real endpoint. `is_corporation` is
    only ever sent by the character endpoints, so `corp_order=True` is how a personal book shows an
    order funded from the corporation wallet."""
    row = {"order_id": order_id, "type_id": type_id, "region_id": region, "location_id": location,
           "range": range_, "price": price, "volume_remain": remain,
           "volume_total": remain if total is None else total,
           "issued": issued or iso(-86400), "duration": duration,
           "is_buy_order": buy}
    # Character endpoints always answer with `is_corporation`, corporation endpoints never mention it,
    # so a corporation row only carries the key when a test insists: that way the fetcher's own claim
    # about a corporation book is what gets checked, not something the fixture handed it.
    if issued_by is None or corp_order is not None:
        row["is_corporation"] = bool(corp_order)
    for key, value in (("escrow", escrow), ("min_volume", min_volume), ("state", state),
                       ("wallet_division", wallet_division), ("issued_by", issued_by)):
        if value is not None:
            row[key] = value
    return row


# Per-region book ages, so a cluster scan's freshness can be pinned to its oldest region.
MARKET_BOOK_AGE = {MARKET_FORGE: 120, MARKET_DOMAIN: 900}


# `volume_total` deliberately differs from `volume_remain` on some rows: a quote that folded the
# wrong one would report orders as bigger than they are.
MARKET_ORDERS: dict[int, list[dict]] = {
    MARKET_FORGE: [
        _order(900001, 5.05, STATION_JITA, SYSTEM_FORGE, remain=1000, total=2000),
        _order(900002, 4.98, STATION_FORGE_OTHER, SYSTEM_FORGE, remain=250),
        _order(900003, 4.20, STATION_JITA, SYSTEM_FORGE, remain=800, buy=True),
        _order(900004, 4.30, STATION_FORGE_OTHER, SYSTEM_FORGE, remain=120, total=400, buy=True),
        # Another type in the same region: only appears if a book is read without `type_id`.
        _order(900005, 7.00, STATION_JITA, SYSTEM_FORGE, remain=10, type_id=36),
        # A type with demand and no supply at all: the sell side must come back missing, not 0.
        _order(900008, 3.10, STATION_JITA, SYSTEM_FORGE, remain=40, buy=True, type_id=590),
    ],
    MARKET_DOMAIN: [
        _order(900006, 6.20, STATION_AMARR, SYSTEM_AMARR, remain=50),
        # The best buy in the cluster, and it is not in the region with the best sell.
        _order(900007, 4.55, STATION_AMARR, SYSTEM_AMARR, remain=60, buy=True),
    ],
}

# Ten days, oldest first like live ESI; `days=7` must take the last seven, not the first.
MARKET_HISTORY: dict[int, list[dict]] = {
    MARKET_FORGE: [{"date": f"2026-08-{day:02d}T12:00:00Z", "volume": 10 * day,
                    "average": 5.0 + day / 100, "highest": 5.2, "lowest": 4.9, "order_count": 3,
                    "type_id": 34} for day in range(1, 11)],
    MARKET_DOMAIN: [],
}

# PLEX trades on the account-wide vault market, which no regional order book exposes - so its book
# is empty in every region here exactly as it is live, and only `/markets/prices` knows it at all.
MARKET_PLEX = 44992

# An ordinary type that nobody here has ordered in any region, and that is nothing special: ESI's
# price document carries a row for it, so the whole-cluster footnote has a figure to show. It exists
# to keep PLEX's vault explanation from being tested as the general case - an empty cluster scan of
# an unremarkable module must say "empty everywhere", not "trades out of sight".
MARKET_UNTRADED = 32458

# `/markets/prices`: one row per priced type, with both optional keys exercised. PLEX's
# `adjusted_price` really is 0.0 on live ESI (a published value, not a gap); Pyerite's row omits the
# key outright; Large Skill Injector has no row at all. Three different statements, and a reader -
# or a fetcher that reaches for `or 0` - must not be able to confuse them. The untraded module's row
# is the fourth case: priced by ESI, ordered by nobody, in every region of this fixture.
MARKET_PRICES = [
    {"type_id": 34, "adjusted_price": 4.05, "average_price": 4.87},
    {"type_id": 36, "average_price": 4.20},
    {"type_id": MARKET_PLEX, "adjusted_price": 0.0, "average_price": 4574918.36},
    {"type_id": MARKET_UNTRADED, "average_price": 118.5},
]

# Live ESI stamps this document an hour ahead of its own `Last-Modified` (measured 2026-09-07), so
# the fixture serves it hours old rather than minutes: a footnote's age must be assertable as the
# document's own, not accidentally the book's five-minute age.
MARKET_PRICES_AGE = 3600 + 42

MARKET_IDS: dict[str, dict[str, list[int]]] = {
    # ESI answers a name in every category: Tritanium is also a character, and a resolver that
    # takes the first non-empty bucket would price a player.
    "Tritanium": {"characters": [91007777], "inventory_types": [34]},
    "Large Skill Injector": {"inventory_types": [40520]},
    "The Forge": {"regions": [MARKET_FORGE]},
    "PLEX": {"inventory_types": [MARKET_PLEX]},
    "Domain": {"regions": [MARKET_DOMAIN]},
    "Nanite Repair Paste": {"inventory_types": [MARKET_UNTRADED]},
}

# Order fixtures. CORP_SHARED is Ada's corporation in install_core, and Mira joins it in
# install_orders: two stored characters in one corp is the case the corporation view has to
# collapse into one set of rows instead of reporting them twice.
CORP_SHARED = 98356123


NAMES: dict[int, str] = {
    SKILL_CAPPED: "Capped Skill",
    SKILL_WIDE: "Wide Skill",
    SKILL_NAV: "Navigation",
    SKILL_UNSTARTED: "Unstarted Skill",
    SKILL_OMEGA_ONLY: "Omega Only Skill",
    MARKET_UNTRADED: "Nanite Repair Paste",
    34: "Tritanium",
    36: "Pyerite",
    MARKET_PLEX: "PLEX",
    590: "Caldari Ship Blueprint",
    32874: "Memory Augmentation",
    60003760: "Jita - Mradd",
    30000142: "The Forge",
    60015129: "Rens - Datauri",
    3019840: "Agent Six",
    1000125: "Science and Trade Institute",
    MARKET_FORGE: "The Forge",
    MARKET_DOMAIN: "Domain",
    MARKET_BROKEN: "Heimatar",
    STATION_AMARR: "Amarr VIII (Oris) - Emperor Family Academy",
    STATION_FORGE_OTHER: "Nourvukaiken III - Moon 4 - Federation Customs Office",
    CORP_SHARED: "Shared Ledger Holdings",
    91000001: "Ada Vane",
    91000002: "Vela Krinn",
    91000003: "Mira Solen",
}


# Inventory universe, invented like everything above except the two ore types. `universe.type_info`
# needs a type -> group -> category chain per held type, so all five records are served here. Two
# types exist only for this view: a Rifter that carries a freight container (the only nesting live
# asset rows express) and a blueprint ESI's price document has no row for at all.
INV_TYPE_SHIP, INV_TYPE_CONTAINER = 587, 21078
INV_GROUP_ORE, INV_GROUP_FRIGATE = 18, 420
INV_GROUP_BLUEPRINT, INV_GROUP_CONTAINER = 96, 303
INV_CATEGORY_MATERIAL, INV_CATEGORY_SHIP = 5, 7      # exactly `Ship` is what makes an item a ship
INV_CATEGORY_BLUEPRINT, INV_CATEGORY_CONTAINER = 9, 20
INVENTORY_TYPES = {
    34: {"type_id": 34, "name": "Tritanium", "group_id": INV_GROUP_ORE, "volume": 0.02},
    36: {"type_id": 36, "name": "Pyerite", "group_id": INV_GROUP_ORE, "volume": 0.02},
    590: {"type_id": 590, "name": "Caldari Ship Blueprint", "group_id": INV_GROUP_BLUEPRINT,
          "volume": 1.0},
    INV_TYPE_SHIP: {"type_id": INV_TYPE_SHIP, "name": "Rifter", "group_id": INV_GROUP_FRIGATE,
                    "volume": 2500.0, "packaged_volume": 780.0},
    INV_TYPE_CONTAINER: {"type_id": INV_TYPE_CONTAINER, "name": "Freight Container",
                         "group_id": INV_GROUP_CONTAINER, "volume": 25000.0},
}
INVENTORY_GROUPS = {
    INV_GROUP_ORE: {"group_id": INV_GROUP_ORE, "name": "Ore", "category_id": INV_CATEGORY_MATERIAL},
    INV_GROUP_FRIGATE: {"group_id": INV_GROUP_FRIGATE, "name": "Frigate",
                        "category_id": INV_CATEGORY_SHIP},
    INV_GROUP_BLUEPRINT: {"group_id": INV_GROUP_BLUEPRINT, "name": "Ship Blueprint",
                          "category_id": INV_CATEGORY_BLUEPRINT},
    INV_GROUP_CONTAINER: {"group_id": INV_GROUP_CONTAINER, "name": "Container",
                          "category_id": INV_CATEGORY_CONTAINER},
}
INVENTORY_CATEGORIES = {INV_CATEGORY_MATERIAL: "Material", INV_CATEGORY_SHIP: "Ship",
                        INV_CATEGORY_BLUEPRINT: "Blueprint", INV_CATEGORY_CONTAINER: "Container"}

# Item-sized location ids, all above int32 like real ones. Two player structures: ESI names the
# first to Ada's token and refuses the second with 403, which is exactly how live ESI answers a
# character that never consented `esi-universe.read_structures.v1`. Neither id may ever be asked of
# /universe/names - see `_names_handler` - so any name for them can only come from the right place.
INV_SHIP_ITEM, INV_CONTAINER_ITEM = 90000001, 90000002
INV_CITADEL_SEEN, INV_CITADEL_BLIND = 1048236548577, 1048248887257
INV_CUSTOM_NAMES = {INV_SHIP_ITEM: "Nightwatch", INV_CONTAINER_ITEM: "Second Shift"}
CORP_CUSTOM_NAMES = {2002: "Ledger Runner"}

# `/markets/prices` for inventory runs: the market document plus a ship that only has CCP's industry
# figure (so the average-missing fallback is exercised) and a container published at 0.0 - a
# published value, which counts as priced, unlike type 590, whose row does not exist. The three
# statements an inventory footnote has to keep apart.
INVENTORY_PRICES = MARKET_PRICES + [{"type_id": INV_TYPE_SHIP, "adjusted_price": 12_000_000.0},
                                    {"type_id": INV_TYPE_CONTAINER, "adjusted_price": 0.0}]


@dataclass(frozen=True)
class Character:
    character_id: int
    name: str
    token: str


ADA = Character(91000001, "Ada Vane", "token-ada")     # omega evidence + active queue
VELA = Character(91000002, "Vela Krinn", "token-vela")  # live clamp (alpha), empty queue

MIRA = Character(91000003, "Mira Solen", "token-mira")  # Ada's colleague in CORP_SHARED

# `issued` offsets are relative to the real clock, so their order is fixed even though the stamps
# move: Mira's sell is always the newest row of the combined book.
ADA_OPEN = [
    owner_order(700001, price=5.75, remain=100, total=250),
    owner_order(700002, type_id=36, buy=True, price=3.10, remain=500, escrow=1550.0,
                min_volume=10, location=STATION_AMARR, region=MARKET_DOMAIN, duration=30),
]
# Issued/duration pairs are chosen so an `expired` row's derived expiry lies in the past - a history
# row that lapses in the future would be self-contradictory, and the derivation could not be read
# off the table. A cancelled order keeps its original duration: it ended early, ESI does not say when.
ADA_HISTORY = [
    owner_order(700101, state="cancelled", remain=40, total=100, issued=iso(-40 * 86400),
                duration=90),                                        # called off with stock left
    owner_order(700102, type_id=36, state="expired", remain=0, total=90, issued=iso(-60 * 86400),
                duration=30),                                        # sold out -> filled
    # `escrow` is the ISK still held for the units not yet bought, i.e. price * volume_remain.
    owner_order(700103, type_id=590, buy=True, price=400000.0, state="expired", remain=20, total=80,
                escrow=8000000.0, issued=iso(-30 * 86400), duration=14),   # lapsed part-filled
]
MIRA_OPEN = [owner_order(700011, price=4.99, remain=10, issued=iso(-3600))]
CORP_OPEN = [
    owner_order(700201, price=6.10, remain=1000, total=4000, wallet_division=2,
                issued_by=MIRA.character_id),
    owner_order(700202, type_id=590, buy=True, price=120.0, remain=5, escrow=600.0,
                location=STATION_AMARR, region=MARKET_DOMAIN, wallet_division=7,
                issued_by=ADA.character_id),
]
CORP_HISTORY = [owner_order(700301, state="expired", remain=0, total=500, wallet_division=2,
                            issued=iso(-50 * 86400), duration=7)]


@dataclass
class OrderBook:
    """One owner's two order documents, mutable between watch cycles.

    The one-shot commands read fixed fixtures; a watcher has to see the *same* order move from the open
    book into the history across polls, which only handlers over live state can express."""
    open: list = field(default_factory=list)
    history: list = field(default_factory=list)
    history_error: tuple | None = None    # (status, payload): the history call fails this cycle


class FakeEsiEnv:
    """Temporary XDG tree + fake transport + two seeded stored characters.

    Usage in a TestCase::

        def setUp(self):
            self.env = FakeEsiEnv()
            self.env.start()
            self.env.install_core()
            self.addCleanup(self.env.stop)
    """

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-test-")
        root = self.tmp.name
        self.config_home = os.path.join(root, "config")
        self.cache_home = os.path.join(root, "cache")
        self.data_home = os.path.join(root, "data")
        self.state_home = os.path.join(root, "state")
        self.server = FakeEsiServer()
        self.order_books: dict[str, OrderBook] = {}
        self.names = dict(NAMES)
        self._patchers = []

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self._patchers.append(mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.config_home,
            "XDG_CACHE_HOME": self.cache_home,
            "XDG_DATA_HOME": self.data_home,
            "XDG_STATE_HOME": self.state_home,
        }))
        self._patchers.append(mock.patch.object(esi.urllib.request, "urlopen", self.server))
        for patcher in self._patchers:
            patcher.start()
        os.makedirs(os.path.join(self.config_home, "eve-skills"), exist_ok=True)
        # Ada consented to everything; Vela only to the base skills scopes - every
        # optional command must degrade to a hint for her rather than fail.
        self.write_tokens([
            self.token_for(ADA, sso.SCOPES + sso.scopes_for(["all"])),
            self.token_for(VELA),
        ])
        self._write_json(os.path.join(self.data_home, "eve-skills", "clone_grades.json"), {
            "source": "synthetic", "build": 2500001, "fetched": iso(-86400),
            "grades": {
                "1": {"name": "Caldari Alpha Clone", "caps": dict(CAPS_BY_RACE)},
                "8": {"name": "Gallente Alpha Clone", "caps": dict(CAPS_BY_RACE)},
            },
        })
        self._write_json(os.path.join(self.data_home, "eve-skills", "bloodline_races.json"), {
            "source": "synthetic", "build": 2500001, "fetched": iso(-86400),
            "races": {"402": 1, "403": 8},
        })
        return self

    def stop(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers.clear()
        self.tmp.cleanup()

    # -- seeding helpers ------------------------------------------------------

    def token_for(self, char: Character, scopes: list[str] | None = None) -> dict:
        return {
            "client_id": "test-client",
            "access_token": char.token,
            "refresh_token": None,
            "expires_at": time.time() + 3600,  # never expires mid-test: no refresh traffic
            "scopes": list(sso.SCOPES if scopes is None else scopes),
            "character_id": char.character_id,
            "character_name": char.name,
        }

    def write_tokens(self, records: list[dict]):
        store = {"characters": {str(r["character_id"]): r for r in records}}
        self._write_json(os.path.join(self.config_home, "eve-skills", "tokens.json"), store)

    @staticmethod
    def _write_json(path: str, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    # -- route scenarios ------------------------------------------------------

    def install_core(self):
        """Everything `cli.gather` needs for both characters, plus attributes."""
        self.server.post("/universe/names", handler=self._names_handler)
        public = {
            ADA: {"name": "Ada Vane", "bloodline_id": 402, "corporation_id": CORP_SHARED},
            VELA: {"name": "Vela Krinn", "bloodline_id": 403, "corporation_id": 98000000},
        }
        skills = {
            ADA: {
                "skills": [
                    {"skill_id": SKILL_CAPPED, "trained_skill_level": 5, "active_skill_level": 5,
                     "skillpoints_in_skill": 512000},                      # beyond cap 3 -> omega
                    {"skill_id": SKILL_WIDE, "trained_skill_level": 4, "active_skill_level": 4,
                     "skillpoints_in_skill": 45250},                       # + done queue item -> pending L5
                    {"skill_id": SKILL_OMEGA_ONLY, "trained_skill_level": 2, "active_skill_level": 2,
                     "skillpoints_in_skill": 8000},                        # omega-only skill
                    {"skill_id": SKILL_NAV, "trained_skill_level": 1, "active_skill_level": 1,
                     "skillpoints_in_skill": 1500},
                    {"skill_id": SKILL_UNSTARTED, "trained_skill_level": 0, "active_skill_level": 0,
                     "skillpoints_in_skill": 0},                           # ESI lists untrained prereqs
                ],
                "total_sp": 6000000, "unallocated_sp": 120000,
            },
            VELA: {
                "skills": [
                    {"skill_id": SKILL_CAPPED, "trained_skill_level": 5, "active_skill_level": 3,
                     "skillpoints_in_skill": 512000},                      # live clamp -> ALPHA
                    {"skill_id": SKILL_WIDE, "trained_skill_level": 2, "active_skill_level": 2,
                     "skillpoints_in_skill": 4500},
                ],
                "total_sp": 900000, "unallocated_sp": 5000,
            },
        }
        queues = {
            ADA: [
                {"skill_id": SKILL_WIDE, "finished_level": 5, "queue_position": 0,
                 "start_date": iso(-7200), "finish_date": iso(-900)},       # done, pending login
                {"skill_id": SKILL_NAV, "finished_level": 2, "queue_position": 1,
                 "start_date": iso(-3600), "finish_date": iso(9030)},       # training now; ~2h30m left stays stable under ms drift
            ],
            VELA: [],
        }
        attributes = {
            ADA: {"perception": 23, "intelligence": 21, "memory": 20, "charisma": 19,
                  "willpower": 22, "last_remap_date": "2025-03-01T12:00:00+00:00",
                  "accumulated_remaps": 2, "accelerator_bonus_days": 7},
            VELA: {"perception": 19, "intelligence": 23, "memory": 21, "charisma": 20,
                   "willpower": 18, "last_remap_date": None, "accumulated_remaps": 0},
        }
        for char in (ADA, VELA):
            self.server.get(f"/characters/{char.character_id}", doc=public[char])
            self.server.get(f"/characters/{char.character_id}/skills", doc=skills[char], token=char.token)
            self.server.get(f"/characters/{char.character_id}/skillqueue", doc=queues[char], token=char.token)
            self.server.get(f"/characters/{char.character_id}/attributes", doc=attributes[char], token=char.token)
        self.core_docs = {"public": public, "skills": skills, "queues": queues}
        # Ada consented to every optional scope, so a watch cycle polls her orders too. Empty documents
        # by default: watch tests that want order events republish them with `install_watch_orders`, and
        # the ones that do not must see neither an unrouted path nor a stray event.
        self.install_watch_orders(ADA)
        self.install_watch_corp_orders(CORP_SHARED, ADA.token)

    def set_queue(self, char: Character, queue: list[dict]):
        """Replace one character's live queue and republish the route (multi-cycle watch)."""
        self.core_docs["queues"][char] = queue
        self.server.get(f"/characters/{char.character_id}/skillqueue", doc=queue, token=char.token)

    def set_trained_level(self, char: Character, skill_id: int, level: int):
        """Move one trained level on the skills endpoint and republish the route."""
        doc = self.core_docs["skills"][char]
        for entry in doc["skills"]:
            if entry["skill_id"] == skill_id:
                entry["trained_skill_level"] = level
                entry["active_skill_level"] = min(entry.get("active_skill_level", level), level)
        self.server.get(f"/characters/{char.character_id}/skills", doc=doc, token=char.token)

    def install_skill_catalog(self) -> None:
        """Ranks and prerequisites for the synthetic ids above, invented here so plan tests
        pin the planner rather than whatever SDE build CCP happens to have shipped."""
        self._write_json(os.path.join(self.data_home, "eve-skills", "skill_catalog.json"), {
            "source": "synthetic", "build": 2500001, "fetched": iso(-86400),
            "skills": {
                str(SKILL_CAPPED): {"name": "Capped Skill", "rank": 1, "pri": "perception",
                                    "sec": "willpower", "pre": {}},
                str(SKILL_WIDE): {"name": "Wide Skill", "rank": 2, "pri": "intelligence",
                                  "sec": "memory", "pre": {str(SKILL_CAPPED): 3}},
                str(SKILL_NAV): {"name": "Navigation", "rank": 1, "pri": "intelligence",
                                 "sec": "perception", "pre": {}},
                str(SKILL_UNSTARTED): {"name": "Unstarted Skill", "rank": 3, "pri": "memory",
                                       "sec": "intelligence", "pre": {str(SKILL_CAPPED): 1}},
                str(SKILL_OMEGA_ONLY): {"name": "Omega Only Skill", "rank": 4, "pri": "charisma",
                                        "sec": "willpower", "pre": {str(SKILL_UNSTARTED): 2}},
                # Needs a skill Ada is still training towards: nothing in ESI names this one.
                str(SKILL_UNSEEN): {"name": "Unseen Skill", "rank": 1, "pri": "perception",
                                    "sec": "willpower", "pre": {str(SKILL_NAV): 3}},
            },
        })

    def _names_handler(self, call: Call):
        """`/universe/names` with live ESI's sharpest edge: one id above int32 fails the *whole*
        batch with 400, so every station name in it goes with it. Answering the resolvable subset
        instead would let the bug `cmd_inventory` used to have pass here forever."""
        overflow = [ident for ident in call.json or []
                    if isinstance(ident, int) and not -2**31 <= ident <= 2**31 - 1]
        if overflow:
            raise http_error(call.url, 400, {"error": f"id out of int32 range: {overflow[0]}"})
        return [{"id": i, "name": self.names[i]} for i in call.json if i in self.names]

    def install_standings(self):
        # Live shape: a bare list; no {"standings": ...} envelope anymore.
        self.server.get(f"/characters/{ADA.character_id}/standings", token=ADA.token, doc=[
            {"from_id": 3019840, "from_type": "agent", "standing": 3.5},
            {"from_id": 1000125, "from_type": "npc_corp", "standing": -1.25},
            {"from_id": 500001, "from_type": "faction", "standing": 0.0},   # unresolvable id
        ])

    def install_jobs(self):
        self.server.get(f"/characters/{ADA.character_id}/industry/jobs", token=ADA.token, doc=[
            {"activity": 1, "status": "active", "output_type_id": 34, "installed_in": 60003760,
             "installed_runs": 2, "runs": 10, "finish_date": iso(7200)},
            {"activity": 8, "status": "finished", "output_type_id": 36, "installed_in": 60015129,
             "finish_date": iso(-86400)},
        ])

    def install_inventory(self):
        """Ada's holdings, reported the way live ESI reports them.

        Nine rows over two pages, one per shape an inventory view has to render: plain quantities in
        two NPC stations and loose in a system, a named ship carrying a named cargo container (the
        nesting live rows express, via `location_type: "item"`), a player structure this token may
        name, and one it may not - the difference that makes the consent notice assertable. The type
        catalogue and the price document come with it, because a run reads both before it renders.
        """
        self.install_market()
        # Republished rather than extended in place: market tests read `MARKET_PRICES` as served,
        # and that document has no business carrying inventory-only rows.
        self.server.get("/markets/prices", doc=INVENTORY_PRICES,
                        headers={"Last-Modified": http_date(-MARKET_PRICES_AGE)})
        for type_id, doc in INVENTORY_TYPES.items():
            self.server.get(f"/universe/types/{type_id}", doc=doc)
        for group_id, doc in INVENTORY_GROUPS.items():
            self.server.get(f"/universe/groups/{group_id}", doc=doc)
        for category_id, name in INVENTORY_CATEGORIES.items():
            self.server.get(f"/universe/categories/{category_id}",
                            doc={"category_id": category_id, "name": name, "published": True})
        self.server.get(f"/universe/structures/{INV_CITADEL_SEEN}", token=ADA.token,
                        doc={"name": "Keepstar Outpost", "solar_system_id": SYSTEM_FORGE,
                             "type_id": 35893, "owner_id": CORP_SHARED})
        self.server.get(f"/universe/structures/{INV_CITADEL_BLIND}", token=ADA.token,
                        error=(403, {"error": "Forbidden"}))

        def asset_names(call: Call):
            return [{"item_id": i, "name": INV_CUSTOM_NAMES[i]} for i in call.json
                    if i in INV_CUSTOM_NAMES]

        self.server.post(f"/characters/{ADA.character_id}/assets/names", token=ADA.token,
                         handler=asset_names)

        def row(item_id, type_id, quantity, location_id, location_type, *, singleton=False,
                flag="Hangar"):
            """One asset row with every key a location decision reads, `location_type` included."""
            return {"item_id": item_id, "type_id": type_id, "quantity": quantity,
                    "is_singleton": singleton, "location_id": location_id,
                    "location_flag": flag, "location_type": location_type}

        page1 = [
            row(1001, 34, 500, STATION_JITA, "station"),
            row(1002, 590, 1, STATION_JITA, "station", singleton=True),
            row(INV_SHIP_ITEM, INV_TYPE_SHIP, 1, STATION_JITA, "station", singleton=True),
            row(INV_CONTAINER_ITEM, INV_TYPE_CONTAINER, 1, INV_SHIP_ITEM, "item", singleton=True,
                flag="Cargo"),
        ]
        page2 = [
            row(1003, 34, 1200, INV_CONTAINER_ITEM, "item", flag="Cargo"),
            row(1004, 36, 40, 60015129, "station"),
            row(1005, 36, 25, SYSTEM_FORGE, "solar_system", flag="Drop"),
            row(1006, 34, 700, INV_CITADEL_SEEN, "station"),
            row(1007, 36, 90, INV_CITADEL_BLIND, "station"),
        ]

        def assets(call: Call):
            doc = page1 if call.query.get("page", "1") == "1" else page2
            return doc, {"X-Pages": "2"}

        self.server.get(f"/characters/{ADA.character_id}/assets", token=ADA.token, handler=assets)

    def install_corp_inventory(self):
        """Corporation assets for Ada's corp, on top of the personal fixture.

        Same shapes, different owner: a corporation run must ask `/corporations/{corp}/assets/names`
        about the corporation's items. Reusing the character endpoint would 404 on live ESI and cost
        every custom name in the batch, so the two routes are registered separately here and the
        test can see which one was called."""
        self.install_inventory()
        self.server.get(f"/corporations/{CORP_SHARED}/assets", token=ADA.token, doc=[
            {"item_id": 2001, "type_id": 34, "quantity": 5000, "is_singleton": False,
             "location_id": STATION_JITA, "location_flag": "Hangar", "location_type": "station"},
            {"item_id": 2002, "type_id": INV_TYPE_SHIP, "quantity": 1, "is_singleton": True,
             "location_id": INV_CITADEL_SEEN, "location_flag": "Hangar", "location_type": "station"},
        ])

        def corp_asset_names(call: Call):
            return [{"item_id": i, "name": CORP_CUSTOM_NAMES[i]} for i in call.json
                    if i in CORP_CUSTOM_NAMES]

        self.server.post(f"/corporations/{CORP_SHARED}/assets/names", token=ADA.token,
                         handler=corp_asset_names)

    def install_travel(self):
        self.server.get(f"/characters/{ADA.character_id}/location", token=ADA.token,
                        doc={"solar_system_id": 30000142, "station_id": 60003760})
        self.server.get(f"/characters/{ADA.character_id}/clones", token=ADA.token, doc={
            "home_location": {"location_id": 60003760},
            "last_clone_jump_date": "2026-08-01T10:00:00+00:00",
            "jump_clones": [{"location_id": 60015129, "name": "Rens bolt-hole", "implants": [32874]}],
        })

    def install_implants(self):
        # The same type fitted in both head slots: one row per instance.
        self.server.get(f"/characters/{ADA.character_id}/implants", token=ADA.token,
                        doc={"implants": [32874, 32874]})

    # -- order routes ---------------------------------------------------------

    def install_order_book(self, path: str, token: str | None, rows=(), history=()) -> OrderBook:
        """Mutable `<path>` and `<path>/history` documents for one owner.

        A watch test has to move the *same* order from the live book into the history between cycles,
        which a frozen fixture cannot express - so both routes are handlers over one `OrderBook`.
        `install_orders()` below keeps serving fixed rows for the one-shot commands."""
        book = OrderBook(list(rows), list(history))
        self.order_books[path] = book

        def open_rows(_call: Call):
            return list(book.open)

        def history_rows(call: Call):
            if book.history_error is not None:
                # The two documents are cached separately live, so history can fail on its own while
                # the live book still answers - exactly the case `history_ok` exists for.
                raise http_error(call.url, *book.history_error)
            return list(book.history)

        self.server.get(path, token=token, handler=open_rows)
        self.server.get(f"{path}/history", token=token, handler=history_rows)
        return book

    def install_watch_orders(self, char: Character, rows=(), history=()) -> OrderBook:
        """Personal order documents for one stored character, editable between cycles."""
        return self.install_order_book(f"/characters/{char.character_id}/orders", char.token,
                                       rows, history)

    def install_watch_corp_orders(self, corp_id: int, token: str, rows=(), history=()) -> OrderBook:
        """The corporation twin, served for whichever colleague's token ESI accepts."""
        return self.install_order_book(f"/corporations/{corp_id}/orders", token, rows, history)

    def order_book(self, path: str) -> OrderBook:
        """The live documents a previous `install_order_book` registered for that path."""
        return self.order_books[path]

    def install_orders(self):
        """Personal and corporation order books, plus Mira - a third stored character in Ada's corp.

        Ada's history is served over two pages, so a fetcher that ignores `X-Pages` loses rows
        instead of passing. The roles route says she holds neither Accountant nor Trader, which is
        what lets the 403 path name something the user can actually fix."""
        self.write_tokens([
            self.token_for(ADA, sso.SCOPES + sso.scopes_for(["all"])),
            self.token_for(VELA),
            self.token_for(MIRA, sso.SCOPES + sso.scopes_for(["all"])),
        ])
        self.server.get(f"/characters/{MIRA.character_id}",
                        doc={"name": MIRA.name, "bloodline_id": 403, "corporation_id": CORP_SHARED})
        for char, rows in ((ADA, ADA_OPEN), (MIRA, MIRA_OPEN)):
            self.server.get(f"/characters/{char.character_id}/orders", token=char.token, doc=rows)

        def ada_history(call: Call):
            page = [ADA_HISTORY[:2], ADA_HISTORY[2:]]
            return (page[0] if call.query.get("page", "1") == "1" else page[1]), {"X-Pages": "2"}

        self.server.get(f"/characters/{ADA.character_id}/orders/history", token=ADA.token,
                        handler=ada_history)
        self.server.get(f"/characters/{MIRA.character_id}/orders/history", token=MIRA.token, doc=[])
        # Corporation rows belong to the corp: whichever colleague asks, Ada's token is what the
        # endpoint is registered for, so a second fetch by Mira would be rejected like live ESI.
        self.server.get(f"/corporations/{CORP_SHARED}/orders", token=ADA.token, doc=CORP_OPEN)
        self.server.get(f"/corporations/{CORP_SHARED}/orders/history", token=ADA.token,
                        doc=CORP_HISTORY)
        self.server.get(f"/characters/{ADA.character_id}/roles", token=ADA.token,
                        doc={"roles": {"Station_Manager": 1}, "titles": []})

    # -- market routes ----------------------------------------------------------

    def install_market(self):
        """Public market endpoints: id lookup, region list, two books, one dead shard, history."""
        self.server.post("/universe/ids", handler=self._ids_handler)
        self.server.get("/universe/regions", doc=[MARKET_FORGE, MARKET_DOMAIN, MARKET_BROKEN,
                                                  WORMHOLE_REGION, ABYSSAL_REGION])
        for region in (MARKET_FORGE, MARKET_DOMAIN):
            self.server.get(f"/markets/{region}/orders", handler=self._market_orders)
            self.server.get(f"/markets/{region}/history", handler=self._market_history)
        # 500 rather than 503: the client retries 503, and a test must not spend real seconds
        # asleep in backoff to prove that one dead region cannot sink a cluster scan.
        self.server.get(f"/markets/{MARKET_BROKEN}/orders", error=(500, {"error": "shard unavailable"}))
        # One document for every priced type, so a test can count requests and see that a run pays
        # for it at most once.
        self.server.get("/markets/prices", doc=MARKET_PRICES,
                        headers={"Last-Modified": http_date(-MARKET_PRICES_AGE)})

    def _ids_handler(self, call: Call):
        doc: dict[str, list[dict]] = {}
        for name in call.json:
            for bucket, ids in MARKET_IDS.get(name, {}).items():
                doc.setdefault(bucket, []).extend({"id": ident, "name": name} for ident in ids)
        return doc

    def _market_orders(self, call: Call):
        """Regional book, honouring the same query filters ESI does - a filter we forget to send
        (or start sending) changes the numbers instead of passing silently."""
        region = int(call.path.split("/")[2])
        rows = MARKET_ORDERS.get(region, [])
        for key in ("type_id", "location_id", "system_id"):
            if call.query.get(key) is not None:
                rows = [r for r in rows if str(r[key]) == call.query[key]]
        return rows, {"Last-Modified": http_date(-MARKET_BOOK_AGE[region]), "X-Pages": "1"}

    def _market_history(self, call: Call):
        rows = MARKET_HISTORY.get(int(call.path.split("/")[2]), [])
        wanted = call.query.get("type_id")
        if wanted is not None:
            rows = [r for r in rows if str(r["type_id"]) == wanted]
        return rows, {"Last-Modified": http_date(-86400)}

    # -- command runner ---------------------------------------------------------

    def run(self, argv: list[str]) -> tuple[int, str, str]:
        """Run the real CLI handler chain; returns (exit code, stdout, stderr)."""
        from eve_skills import cli
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(argv)
        return code if code is not None else 0, out.getvalue(), err.getvalue()
