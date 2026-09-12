"""Live market prices from official ESI, reduced to the numbers a trader asks for.

Kept apart from the CLI so the same reductions serve `market` today and order monitoring later.
Two kinds of staleness meet here and neither is hidden: ESI regenerates a regional order book
every 5 minutes (reported by `Last-Modified`), while `/markets/{region}/history` is daily and one
day behind. Printing either without its age would present a cached answer as live.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from . import esi as esi_mod, paths, storage

# ESI rebuilds the regional book on this cadence (its `Expires` is 300s later); quoted in every
# freshness line so a reader can tell "just refreshed" from "about to move".
BOOK_REFRESH_SECONDS = 300

# One document with a row for every type CCP prices, stamped with its own `Last-Modified` - an hour
# apart from its `Expires` on live ESI today, so it moves on ESI's schedule, not the book's five
# minutes. Read lazily and once per run: it is over a megabyte, so paying for it on a run whose
# books were all non-empty would be a waste nobody asked for.
PRICES_PATH = "/markets/prices"

# Region ids inside this band are k-space plus Pochven and have markets. Everything else
# /universe/regions lists is wormhole space (11000001+, "A-R00001"), abyssal (12000001+, "ADR01"),
# VR-01..05 (14000001+) or GPMR-01 - none of which trade: probing one answers with an empty book,
# so including them would add ~44 pointless requests to every whole-cluster scan. Verified against
# live ESI on 2026-09-07 (70 regions in the band, 114 listed).
MARKET_REGION_MIN = 10_000_000
MARKET_REGION_MAX = 11_000_000

# The one empty book whose cause is measured rather than guessed. PLEX trades on the account-wide
# vault market, which belongs to no region's order book: measured live on 2026-09-07 against
# compatibility date 2026-08-18, `GET /markets/{region}/orders?type_id=44992` answered `[]` in all
# 70 market regions, while `/markets/prices` carried the type the same minute (`average_price`
# 4574918.36, `adjusted_price` 0.0). Only this id has been measured across the whole cluster, so it
# is the only one the tool may explain that way: an empty book for any other type says nothing more
# than that the books which were read are empty.
VAULT_TRADED_TYPE_IDS: frozenset[int] = frozenset({44_992})   # PLEX

# Shape version of quotes.json - the reduced order-book figures this module is willing to reuse.
# Bump it when the record layout below changes: a reader that does not recognise the document
# discards it wholesale rather than mis-reading an old field as a new one.
QUOTE_CACHE_VERSION = 1
QUOTE_DOC_NAME = "quotes.json"
QUOTE_LOCK_NAME = "quotes.lock"


@dataclass(frozen=True)
class Hub:
    """One of the five trade hubs, with the ids each market endpoint needs.

    Verified against live ESI on 2026-09-07: `GET /universe/stations/{id}` for the name and its
    `system_id`, then that system's constellation for the `region_id` (neither station nor system
    records carries a region id). All five agreed with the long-standing values below, so none is
    a guess. Region ids are what `/markets/{region_id}/orders` takes; the station id is what makes
    a hub quote mean *that* station rather than its whole region.
    """

    key: str
    label: str
    station_id: int
    region_id: int
    system_id: int


HUBS: dict[str, Hub] = {hub.key: hub for hub in (
    Hub("jita", "Jita 4-4", 60003760, 10000002, 30000142),
    Hub("amarr", "Amarr", 60008494, 10000043, 30002187),
    Hub("dodixie", "Dodixie", 60011866, 10000032, 30002659),
    Hub("rens", "Rens", 60004588, 10000030, 30002510),
    Hub("hek", "Hek", 60005686, 10000042, 30002053),
)}


@dataclass(frozen=True)
class Scope:
    """What one row of a quote describes: a region, optionally narrowed to one system or station.

    ESI only ever answers per region, so `(system_id, location_id)` are filters applied to the
    regional book rather than endpoints; `label` is what the user reads in the scope column. A
    whole-cluster view is not a Scope - see `quote_cluster`.
    """

    region_id: int
    label: str
    system_id: int | None = None
    location_id: int | None = None


@dataclass(frozen=True)
class Quote:
    """One scope's order book, reduced.

    Prices are None when that side has no orders at all. Never 0.0: a missing sell side would read
    as "somebody is selling it for nothing", which is a different and very wrong statement.
    """

    scope: str
    min_sell: float | None = None
    max_buy: float | None = None
    sell_volume: int = 0
    buy_volume: int = 0
    sell_orders: int = 0
    buy_orders: int = 0
    best_sell_location: int | None = None
    best_buy_location: int | None = None
    best_sell_region: int | None = None
    best_buy_region: int | None = None
    regions_scanned: int = 1
    regions_failed: int = 0
    meta: esi_mod.Meta = field(default_factory=esi_mod.Meta)

    @property
    def spread(self) -> float | None:
        """min sell - max buy; None while either side is empty, because a gap is not a spread."""
        if self.min_sell is None or self.max_buy is None:
            return None
        return self.min_sell - self.max_buy

    @property
    def margin_pct(self) -> float | None:
        """Buy at max buy, sell at min sell: profit as a percentage of the buy price."""
        if self.min_sell is None or not self.max_buy:
            return None
        return (self.min_sell - self.max_buy) / self.max_buy * 100.0


@dataclass(frozen=True)
class CachedFigure:
    """One scope's two extreme prices for one type, with the stamps that license believing them.

    Only what a valuation reads is kept - never the orders themselves. Five hundred types of raw
    regional book is megabytes; this reduction is tens of kilobytes and is the same answer the rows
    would give until ESI says the book moves again, which the response states as `expires`.
    """

    min_sell: float | None
    max_buy: float | None
    last_modified: float | None   # when ESI generated the book these figures came from
    expires: float                # ESI's own `Expires`: the end of that claim

    @property
    def meta(self) -> esi_mod.Meta:
        """The stamps as a Meta, so a cached figure folds with a freshly read one identically."""
        return esi_mod.Meta(expires=self.expires, last_modified=self.last_modified)

    def live_at(self, now: float) -> bool:
        """True while ESI's stated expiry still vouches for these figures. Past it the entry is not
        merely old - it is unusable, and the caller has to read the book again."""
        return self.expires > now


@dataclass(frozen=True)
class Preflight:
    """What a valuation is about to cost, known before any of it is fetched.

    Handed to the caller at the one moment it can still be said out loud: a minute without output
    reads as a hang, and "518 types, 518 books" turns it into an understood wait."""

    types: int      # distinct types to price
    cached: int     # figures the local cache still vouches for
    fetches: int    # order books that still have to be read


@dataclass(frozen=True)
class BookFigures:
    """Cheapest ask and richest bid per type at one scope, and what reading them cost this run.

    `meta` describes every figure in the maps together - their oldest `Last-Modified`, so a total is
    never described as fresher than its stalest input, whether that input came off disk or off ESI.
    A type absent from both maps is not worth zero; it is worth nothing ESI would say."""

    min_sell: dict[int, float] = field(default_factory=dict)
    max_buy: dict[int, float] = field(default_factory=dict)
    meta: esi_mod.Meta = field(default_factory=esi_mod.Meta)
    fetched: int = 0     # books this run asked ESI for, whether they answered or not
    cached: int = 0      # types priced from the local cache instead of a request
    failed: int = 0      # books that did not answer; their types stay unpriced and uncached

    @property
    def answered(self) -> int:
        """Books that spoke, on disk or on the wire. An empty one still said something."""
        return self.cached + self.fetched - self.failed

@dataclass(frozen=True)
class HistoryStats:
    """Traded volume from `/markets/{region}/history` - daily, one day behind, region-level only."""

    region_id: int
    days: int
    rows: int
    total_volume: int
    volume_per_day: float
    average_price: float | None
    newest_date: str | None
    meta: esi_mod.Meta = field(default_factory=esi_mod.Meta)


@dataclass(frozen=True)
class Reference:
    """CCP's published reference price for one type - a figure, never a quote.

    `average_price` is ESI's rolling average of what the type has been selling for;
    `adjusted_price` is CCP's industry reference, the number behind invention and reaction costs.
    Both keys are optional in ESI's schema, so each stays None when the row omits it - while 0.0
    stays 0.0, because that is a value CCP really publishes (PLEX carries `adjusted_price: 0.0`
    today, not an absent key). The document lists no orders and is refreshed on ESI's own schedule.
    Neither number is a bid or an ask, nothing can be bought or sold at it, and it never belongs in
    `Quote.min_sell` / `Quote.max_buy`. It exists because some types have no order book ESI will
    show at all (see `price_table`), and "here is what CCP publishes" beats a row of dashes.
    """

    type_id: int
    average_price: float | None = None
    adjusted_price: float | None = None
    meta: esi_mod.Meta = field(default_factory=esi_mod.Meta)


@dataclass(frozen=True)
class PriceTable:
    """The whole `/markets/prices` document, indexed by type id.

    ESI serves every priced type in one response (tens of thousands of rows), so it is read as a
    table and looked up in - never rescanned per type - and a run that prices five items pays for
    exactly one request."""

    rows: Mapping[int, dict] = field(default_factory=dict)
    meta: esi_mod.Meta = field(default_factory=esi_mod.Meta)

    def reference(self, type_id: int) -> Reference | None:
        """One type's row, or None when ESI's price document does not carry that type."""
        row = self.rows.get(type_id)
        if row is None:
            return None
        # `_price` turns an omitted key into None and a published 0.0 into 0.0; that distinction is
        # the difference between "CCP gives no industry reference" and "the reference is zero".
        return Reference(type_id=type_id,
                         average_price=_price(row.get("average_price")),
                         adjusted_price=_price(row.get("adjusted_price")),
                         meta=self.meta)


def book_path(region_id: int, type_id: int) -> str:
    """The regional book for one type.

    `type_id` is not optional in practice: without it the endpoint serves the whole region (Forge
    alone: 1000 rows over 410 pages), which no price check needs and no rate limit deserves."""
    return f"/markets/{region_id}/orders?order_type=all&type_id={type_id}"


def _id(value) -> int | None:
    """Id as an int, or None for anything that is not one - a filter must not crash on a row."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _price(value) -> float | None:
    """Order price as a float, or None when the row carries nothing numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reduce(label: str, rows: Sequence[tuple[int | None, dict]], meta: esi_mod.Meta, *,
            regions_scanned: int = 1, regions_failed: int = 0) -> Quote:
    """Fold order rows into a Quote; every row carries the region id it was read from.

    Volumes are `volume_remain`, not `volume_total`: what is still on the book is what a buyer or
    seller can actually act on. Each extreme keeps its own location and region because that is the
    actionable half of the number - in a cluster scan it says which station to fly to."""
    sells: list[tuple[float, int | None, int | None]] = []
    buys: list[tuple[float, int | None, int | None]] = []
    sell_volume = buy_volume = 0
    for region_id, order in rows:
        price = _price(order.get("price"))
        if price is None:
            continue  # ESI always prices a row; one without a number cannot be quoted
        side = (price, _id(order.get("location_id")), region_id)
        if order.get("is_buy_order"):
            buys.append(side)
            buy_volume += int(order.get("volume_remain") or 0)
        else:
            sells.append(side)
            sell_volume += int(order.get("volume_remain") or 0)
    cheapest = min(sells, key=lambda side: side[0]) if sells else None
    richest = max(buys, key=lambda side: side[0]) if buys else None
    return Quote(
        scope=label,
        min_sell=cheapest[0] if cheapest else None,
        max_buy=richest[0] if richest else None,
        sell_volume=sell_volume,
        buy_volume=buy_volume,
        sell_orders=len(sells),
        buy_orders=len(buys),
        best_sell_location=cheapest[1] if cheapest else None,
        best_buy_location=richest[1] if richest else None,
        best_sell_region=cheapest[2] if cheapest else None,
        best_buy_region=richest[2] if richest else None,
        regions_scanned=regions_scanned,
        regions_failed=regions_failed,
        meta=meta,
    )


def _scope_rows(rows: Sequence[dict], scope: Scope) -> list[dict]:
    """The orders physically inside one scope.

    ESI only ever answers per region, so `location_id`/`system_id` are filters applied here rather
    than endpoints: the same regional rows have to read as "the price at Jita 4-4" or as "The Forge"
    depending on what was asked. A station filter beats a system one because a hub scope carries both
    ids and the narrower question is the one the user typed."""
    if scope.location_id is not None:
        return [row for row in rows if _id(row.get("location_id")) == scope.location_id]
    if scope.system_id is not None:
        return [row for row in rows if _id(row.get("system_id")) == scope.system_id]
    return list(rows)


def book_sides(rows: Sequence[dict]) -> tuple[float | None, float | None]:
    """Cheapest ask and richest bid in one book.

    A side with no orders stays None rather than 0.0: a missing sell side printed as zero reads as
    "somebody is selling it for nothing", which is a different and very wrong statement."""
    min_sell = max_buy = None
    for row in rows:
        price = _price(row.get("price")) if isinstance(row, dict) else None
        if price is None:
            continue
        if row.get("is_buy_order"):
            if max_buy is None or price > max_buy:
                max_buy = price
        elif min_sell is None or price < min_sell:
            min_sell = price
    return min_sell, max_buy


def quote(client: esi_mod.Esi, type_id: int, scope: Scope) -> Quote:
    """One scope's book.

    EVE's own order `range` rules are modelled in neither direction, deliberately: a region-range
    buy order placed elsewhere in the region is not pulled into a station scope, and a narrow-range
    order sitting at the scope is not dropped from it. Reproducing that geometry (plus structure
    edges) is appraisal territory, out of scope here; what this reports is the orders physically
    filed at the scope, which is the honest reading of "the price at Jita 4-4".
    """
    rows, meta = client.get_meta(book_path(scope.region_id, type_id))
    picked = _scope_rows(rows, scope)
    return _reduce(scope.label, [(scope.region_id, o) for o in picked], meta)


def quote_cluster(client: esi_mod.Esi, type_id: int, regions: Sequence[tuple[int, str]]) -> Quote:
    """Whole-cluster quote from `market_regions()` output, folded into one view.

    A cluster scan is ~70 regional books, so two things are recorded rather than smoothed over:
    the best prices keep their region id (the trade is *somewhere*, and "somewhere" is the point of
    the scan) and freshness is the oldest `Last-Modified` of the batch. Regions that failed are
    counted, never raised - a partial cluster is still worth reading, and the caller warns."""
    paths = {region_id: book_path(region_id, type_id) for region_id, _name in regions}
    fetched = client.get_many_meta(list(paths.values()))
    region_of = {path: region_id for region_id, path in paths.items()}
    rows: list[tuple[int | None, dict]] = []
    metas: list[esi_mod.Meta] = []
    failed = 0
    for path, item in fetched.items():
        if isinstance(item, Exception):
            failed += 1
            continue
        payload, meta = item
        metas.append(meta)
        rows.extend((region_of[path], order) for order in payload)
    scanned = len(paths) - failed
    label = f"global ({scanned} regions)" if not failed else f"global ({scanned}/{len(paths)} regions)"
    return _reduce(label, rows, esi_mod.fold_meta(metas), regions_scanned=scanned, regions_failed=failed)


def history_stats(client: esi_mod.Esi, region_id: int, type_id: int, days: int) -> HistoryStats | None:
    """Daily traded volume for the last `days` days a region reported; None when it never traded.

    Read with `get_meta` rather than the paginated helper: this endpoint answers with every day it
    has in one response and sends no `X-Pages`, so appending `page=` would be guessing at a
    behaviour that was not verified. Rows are sorted before the window is taken - live ESI returns
    them oldest-first, and the newest days are what "last N days" must mean if that ever changes.
    Per-day averages divide by the rows actually present, so a type with a short history is not
    made to look quieter than it was."""
    rows, meta = client.get_meta(f"/markets/{region_id}/history?type_id={type_id}")
    dated = sorted((row for row in rows if row.get("date")), key=lambda row: str(row["date"]))
    window = dated[-days:] if days > 0 else dated
    if not window:
        return None
    total = sum(int(row.get("volume") or 0) for row in window)
    prices = [float(row["average"]) for row in window if row.get("average") is not None]
    return HistoryStats(
        region_id=region_id,
        days=days,
        rows=len(window),
        total_volume=total,
        volume_per_day=total / len(window),
        average_price=sum(prices) / len(prices) if prices else None,
        newest_date=str(window[-1]["date"]),
        meta=meta,
    )


def price_table(client: esi_mod.Esi) -> PriceTable:
    """ESI's published reference prices for every type, indexed once by type id.

    Fetched through the cache-aware transport because the document is over a megabyte and moves on
    ESI's schedule rather than the book's:
    a second lookup in the same run, or in another run before ESI's `Expires`, must not cost another
    request. Rows without a usable `type_id` are dropped rather than crashing the index - one odd row
    must not remove the reference price of every other type."""
    rows, meta = client.get_meta(PRICES_PATH)
    index: dict[int, dict] = {}
    for row in rows:
        ident = _id(row.get("type_id"))
        if ident is not None:
            index[ident] = row
    return PriceTable(rows=index, meta=meta)


def reference_price(client: esi_mod.Esi, type_id: int) -> Reference | None:
    """One type's published reference price; None when ESI's price document has no row for it.

    Costs a whole-document request, so ask for several types through one `price_table()`."""
    return price_table(client).reference(type_id)


def market_regions(client: esi_mod.Esi) -> list[tuple[int, str]]:
    """Every region that has a market, as (id, name), sorted by name."""
    ids = [region for region in (int(i) for i in client.get("/universe/regions"))
           if MARKET_REGION_MIN < region < MARKET_REGION_MAX]
    names = esi_mod.resolve_names(client, set(ids))
    return sorted(((region, names.get(region, f"region {region}")) for region in ids),
                  key=lambda pair: (pair[1].lower(), pair[0]))


def _resolve(client: esi_mod.Esi, spec: str, bucket: str, what: str) -> tuple[int, str]:
    """(id, name) for a specifier given as a numeric id or an exact name.

    Only the requested `bucket` of `/universe/ids` is read: `["Tritanium"]` also matches a
    *character* called Tritanium, and taking the first non-empty bucket would happily price a
    player. Names are matched exactly because that is what ESI does - no fuzzy matching whose
    surprises would have to be explained in a price report.
    """
    text = str(spec).strip()
    if not text:
        raise RuntimeError(f"empty {what} specifier")
    if text.isdigit():
        ident = int(text)
        # Best effort: an id is quotable even when ESI declines to name it.
        return ident, esi_mod.resolve_names(client, {ident}).get(ident) or f"{what} {ident}"
    hits = client.post("/universe/ids", [text]).get(bucket) or []
    for hit in hits:
        if str(hit.get("name", "")).lower() == text.lower():
            return int(hit["id"]), str(hit["name"])
    if len(hits) == 1:
        return int(hits[0]["id"]), str(hits[0].get("name") or text)
    raise RuntimeError(f"no {what} named '{text}' - ESI matches exact names only; try the numeric id")


def resolve_type(client: esi_mod.Esi, spec) -> tuple[int, str]:
    """Item type as (id, name) from a type id or an exact type name."""
    return _resolve(client, spec, "inventory_types", "type")


def resolve_region(client: esi_mod.Esi, spec) -> tuple[int, str]:
    """Region as (id, name) from a region id or an exact region name."""
    return _resolve(client, spec, "regions", "region")


def resolve_system(client: esi_mod.Esi, spec) -> tuple[int, str]:
    """Solar system as (id, name) from a system id or an exact system name.

    Same exact-name rule as the type and region resolvers, for the same reason: ESI's search endpoint
    returns neighbours, and quietly building in `Jita` because someone typed `Jit` is not a decision
    this tool gets to make on their behalf."""
    return _resolve(client, spec, "systems", "system")


def hub_scope(spec: str) -> Scope:
    """A named trade hub, narrowed to its station rather than its whole region."""
    hub = HUBS.get(spec.strip().lower())
    if hub is None:
        raise RuntimeError(f"unknown hub '{spec}' - choices: {', '.join(HUBS)}")
    return Scope(hub.region_id, f"{hub.label} (station)", hub.system_id, hub.station_id)


# ---------------------------------------------------------------------------
# quote cache: what ESI's own expiry licenses believing
# ---------------------------------------------------------------------------

def figure_key(scope: Scope, type_id: int) -> str:
    """The cache key for one figure: the scope's filter *and* the type.

    Position carries the meaning - region, station, system, type - and `-` marks "this scope does
    not filter on that". A hub scope reads one station's orders while a region scope reads the whole
    region, so a Jita figure must never answer an Amarr question, nor a region-wide figure a station
    one. Keying on the type alone would do exactly that."""
    return ":".join(str(value) if value is not None else "-" for value in
                    (scope.region_id, scope.location_id, scope.system_id, type_id))


def quote_doc_path(cache_dir: str | None = None) -> str:
    """Where the quote cache lives: `quotes.json` beside `names.json` and `types.json`."""
    return os.path.join(cache_dir or paths.cache_dir(), QUOTE_DOC_NAME)


def _stamp(value) -> float | None:
    """An epoch stamp read back from a record; a bool is not a timestamp, and neither is text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_figure(value) -> CachedFigure | None:
    """A cached record, or None when it is not one.

    A record with no stated expiry is discarded rather than served: without `Expires` nothing says
    how long its figures may still be called current, and "we hope it is fresh" is the one sentence
    this tool never prints."""
    if not isinstance(value, dict):
        return None
    expires = _stamp(value.get("expires"))
    if expires is None:
        return None
    return CachedFigure(min_sell=_price(value.get("min_sell")), max_buy=_price(value.get("max_buy")),
                        last_modified=_stamp(value.get("last_modified")), expires=expires)


def _figure_record(figure: CachedFigure) -> dict:
    """A record as written to disk - the same four fields `_parse_figure` accepts back."""
    return {"min_sell": figure.min_sell, "max_buy": figure.max_buy,
            "last_modified": figure.last_modified, "expires": figure.expires}


def read_quote_cache(cache_dir: str | None = None) -> dict[str, CachedFigure]:
    """quotes.json as key -> figure; unreadable, foreign or corrupt reads as empty.

    Every record goes through `_parse_figure`, the same judgement a live payload gets, so a
    half-written or hand-edited file costs a refetch of the affected keys instead of an
    AttributeError in the middle of a valuation."""
    try:
        with open(quote_doc_path(cache_dir), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}     # absent, unreadable and unparseable are one thing here: nothing cached
    if not isinstance(raw, dict) or raw.get("version") != QUOTE_CACHE_VERSION:
        return {}
    entries = raw.get("figures")
    if not isinstance(entries, dict):
        return {}
    out: dict[str, CachedFigure] = {}
    for key, value in entries.items():      # json gives back str keys, always
        figure = _parse_figure(value)
        if figure is not None:
            out[key] = figure
    return out


def publish_figures(entries: Mapping[str, CachedFigure], cache_dir: str | None = None,
                    now: float | None = None) -> None:
    """Add this run's figures to quotes.json without losing anybody else's.

    The re-read happens inside the lock on purpose, exactly as in `universe._publish`: a second
    process valuing a different character may have cached other scopes since this one read the file,
    and publishing our own view of the document would silently drop them. Anything already past its
    stated expiry is dropped here instead of written back - it can never be served again, so keeping
    it would only grow the file. Nothing is written when there is nothing to add."""
    if not entries:
        return
    moment = time.time() if now is None else now
    path = quote_doc_path(cache_dir)
    with storage.file_lock(os.path.join(os.path.dirname(path) or ".", QUOTE_LOCK_NAME)):
        stored = read_quote_cache(cache_dir)
        stored.update(entries)
        storage.atomic_write_json(path, {
            "version": QUOTE_CACHE_VERSION,
            "figures": {key: _figure_record(figure)
                        for key, figure in stored.items() if figure.live_at(moment)},
        })


def _read_books(client: esi_mod.Esi, type_ids: Sequence[int], scope: Scope) -> dict[int, object]:
    """One fan-out of regional books keyed by type id: `(rows, Meta)`, or that book's Exception.

    A type missing from the answer reads as a failure rather than as an empty book - "nobody has
    ordered it here" and "we never got an answer" are different statements, and only the first is
    allowed to be cached."""
    if not type_ids:
        return {}
    path_of = {type_id: book_path(scope.region_id, type_id) for type_id in type_ids}
    answers = client.get_many_meta(list(path_of.values()))
    return {type_id: answers.get(path_of[type_id]) for type_id in type_ids}


def book_figures(client: esi_mod.Esi, type_ids: Iterable[int], scope: Scope, *,
                 cache_dir: str | None = None, preflight=None,
                 now: float | None = None) -> BookFigures:
    """Cheapest ask and richest bid per type at one scope, reading only what disk cannot answer.

    ESI regenerates a regional book on `BOOK_REFRESH_SECONDS`' cadence and says so in the response's
    own `Expires`, so reusing the reduction until that stated moment is not staleness - it is the
    freshness contract the endpoint publishes. Past it the entry is refetched, and those ids only,
    which is what makes a second look at the same holding instant without ever showing a figure ESI
    has already disowned.

    A book that fails to answer leaves its type unpriced and out of the cache, so one timeout costs
    a retry on the next run rather than five minutes of silence. Expiry is judged against the local
    clock, exactly as `esi.Esi` judges its own in-process cache."""
    wanted = sorted({int(ident) for ident in type_ids})
    moment = time.time() if now is None else now
    stored = read_quote_cache(cache_dir)
    hits: dict[int, CachedFigure] = {}
    misses: list[int] = []
    for type_id in wanted:
        entry = stored.get(figure_key(scope, type_id))
        if entry is not None and entry.live_at(moment):
            hits[type_id] = entry
        else:
            misses.append(type_id)
    if preflight is not None:
        preflight(Preflight(types=len(wanted), cached=len(hits), fetches=len(misses)))

    min_sell: dict[int, float] = {}
    max_buy: dict[int, float] = {}
    for type_id, hit in hits.items():
        if hit.min_sell is not None:
            min_sell[type_id] = hit.min_sell
        if hit.max_buy is not None:
            max_buy[type_id] = hit.max_buy

    metas = [hit.meta for hit in hits.values()]
    learned: dict[str, CachedFigure] = {}
    failed = 0
    for type_id, answer in _read_books(client, misses, scope).items():
        if not isinstance(answer, tuple):
            failed += 1     # the Exception, or no answer at all: unpriced, and never cached
            continue
        payload, meta = answer
        if not isinstance(payload, list):
            failed += 1     # not the list of orders asked for, so no statement about the book
            continue
        low, high = book_sides(_scope_rows(payload, scope))
        metas.append(meta)
        if low is not None:
            min_sell[type_id] = low
        if high is not None:
            max_buy[type_id] = high
        if meta.expires is not None and meta.expires > moment:
            learned[figure_key(scope, type_id)] = CachedFigure(
                min_sell=low, max_buy=high, last_modified=meta.last_modified, expires=meta.expires)

    publish_figures(learned, cache_dir=cache_dir, now=moment)
    return BookFigures(min_sell=min_sell, max_buy=max_buy, meta=esi_mod.fold_meta(metas),
                       fetched=len(misses), cached=len(hits), failed=failed)


def format_age(seconds: float) -> str:
    """Age of a payload, keeping seconds for as long as they still matter.

    `render.format_duration` collapses 192 s to "3m"; against a book that refreshes every 5
    minutes the difference between 3m and 4m is the whole message, so market ages print their own."""
    total = int(max(seconds, 0))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    if total < 86400:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    return f"{total // 86400}d {(total % 86400) // 3600:02d}h"


def iso_utc(epoch: float | None) -> str | None:
    """Epoch as the `...Z` stamp every other machine-readable field in this tool uses."""
    return None if epoch is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def freshness_line(meta: esi_mod.Meta, now_epoch: float) -> str:
    """How current a Quote is, in the words printed under its table."""
    if meta.last_modified is None:
        # No Last-Modified means ESI answered without saying when it generated anything. Saying
        # "just now" would be a fabrication, so the line reports what is missing instead.
        return "freshness: unknown (ESI sent no Last-Modified)"
    stamp = time.strftime("%H:%M:%SZ", time.gmtime(meta.last_modified))
    return (f"as of {stamp} ({format_age(now_epoch - meta.last_modified)} ago; "
            f"ESI refreshes the book every {BOOK_REFRESH_SECONDS // 60} min)")


def reference_freshness_line(meta: esi_mod.Meta, now_epoch: float) -> str:
    """How current a Reference is - the same shape as `freshness_line`, with its own cadence.

    Quoting the book's five minutes here would imply this number moves with the orders; ESI stamps it
    separately, and a reader has to be able to tell which of the two ages they are looking at."""
    if meta.last_modified is None:
        return "freshness: unknown (ESI sent no Last-Modified)"
    stamp = time.strftime("%H:%M:%SZ", time.gmtime(meta.last_modified))
    return (f"freshness: as of {stamp}, {format_age(now_epoch - meta.last_modified)} ago; "
            f"ESI stamps this document on its own schedule, not with the order books")
