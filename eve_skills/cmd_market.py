"""market: live order-book prices for item types (public ESI, no login)."""

from __future__ import annotations

import csv
import io
import json
import sys
from dataclasses import dataclass, field


from collections.abc import Callable, Mapping

from . import alphadata, esi as esi_mod, exports, market, render, sso, universe


@dataclass
class MarketRow:
    """One quote plus the ids and traded-volume figures that explain where it came from."""

    quote: market.Quote
    region_id: int | None = None
    location_id: int | None = None
    history: market.HistoryStats | None = None
    depth: dict[str, list[dict]] | None = None


# ---------------------------------------------------------------------------
# output columns: one vocabulary for the text table and for --csv
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _RowCtx:
    """Everything a cell can be read from: one type, one scope's answer, and the run around it.

    `seller` and `places` are the `--seller` half: the character whose two skills set the fee rates,
    and what creating an order at each place in this run would cost them. Both stay empty without the
    flag, which is exactly what makes every fee column print a dash instead of CCP's base case - there
    is no skill level to compute from, and printing 7.5% / 3% would present a stranger's character.
    """

    type_id: int
    type_name: str
    row: MarketRow
    names: dict[int, str]
    history_days: int | None
    reference: object | None      # ESI's published reference for this type, when one was read
    now: float
    seller: "Seller | None" = None
    places: Mapping[int, "ListingPlace"] = field(default_factory=dict)

    @property
    def q(self) -> market.Quote:
        return self.row.quote

    @property
    def h(self) -> market.HistoryStats | None:
        return self.row.history


def _text(value) -> str:
    """A raw figure as table text; a dash when there is nothing to show."""
    return "-" if value is None else str(value)


def _grouped(value) -> str:
    """A count with thousands separators, which is how this table has always printed volumes."""
    return "-" if value is None else f"{value:,}"


def _rate(value) -> str:
    """Units per day: whole units for a busy type, two decimals below ten a day - a carrier that sells
    eight times in ninety days trades 0.09 a day, and rounding that to `0` says nothing sold at all."""
    if value is None:
        return "-"
    return f"{value:,.0f}" if value >= 10 else f"{value:.2f}"


def _percent(value) -> str:
    return "-" if value is None else f"{value:.2f}"


def _age(seconds) -> str:
    """Age in the words `market` uses for it; a bare 192 in a column asks the reader to do sums."""
    return "-" if seconds is None else market.format_age(seconds)


def _rate_pct(value) -> str:
    """A fee as CCP states it - `3`, `1.8`, `3.375` - with no precision invented on the way out."""
    return "-" if value is None else f"{value:g}"


def _signed(value) -> str:
    """A difference in ISK, always with its sign: which way it goes is the whole point of the column."""
    return "-" if value is None else f"{value:+,.2f}"


@dataclass(frozen=True)
class MarketField:
    """One output column: the name `--fields` and the CSV header share, the header the text table puts
    over it, and how to render the cell in each.

    The CSV cell is always the raw value through `render.csv_cell` - empty when unknown, unformatted
    otherwise - so nothing a script parses ever gains a thousands separator. The table cell is
    formatted, and falls back to `str()` wherever that is already the right answer."""

    name: str
    header: str
    value: Callable[[_RowCtx], object]
    text: Callable[[_RowCtx], str] | None = None

    def csv(self, ctx: _RowCtx) -> str:
        return render.csv_cell(self.value(ctx))

    def shown(self, ctx: _RowCtx) -> str:
        if self.text is not None:
            return self.text(ctx)
        return _text(self.value(ctx))


# In CSV column order, which is the order `--csv` has always printed and so the order a script written
# against it reads. `header` is only ever the text table's wording for the same figure.
MARKET_FIELDS: tuple[MarketField, ...] = (
    MarketField("type_id", "id", lambda c: c.type_id),
    MarketField("type_name", "name", lambda c: c.type_name),
    MarketField("scope", "scope", lambda c: c.q.scope),
    MarketField("region_id", "region id", lambda c: c.row.region_id),
    MarketField("region_name", "region", lambda c: c.names.get(c.row.region_id)),
    MarketField("location_id", "station id", lambda c: c.row.location_id),
    MarketField("location_name", "station", lambda c: c.names.get(c.row.location_id)),
    MarketField("min_sell", "min sell", lambda c: c.q.min_sell, lambda c: render.isk(c.q.min_sell)),
    MarketField("max_buy", "max buy", lambda c: c.q.max_buy, lambda c: render.isk(c.q.max_buy)),
    MarketField("spread", "spread", lambda c: c.q.spread, lambda c: render.isk(c.q.spread)),
    MarketField("margin_pct", "margin %", lambda c: c.q.margin_pct, lambda c: _percent(c.q.margin_pct)),
    MarketField("sell_volume", "sell vol", lambda c: c.q.sell_volume, lambda c: _grouped(c.q.sell_volume)),
    MarketField("buy_volume", "buy vol", lambda c: c.q.buy_volume, lambda c: _grouped(c.q.buy_volume)),
    # Order counts stay ungrouped: that is what this table printed before `--fields` existed, and a busy
    # book's row would otherwise change appearance for no reason.
    MarketField("sell_orders", "sells", lambda c: c.q.sell_orders),
    MarketField("buy_orders", "buys", lambda c: c.q.buy_orders),
    # The two renderers genuinely differ here: CSV gets an id column and a name column, the table one
    # column that falls back to `id 12345`, because an unnamed station is still a place an order sits.
    MarketField("best_sell_location_id", "best sell station id", lambda c: c.q.best_sell_location),
    MarketField("best_sell_location_name", "best sell at", lambda c: c.names.get(c.q.best_sell_location),
                lambda c: exports.name_or_id(c.names, c.q.best_sell_location)),
    MarketField("best_sell_region_id", "best sell region id", lambda c: c.q.best_sell_region),
    MarketField("best_sell_region_name", "best sell region", lambda c: c.names.get(c.q.best_sell_region)),
    MarketField("best_buy_location_id", "best buy station id", lambda c: c.q.best_buy_location),
    MarketField("best_buy_location_name", "best buy at", lambda c: c.names.get(c.q.best_buy_location),
                lambda c: exports.name_or_id(c.names, c.q.best_buy_location)),
    MarketField("best_buy_region_id", "best buy region id", lambda c: c.q.best_buy_region),
    MarketField("best_buy_region_name", "best buy region", lambda c: c.names.get(c.q.best_buy_region)),
    MarketField("regions_scanned", "regions read", lambda c: c.q.regions_scanned),
    MarketField("regions_failed", "regions missed", lambda c: c.q.regions_failed),
    MarketField("last_modified", "book modified", lambda c: market.iso_utc(c.q.meta.last_modified)),
    MarketField("expires", "book expires", lambda c: market.iso_utc(c.q.meta.expires)),
    MarketField("age_seconds", "age",
                lambda c: None if c.q.meta.last_modified is None else round(c.now - c.q.meta.last_modified, 1),
                lambda c: _age(None if c.q.meta.last_modified is None else c.now - c.q.meta.last_modified)),
    MarketField("history_days", "history days", lambda c: c.history_days if c.h else None),
    MarketField("history_rows", "history rows", lambda c: c.h.rows if c.h else None),
    MarketField("history_total_volume", "traded total*", lambda c: c.h.total_volume if c.h else None,
                lambda c: _grouped(c.h.total_volume) if c.h else "-"),
    MarketField("history_volume_per_day", "traded/day*", lambda c: c.h.volume_per_day if c.h else None,
                lambda c: _rate(c.h.volume_per_day) if c.h else "-"),
    MarketField("history_average_price", "history avg*", lambda c: c.h.average_price if c.h else None,
                lambda c: render.isk(c.h.average_price) if c.h else "-"),
    MarketField("history_newest_date", "history newest*", lambda c: c.h.newest_date if c.h else None),
    # ESI's published reference, never a quote: these stay last so the columns above keep their meaning
    # for anyone whose script already reads them by name.
    MarketField("reference_average_price", "ESI average",
                lambda c: c.reference.average_price if c.reference else None,
                lambda c: render.isk(c.reference.average_price) if c.reference else "-"),
    MarketField("reference_adjusted_price", "ESI adjusted",
                lambda c: c.reference.adjusted_price if c.reference else None,
                lambda c: render.isk(c.reference.adjusted_price) if c.reference else "-"),
    MarketField("reference_last_modified", "ESI modified",
                lambda c: market.iso_utc(c.reference.meta.last_modified) if c.reference else None),
    MarketField("reference_age_seconds", "ESI age",
                lambda c: None if c.reference is None or c.reference.meta.last_modified is None
                else round(c.now - c.reference.meta.last_modified, 1),
                lambda c: _age(None if c.reference is None or c.reference.meta.last_modified is None
                                else c.now - c.reference.meta.last_modified)),
)
# The five columns `--seller` adds, kept out of MARKET_FIELDS on purpose. That tuple is the shipped CSV
# vocabulary: 616090f guarantees a run without the new flags writes byte-identical output, and a reader
# that indexes by position - the class of bug this file already documents once, a `history_days` figure
# read where `history_volume_per_day` was meant - breaks silently when five empty cells appear on every
# row. So the default column list grows only when a character was actually named to price as. Their names
# stay in the validation vocabulary either way, exactly like the history columns, which are always listed
# as valid and still refused without --history.
SELLER_FIELDS: tuple[MarketField, ...] = (
    MarketField("sales_tax_pct", "sales tax %", lambda c: sales_tax_of(c),
                lambda c: _rate_pct(sales_tax_of(c))),
    MarketField("broker_fee_pct", "broker fee %", lambda c: broker_fee_of(c),
                lambda c: _rate_pct(broker_fee_of(c))),
    MarketField("net_instant", "net instant", lambda c: net_instant_of(c),
                lambda c: render.isk(net_instant_of(c))),
    MarketField("net_listing", "net listing", lambda c: net_listing_of(c),
                lambda c: render.isk(net_listing_of(c))),
    # Listing minus instant: positive means the order earns more per unit than dumping the stack into
    # the best buy order would, negative says the seller pays that much to have the time.
    MarketField("net_edge", "listing edge", lambda c: net_edge_of(c),
                lambda c: _signed(net_edge_of(c))),
)
MARKET_FIELDS_BY_NAME = {col.name: col for col in (*MARKET_FIELDS, *SELLER_FIELDS)}
MARKET_CSV_COLUMNS = [field.name for field in MARKET_FIELDS]
SELLER_CSV_COLUMNS = [field.name for field in SELLER_FIELDS]

# What a mistyped `--fields` name is answered with: every column this command knows, including the five
# that need --seller. They are listed anyway for the same reason the history columns always were - the
# name is real and the run is what is short, and the refusal that follows names the missing flag rather
# than leaving the reader to conclude the column does not exist.
_FIELD_VOCABULARY = ", ".join(MARKET_CSV_COLUMNS + SELLER_CSV_COLUMNS)

# The columns that only mean something when a character was named to price as. `--fields` refuses them
# without --seller, exactly as it refuses a history column without --history - an all-empty fee column
# reads like "no fees", which is a wrong answer rather than a missing one.
SELLER_FIELD_NAMES = frozenset({"sales_tax_pct", "broker_fee_pct", "net_instant", "net_listing",
                                "net_edge"})

# The text table's default columns, exactly as they printed before `--fields` existed. The two history
# columns join them only under `--history`, and the `*` in their headers is what earns the legend.
MARKET_TEXT_COLUMNS = ["scope", "min_sell", "max_buy", "spread", "margin_pct", "sell_volume",
                       "buy_volume", "sell_orders", "buy_orders",
                       "best_sell_location_name", "best_buy_location_name"]
MARKET_TEXT_HISTORY_COLUMNS = ["history_volume_per_day", "history_total_volume"]

# The three figures the list-or-dump decision is made on. The two rates stay out of the table - sales
# tax is one number for the whole run and the broker fee one per station, both spelled out in the
# footer under the tables with the standing they were computed from.
MARKET_TEXT_SELLER_COLUMNS = ["net_instant", "net_listing", "net_edge"]


def select_fields(args) -> list[MarketField] | None:
    """The columns this run prints, in the order asked; None means today's defaults.

    One vocabulary for both renderers because `--csv` is read by column *position* once somebody has
    written a script against it - which is exactly how a `history_days` figure gets read as
    `history_volume_per_day` and turns into a wrong number nobody notices. Naming the columns makes the
    order the caller's in both outputs, and turns a mistyped name into an error listing what exists.

    Names match exactly (they are snake_case identifiers a script types, not prose) and repeats are
    honoured rather than folded: asking for a column twice is asking to see it twice."""
    if not args.fields:
        return None
    wanted = [part.strip() for part in args.fields.split(",")]
    names = [part for part in wanted if part]
    if not names:
        raise RuntimeError(f"--fields needs at least one column name; valid names: "
                           f"{_FIELD_VOCABULARY}")
    unknown = next((name for name in names if name not in MARKET_FIELDS_BY_NAME), None)
    if unknown is not None:
        raise RuntimeError(f"unknown --fields name '{unknown}'; valid names: {_FIELD_VOCABULARY}")

    needs_history = next((name for name in names if name.startswith("history_")), None)
    if needs_history is not None and not args.history:
        # Naming the column and getting an empty one is a small version of the misreading this flag
        # exists to prevent, and guessing a window would put a number nobody asked for in the output.
        raise RuntimeError(f"--fields {needs_history} reads ESI's daily history, which only --history "
                           f"DAYS asks for - add --history 30 (or another window) to fill it")

    needs_seller = next((name for name in names if name in SELLER_FIELD_NAMES), None)
    if needs_seller is not None and not args.seller:
        # The same trap as the history columns just above: naming the column and getting an empty one.
        # Here it is worse, because an empty fee column looks like a seller who pays nothing.
        raise RuntimeError(f"--fields {needs_seller} prices the sale through one character's skills - "
                           f"add --seller NAME to fill it")
    return [MARKET_FIELDS_BY_NAME[name] for name in names]


def text_fields(args) -> list[MarketField]:
    """The columns the text table prints, defaults included."""
    chosen = select_fields(args)
    if chosen is not None:
        return chosen
    # The seller columns join the defaults only under --seller; without it they would print empty, and
    # an empty column on the table somebody reads every day looks like a bug rather than a missing input.
    names = (list(MARKET_TEXT_COLUMNS)
             + (list(MARKET_TEXT_HISTORY_COLUMNS) if args.history else [])
             + (list(MARKET_TEXT_SELLER_COLUMNS) if args.seller else []))
    return [MARKET_FIELDS_BY_NAME[name] for name in names]


# ---------------------------------------------------------------------------
# --seller: what one named character keeps per unit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Seller:
    """One stored character's fee position: the two skills CCP charges with, and its standings.

    The levels come from `active_skill_level`, not `trained_skill_level`: a remap can leave a trained
    level inactive, and CCP bills on what the character actually has when the order is placed. A skill
    missing from ESI's list is untrained - measured 2026-09-13 against a stored character with neither,
    whose `/characters/{id}/skills` document does not contain either id at all - so it reads as level 0
    and the base rate rather than as unknown.

    `standings` keeps only `npc_corp` rows keyed by corporation id: that is the owner CCP's formula asks
    for, and ESI already reports the raw value, which is what the article demands ("The Broker Fees only
    take unmodified standings into account"). It is empty without the optional `standings` consent, and
    `standings_consent` records that difference - "no row for this corporation" and "never asked" are not
    the same claim about a fee.
    """

    character_id: int
    name: str
    accounting: int
    broker_relations: int
    standings: Mapping[int, float] = field(default_factory=dict)
    standings_consent: bool = True

    @property
    def sales_tax_pct(self) -> float:
        return market.sales_tax_pct(self.accounting)

    def standing_with(self, owner_corp_id: int | None) -> float | None:
        """Raw standing with a station's owning corporation; None when it is not known."""
        if owner_corp_id is None:
            return None
        return self.standings.get(owner_corp_id)

    def broker_fee_pct(self, owner_corp_id: int | None) -> float:
        return market.broker_fee_pct(self.broker_relations, self.standing_with(owner_corp_id))


@dataclass(frozen=True)
class ListingPlace:
    """One place an order could be created, and what CCP's terms make it cost this seller.

    `fee_pct` is None when ESI cannot support a figure - see `listing_places` for the two ways that
    happens - and then everything derived from it stays empty too. Falling back to CCP's 3% would be
    wrong in exactly the case that matters, an Upwell, where the rate is whatever its owner set and the
    Broker Relations skill does not apply at all.
    """

    location_id: int
    npc_station: bool = False
    owner_corp_id: int | None = None
    standing: float | None = None
    fee_pct: float | None = None


def _as_int(value) -> int | None:
    """An id out of an ESI document, or None for anything that is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    """A number out of an ESI document, or None for a missing or non-numeric field.

    Written out because the obvious `float(value or 0)` would turn a missing standing into 0.0, and 0.0
    is a real neutral standing that CCP's formula charges for."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_seller(client, spec: str | None) -> tuple[Seller, str | None]:
    """The character `--seller` names, with everything the fee formulas need from it.

    Returns the seller plus a hint line when that character has no `standings` consent. Missing optional
    consent is never an error here - same rule as `exports.targets`: the broker fee still comes out of
    Broker Relations, only without the standing term, and the caller says which of the two happened.

    At most two authenticated reads (skills, plus standings when consented), which is why this runs
    before the first order book: a name that is not a stored character should cost one request rather
    than thirty books - the same reasoning that settles the output columns before anything is fetched.
    """
    char_id = sso.resolve_character(spec)
    token = sso.get_access_token(char_id)
    doc = client.get(f"/characters/{char_id}/skills", token=token["access_token"])
    levels: dict[int, int] = {}
    for entry in doc.get("skills", []):
        ident = _as_int(entry.get("skill_id"))
        if ident is not None:
            levels[ident] = _as_int(entry.get("active_skill_level")) or 0
    name = token.get("character_name") or str(char_id)
    consent = sso.has_feature(token, "standings")
    standings: dict[int, float] = {}
    hint: str | None = None
    rows: list = []                       # no consent means never asked, so nothing to filter
    if consent:
        try:
            rows = client.get(f"/characters/{char_id}/standings", token=token["access_token"])
        except esi_mod.EsiError as exc:
            # Consent recorded but the document unavailable is not the same thing as never having
            # granted it, and neither is fatal here: a fee from Broker Relations alone is still worth
            # more to the caller than no fee, and `market`'s job is the price. The footer names which
            # of the two happened at each station ("no standing recorded with ..."), so nothing in the
            # output claims a standing this run did not read.
            rows = []
            hint = (f"{name}: standing unavailable ({exc}) - broker fees below are computed from "
                    f"Broker Relations alone.")
    else:
        hint = exports.hint(name, "standings")
    for row in rows:
        if not isinstance(row, dict) or row.get("from_type") != "npc_corp":
            continue
        ident, value = _as_int(row.get("from_id")), _as_float(row.get("standing"))
        if ident is not None and value is not None:
            standings[ident] = value
    seller = Seller(character_id=char_id, name=name,
                    accounting=levels.get(market.SKILL_ACCOUNTING, 0),
                    broker_relations=levels.get(market.SKILL_BROKER_RELATIONS, 0),
                    standings=standings, standings_consent=consent)
    return seller, hint


def listing_places(client, seller: Seller, locations) -> dict[int, ListingPlace]:
    """What creating one sell order at each of these places would cost `seller`.

    The place priced is wherever the row's cheapest ask sits: that is the order a seller has to beat,
    and for a station or hub scope it is the very station they are standing in. One public read per
    distinct place (`/universe/stations/{id}` needs no token), cached on disk like every other document.

    Two kinds of place get no rate:
      * An id above `universe.INT32_MAX` is item-derived, which is how this tool already tells a player
        structure from an NPC station (see that constant; verified against live ESI on 2026-09-08). An
        Upwell's broker fee is set by its owner and CCP states that "The Broker Relations skill does not
        apply on orders on Upwell structures", so there is nothing here to compute - and no public ESI
        document publishes it either (ESI's structure endpoints sit behind
        `esi-universe.read_structures.v1`, and they answer with orders and names, not fees).
      * A station lookup that fails leaves us unable to say which of the two we are pricing at all.
    """
    places: dict[int, ListingPlace] = {}
    for location_id in sorted({ident for ident in locations if ident is not None}):
        if location_id > universe.INT32_MAX:
            places[location_id] = ListingPlace(location_id)
            continue
        try:
            doc = client.get(f"/universe/stations/{location_id}")
        except esi_mod.EsiError:
            places[location_id] = ListingPlace(location_id)
            continue
        if not isinstance(doc, dict):
            places[location_id] = ListingPlace(location_id)
            continue
        owner = _as_int(doc.get("owner"))
        standing = seller.standing_with(owner)
        places[location_id] = ListingPlace(
            location_id, npc_station=True, owner_corp_id=owner, standing=standing,
            fee_pct=market.broker_fee_pct(seller.broker_relations, standing))
    return places


def _place(ctx: _RowCtx) -> ListingPlace | None:
    """The place this row's order would be created at, when the run knows what it is."""
    return ctx.places.get(ctx.q.best_sell_location)


def sales_tax_of(ctx: _RowCtx) -> float | None:
    return None if ctx.seller is None else ctx.seller.sales_tax_pct


def broker_fee_of(ctx: _RowCtx) -> float | None:
    place = _place(ctx)
    return None if place is None else place.fee_pct


def net_instant_of(ctx: _RowCtx) -> float | None:
    """Per unit sold straight into the best buy order: sales tax, and nothing else.

    "The broker fee is due on order creation for all non-immediate orders" (CCP), and filling a buy
    order creates none - which is the entire reason this column sits next to `net_listing` rather than
    being the same number twice."""
    if ctx.seller is None:
        return None
    return market.net_price(ctx.q.max_buy, ctx.seller.sales_tax_pct)


def net_listing_of(ctx: _RowCtx) -> float | None:
    """Per unit at the current minimum sell, after that station's broker fee and this seller's tax."""
    if ctx.seller is None:
        return None
    fee = broker_fee_of(ctx)
    if fee is None:
        return None
    return market.net_price(ctx.q.min_sell, fee + ctx.seller.sales_tax_pct)


def net_edge_of(ctx: _RowCtx) -> float | None:
    """Listing minus instant, per unit: positive means the order is the better exit."""
    listing, instant = net_listing_of(ctx), net_instant_of(ctx)
    if listing is None or instant is None:
        return None
    return round(listing - instant, 2)


def seller_notes(seller: Seller | None, places: Mapping[int, ListingPlace], names: dict[int, str],
                 hint: str | None = None, split_scopes: tuple[str, ...] = ()) -> list[str]:
    """The lines under the tables saying what the fee columns were computed from - and from not.

    A percentage with no provenance is exactly the sort of number this tool refuses to print bare: these
    depend on one character's skills and on a standing at one corporation, so both are named next to the
    figure each produced, per station. `split_scopes` names the rows whose best ask and best bid stand in
    different places - true for any region or cluster row where the two sides were not traded at the same
    station - because their edge compares two markets rather than one trade."""
    if seller is None:
        return []
    lines = [f"Seller {seller.name} (id {seller.character_id}): Accounting {seller.accounting} -> sales "
             f"tax {_rate_pct(seller.sales_tax_pct)}% of the sale price; Broker Relations "
             f"{seller.broker_relations} -> broker fee "
             f"{_rate_pct(market.broker_fee_pct(seller.broker_relations))}% before standings."]
    if hint is not None:
        lines.append(hint)
    priced = False
    for place in sorted(places.values(), key=lambda p: p.location_id):
        where = exports.name_or_id(names, place.location_id)
        if place.fee_pct is None:
            lines.append(f"{where}: a player structure sets its own broker fee and CCP exempts it from "
                         f"Broker Relations, so listing there cannot be priced; that cell stays empty.")
            continue
        priced = True
        owner = names.get(place.owner_corp_id) if place.owner_corp_id is not None else None
        if place.standing is None:
            why = ("no standings consent" if not seller.standings_consent
                   else "no standing recorded" + (f" with {owner}" if owner else ""))
        else:
            why = f"standing {place.standing:+.2f}" + (f" with {owner}" if owner else "")
        lines.append(f"{where}: broker fee {_rate_pct(place.fee_pct)}% ({why}).")
    lines.append("Selling straight into a buy order pays sales tax alone: CCP charges the broker fee on "
                 "order creation, so it belongs to the listing side only.")
    if split_scopes:
        lines.append("These rows price a listing at one place and an instant sale at another ("
                     + ", ".join(split_scopes) + "): each figure is what that exit pays where its own best "
                     "order stands, so the edge compares two markets - reaching that bid means moving the "
                     "goods there first.")
    if priced:
        lines.append("Not applied: the further 0.03 percentage points per standing point CCP takes for "
                     "the owning faction - ESI does not say which faction owns a station.")
    return lines


def market_seller_doc(seller: Seller | None, places: Mapping[int, ListingPlace], names: dict[int, str],
                      hint: str | None = None) -> dict | None:
    """The `--json` twin of `seller_notes`: the same facts as fields instead of prose.

    `faction_standing_applied` is false and stays false on purpose - see `market.broker_fee_pct` for the
    measurements behind it - so a script can tell that these fees are conservative rather than complete."""
    if seller is None:
        return None
    return {
        "character_id": seller.character_id,
        "name": seller.name,
        "accounting_level": seller.accounting,
        "broker_relations_level": seller.broker_relations,
        "sales_tax_pct": seller.sales_tax_pct,
        "broker_fee_before_standings_pct": market.broker_fee_pct(seller.broker_relations),
        "standings_consent": seller.standings_consent,
        "faction_standing_applied": False,
        "hint": hint,
        "listing_places": [{
            "location_id": place.location_id,
            "location_name": names.get(place.location_id),
            "npc_station": place.npc_station,
            "owner_corp_id": place.owner_corp_id,
            "owner_corp_name": names.get(place.owner_corp_id) if place.owner_corp_id else None,
            "standing": place.standing,
            "broker_fee_pct": place.fee_pct,
        } for place in sorted(places.values(), key=lambda p: p.location_id)],
    }


# ---------------------------------------------------------------------------
# naming a whole group or category
# ---------------------------------------------------------------------------

# How many types one run may price without being told to, and what a run is expected to cost. Measured
# against live ESI from this workspace on 2026-09-13: `market` reads its scopes one after another - only
# a --global scan fans out - so twelve cold station-scope books off Jita averaged 0.251 s each (median
# 0.263, range 0.172-0.367), and seven daily-history reads averaged 0.222 s. That is the serial round
# trip, not `exports.BOOK_SECONDS_PER_TYPE`'s 0.19 s per type, which counts a fan-out that keeps eight
# workers busy. A --global scan of all 70 market regions measured 12.2-36.2 s per type (mean 18.4 s over
# Tritanium, Plasmoids, PLEX, Large Skill Injectors and a cruiser module - the busiest book in the game
# is also the slowest read), so a cluster row is quoted as its own unit rather than seventy books.
# 200 types is fifty seconds at the cheapest scope: room for every planetary commodity tier at once (68)
# or any group a player actually shops, while `--category Module` - 3,873 listed types, about sixteen
# minutes of reading - refuses with a number instead of looking like a hang.
MAX_TYPES_PER_RUN = 200
BOOK_SECONDS_PER_SCOPE = 0.25
CLUSTER_SCAN_SECONDS = 18.0

# How many near names an error offers before the list stops being help and becomes an obstacle.
SUGGEST_LIMIT = 8


def _market_index() -> dict:
    """The local market type index, or an error naming the one command that builds it.

    Same two failures `system` tells apart for its census: nothing installed, and something installed
    that is not in this shape. Both become `RuntimeError` because `cli.main()` renders that as
    `error: ...` with exit 1; a bare `FileNotFoundError` would reach the user as a traceback."""
    try:
        document = alphadata.market_types()
    except FileNotFoundError:
        raise RuntimeError("no local market type index - run: eve-skills update-data") from None
    except ValueError as err:      # alphadata's own shape errors already name the fix
        raise RuntimeError(str(err)) from None
    return document


def _index_stamp(document: dict) -> str:
    """Which SDE build the refused names came from, so a stale index is visible in the error."""
    build = document.get("build")
    return f" (SDE build {build})" if build else ""


def _name_hint(table: dict, text: str, what: str) -> str:
    """What to type instead: near names first, then every name when there are few enough to print."""
    plural = f"{what[:-1]}ies" if what.endswith("y") else f"{what}s"   # "categories", not "categorys"
    near = sorted(key for key in table if text.lower() in key.lower())
    if near:
        return "; closest: " + ", ".join(f"'{key}'" for key in near[:SUGGEST_LIMIT])
    if len(table) <= 40:       # all 32 categories fit on a screen; the 814 listed groups do not
        return f"; the {plural} are: " + ", ".join(sorted(table))
    return f"; the index has {len(table)} {plural} and none of them contains '{text}'"


def _index_row(table: dict, spec, what: str, document: dict) -> tuple[str, dict]:
    """One `--group`/`--category` value as (key, row), matched by exact name or numeric id.

    Names match case-insensitively because these are long names typed by hand (`Specialized Commodities
    - Tier 3`) - the same way a positional type name matches in `market.resolve_type`. A disambiguated
    key (`Name (id)`, which `alphadata` builds for an English name used by two groups) stays reachable by
    id, the only spelling that tells such a pair apart."""
    text = str(spec).strip()
    if not text:
        raise RuntimeError(f"empty market {what} specifier")
    if text.isdigit():
        ident = int(text)
        for key, row in table.items():
            if row.get("id") == ident:
                return key, row
        raise RuntimeError(f"no market {what} with id {ident} in the local market type index"
                           f"{_index_stamp(document)}{_name_hint(table, text, what)}")
    for key, row in table.items():
        if key.lower() == text.lower():
            return key, row
    raise RuntimeError(f"no market {what} named '{text}' in the local market type index"
                       f"{_index_stamp(document)}{_name_hint(table, text, what)}")


def expanded_types(args) -> tuple[list[tuple[int, str]], list[str]]:
    """Every type `--group`/`--category` names, as (id, name), plus what named them.

    Costs no request: the index carries ids and names for every market-listed type, so naming a whole
    category is a dictionary walk - which also means a mistake costs nothing, worth having when the
    mistake is "no such group"."""
    if not (args.group or args.category):
        return [], []
    document = _market_index()
    pairs: list[tuple[int, str]] = []
    sources: list[str] = []
    for spec in args.group or []:
        key, row = _index_row(document["groups"], spec, "group", document)
        pairs += [(int(ident), name) for ident, name in row["types"].items()]
        sources.append(f'--group "{key}"')
    for spec in args.category or []:
        key, row = _index_row(document["categories"], spec, "category", document)
        for group in row["groups"]:
            pairs += [(int(ident), name) for ident, name in document["groups"][group]["types"].items()]
        sources.append(f'--category "{key}"')
    return pairs, sources


def _unique_types(pairs) -> list[tuple[int, str]]:
    """First spelling wins, in the order asked.

    A type named on the command line and also inside a `--group` is priced once, under the name the
    caller typed - which is also what makes `--group G --group G`, and a `--group` sitting inside a
    `--category` that was named too, idempotent instead of doubling the run."""
    seen: dict[int, str] = {}
    for ident, name in pairs:
        seen.setdefault(int(ident), name)
    return list(seen.items())

def book_request_count(type_count: int, scope_count: int, cluster: bool) -> int:
    """How many order books a run reads: one per type per scope, plus one whole-cluster scan per type."""
    return type_count * (scope_count + (1 if cluster else 0))


def book_phrase(type_count: int, scope_count: int, cluster: bool) -> str:
    """How many order books a run reads, with the shape of the run spelled out.

    The clarification is not decoration: a --global scan is one request per type that costs about
    eighteen seconds, so "3 order books to read, about 54s" would read as a contradiction."""
    books = book_request_count(type_count, scope_count, cluster)
    line = f"{books:,} order book{'s' if books != 1 else ''} to read"
    if cluster:
        line += (" (one whole-cluster scan per type, each covering every market region)" if not scope_count
                 else " (one per type per scope, plus a whole-cluster scan per type)")
    return line


def run_seconds(type_count: int, scope_count: int, cluster: bool, history_days=None) -> float:
    """What a fan-out of that size should cost, from the rates measured above.

    A daily-history read is one request per type per region and measures about the same as an order book,
    so `--history` adds one more scope-equivalent per type rather than a window's worth."""
    per_type = (scope_count + (1 if history_days else 0)) * BOOK_SECONDS_PER_SCOPE
    return type_count * (per_type + (CLUSTER_SCAN_SECONDS if cluster else 0.0))


def _guard_run_size(args, types, scope_count: int, cluster: bool) -> None:
    """Refuse a run far bigger than the command line can have meant.

    The limit is in types because that is what `--max-types` names; the message answers the question the
    caller actually has - how long - and prints the exact number that clears it, so raising the limit is
    one edit to the same command line rather than a guess."""
    limit = MAX_TYPES_PER_RUN if args.max_types is None else args.max_types
    if limit < 1:
        raise RuntimeError(f"--max-types needs a positive number of types, not {args.max_types}")
    if len(types) <= limit:
        return
    estimate = market.format_age(run_seconds(len(types), scope_count, cluster, args.history))
    hint = ("narrow it to one --group at a time" if (args.group or args.category)
            else "name fewer types, or name them by --group / --category instead")
    raise RuntimeError(f"{len(types)} types is more than the {limit} `market` prices without being asked "
                       f"twice: that is {book_phrase(len(types), scope_count, cluster)}, about {estimate} "
                       f"at the rate ESI answers here. {hint}, or say you meant it with "
                       f"--max-types {len(types)}")


def _fan_out_notice(types, scope_count: int, cluster: bool, sources, history_days=None) -> str:
    """The line printed before an expanded run starts, so a long read is not mistaken for a hang.

    Only for runs that did not list their own types: a command line with thirty names on it has already
    said how big it is, and printing for those would change what `market` has always printed."""
    origin = f" from {', '.join(sources)}" if sources else ""
    line = (f"{len(types)} type{'s' if len(types) != 1 else ''}{origin}: "
            f"{book_phrase(len(types), scope_count, cluster)}")
    seconds = run_seconds(len(types), scope_count, cluster, history_days)
    if seconds >= exports.NOTICE_MIN_SECONDS:
        # The threshold `inventory` uses for the same reason: under ten seconds a wait needs no
        # explaining, and above it silence looks like a hang.
        line += f"; about {market.format_age(seconds)} at this size"
    return line


def market_scope_list(client: esi_mod.Esi, args) -> list[market.Scope]:
    """Every station/region scope asked for; nothing at all asked means station-level Jita.

    Jita is what a trader means by "the price of Tritanium", and it is also the cheapest question
    ESI can be asked: one regional book instead of seventy. `--global` alone therefore gets no
    scope here at all - the cluster row is the answer that was asked for."""
    scopes: list[market.Scope] = []
    for spec in args.region or []:
        region_id, name = market.resolve_region(client, spec)
        scopes.append(market.Scope(region_id, name))
    for spec in args.hub or []:
        scopes.append(market.hub_scope(spec))
    if not scopes and not args.global_scopes:
        scopes.append(market.hub_scope("jita"))
    # The same region named twice ("The Forge" and 10000002) is one row, not two.
    return list({(s.region_id, s.system_id, s.location_id): s for s in scopes}.values())


def market_type_rows(client: esi_mod.Esi, args, type_id: int, scopes, regions) -> tuple[list[MarketRow], list[str]]:
    """Rows for one type, plus warnings about the parts of the cluster ESI did not answer for."""
    rows = []
    for scope in scopes:
        priced = market.quote(client, type_id, scope, depth=args.depth)
        if args.depth:
            quote, depth = priced
        else:
            quote, depth = priced, None
        rows.append(MarketRow(quote, scope.region_id, scope.location_id, depth=depth))
    if regions:
        priced = market.quote_cluster(client, type_id, regions, depth=args.depth)
        if args.depth:
            quote, depth = priced
        else:
            quote, depth = priced, None
        rows.append(MarketRow(quote, depth=depth))
    warnings = []
    for row in rows:
        if args.history and row.region_id is not None:
            # Two scopes in one region ask twice; ESI's day-long Expires makes the repeat free.
            # The cluster row has no single region, so it carries no traded volume at all.
            row.history = market.history_stats(client, row.region_id, type_id, args.history)
        if row.quote.regions_failed:
            total = row.quote.regions_scanned + row.quote.regions_failed
            warnings.append(f"{row.quote.scope}: {row.quote.regions_failed} of {total} regions did not "
                            f"answer; their orders are missing from the numbers above")
    return rows, warnings


def _book_is_empty(rows) -> bool:
    """True when no scope asked for had a single order on either side."""
    return all(row.quote.sell_orders + row.quote.buy_orders == 0 for row in rows)


# An empty book is only evidence about the books that were read, so its footnote is chosen by what
# this run actually covered - not by the fact that nothing came back. Two of the four cases below
# have earned the right to quote ESI's published reference, and they are exactly the two where no
# wider order book exists to ask for. The others get the wider question instead of a figure, because
# an item absent from one station is absent from one station, and pricing it from there is how a
# reader ends up believing a thinly traded module has no market at all.

# Asked for stations or systems: the whole regional book is still unasked, so name it.
EMPTY_BOOK_NOTE_STATION = (
    "No orders in any requested scope, which is a statement about those books and nothing else: an",
    "item nobody stocks at one station trades freely at the next. The wider book is still unasked -",
    "try {wider}, which reads the whole regional book ESI publishes.",
)

# Asked for whole regions, but not for every region: same reasoning one level up, and only the
# cluster scan is left to ask.
EMPTY_BOOK_NOTE_REGION = (
    "No orders in any requested scope, which is a statement about those regions and nothing else:",
    "a thinly traded module can be empty here and stocked one region over. The widest question ESI",
    "can be asked is still unasked - --global reads every market region's book.",
)

# Whole-cluster coverage: nothing wider does exist, because ESI publishes books per region only and
# has no endpoint above them. That is still not a cause, so the note declines to offer one.
EMPTY_BOOK_NOTE_CLUSTER = (
    "No orders in any book this run read, including a scan of every market region: ESI publishes",
    "order books per region only and has no global endpoint, so no wider book was left to ask.",
    "Nothing here says why: it says only that no region has an order out for this type right now.",
)

# The one type whose emptiness has a measured cause rather than an inferred one: no regional
# book can show it at all, whatever scope is asked (see `market.VAULT_TRADED_TYPE_IDS` for the id,
# the date and what exactly was measured).
EMPTY_BOOK_NOTE_VAULT = (
    "No orders in any requested scope: ESI publishes order books per region only, and no regional",
    "book can show this type - it trades on the account-wide vault market instead.",
    "There is no global order-book endpoint above them, so no wider book was left to ask.",
)

EMPTY_BOOK_NOTES = {"station": EMPTY_BOOK_NOTE_STATION, "region": EMPTY_BOOK_NOTE_REGION,
                    "cluster": EMPTY_BOOK_NOTE_CLUSTER, "vault": EMPTY_BOOK_NOTE_VAULT}
# The cases with nothing wider left to ask, and therefore the only ones that show a reference figure
# - which is what `cmd_market` decides the cost of `/markets/prices` on.
REFERENCE_BOOK_CASES = ("cluster", "vault")


@dataclass(frozen=True)
class MarketCoverage:
    """What one `market` run actually asked ESI, which is all an empty book can honestly answer for.

    ESI publishes order books per region and nothing above them, so a whole-cluster scan is the
    widest question there is. Anything short of it leaves a bigger book unasked, and then the honest
    footnote names that book rather than explaining the item away."""

    scopes: tuple[market.Scope, ...] = ()
    cluster_scanned: bool = False

    def empty_book_case(self, type_id: int, rows) -> str | None:
        """Which footnote this type has earned, or None when some scope had orders.

        `cmd_market` pays for `/markets/prices` on the strength of this predicate alone, and
        `market_text` prints its wording from it, so the two cannot drift apart."""
        if not _book_is_empty(rows):
            return None
        if type_id in market.VAULT_TRADED_TYPE_IDS:
            return "vault"
        if self.cluster_scanned:
            return "cluster"
        # A scope narrowed to one system or station still has its region's whole book above it; a
        # run that read whole regions already has only the cluster scan left.
        narrowed = any(scope.system_id or scope.location_id for scope in self.scopes)
        return "station" if narrowed else "region"

    def needs_reference(self, entries) -> bool:
        """True when some requested type's footnote will quote ESI's published reference."""
        return any(self.empty_book_case(type_id, rows) in REFERENCE_BOOK_CASES
                   for type_id, _name, rows in entries)


def _wider_book_hint(scopes, names) -> str:
    """The next-wider question this run did not ask, spelled the way it has to be typed back."""
    regions = list(dict.fromkeys(scope.region_id for scope in scopes
                                 if scope.system_id or scope.location_id))
    return " or ".join(f'--region "{names.get(rid) or rid}"' for rid in regions)


def market_empty_book_notes(case: str, scopes, names) -> list[str]:
    """Lines under a type's table for a book that came back empty wherever it was read."""
    note = EMPTY_BOOK_NOTES[case]
    if case == "station":
        note = tuple(line.format(wider=_wider_book_hint(scopes, names)) for line in note)
    return [f"  {line}" for line in note]


def market_reference_notes(reference, now: float) -> list[str]:
    """ESI's published figures for a type no order book answered, labelled as what they are not."""
    if reference is None:
        return ["  ESI's price document has no row for this type either, so there is no "
                "published price to show."]
    figures = []
    if reference.average_price is not None:
        figures.append(f"average {render.isk(reference.average_price)} ISK")
    if reference.adjusted_price is not None:
        figures.append(f"industry adjusted {render.isk(reference.adjusted_price)} ISK")
    if not figures:
        return ["  ESI's price document lists this type without a price, so there is "
                "no published figure to show."]
    return [f"  ESI's published reference for this type: {', '.join(figures)}",
            f"  {market.reference_freshness_line(reference.meta, now)}",
            "  A published figure, not a bid or an ask: nothing can be bought or sold at it."]


def market_reference_doc(reference, now: float) -> dict | None:
    """ESI's published reference as machine-readable data, stamped as what it is.

    `kind` exists because a consumer that reads `average_price` next to `min_sell` would otherwise
    have no way to tell a figure ESI publishes from an order someone placed."""
    if reference is None:
        return None
    return {"kind": "esi_published_reference",
            "average_price": reference.average_price,
            "adjusted_price": reference.adjusted_price,
            "last_modified": market.iso_utc(reference.meta.last_modified),
            "expires": market.iso_utc(reference.meta.expires),
            "age_seconds": None if reference.meta.last_modified is None
            else round(now - reference.meta.last_modified, 1)}


def _reference_of(prices, type_id: int):
    """One type's row in ESI's price document - None when this run had no reason to read it at all."""
    return None if prices is None else prices.reference(type_id)


def market_text(client: esi_mod.Esi, type_id: int, type_name: str, rows, names, coverage,
                history_days, prices, columns, seller: Seller | None = None,
                places: Mapping[int, ListingPlace] | None = None) -> str:
    """One type's block: the requested columns as a table, then what the numbers cannot say alone."""
    now = client.now().timestamp()
    reference = _reference_of(prices, type_id)
    cells = [_RowCtx(type_id, type_name, row, names, history_days, reference, now, seller,
                     places or {}) for row in rows]
    lines = [f"{type_name} (id {type_id})",
             render.table([field.header for field in columns],
                          [[field.shown(cell) for field in columns] for cell in cells])]
    # One freshness line per scope: scopes are fetched separately and can be minutes apart in age,
    # so a single stamp for the whole block would quietly claim they are all as old as the oldest.
    lines += [f"  {row.quote.scope}: {market.freshness_line(row.quote.meta, now)}" for row in rows]
    if any(row.depth is not None for row in rows):
        for row in rows:
            if row.depth is None:
                continue
            depth_rows = ([["sell", render.isk(order["price"]), f"{order['volume']:,}"]
                           for order in row.depth["sell"]] +
                          [["buy", render.isk(order["price"]), f"{order['volume']:,}"]
                           for order in row.depth["buy"]])
            lines.append(f"  {row.quote.scope} depth")
            lines.append(render.table(["side", "price", "volume"], depth_rows)
                         if depth_rows else "  (no orders)")
    case = coverage.empty_book_case(type_id, rows)
    if case is not None:
        lines += market_empty_book_notes(case, coverage.scopes, names)
        if case in REFERENCE_BOOK_CASES:
            # `cmd_market` fetches `/markets/prices` for exactly these two cases and no others, so a
            # table is in hand whenever the footnote has earned one.
            lines += market_reference_notes(reference, now)
    return "\n".join(lines)


def market_history_doc(row: MarketRow, names) -> dict | None:
    if row.history is None:
        return None
    h = row.history
    return {"region_id": h.region_id, "region_name": names.get(h.region_id), "days": h.days,
            "rows": h.rows, "total_volume": h.total_volume, "volume_per_day": h.volume_per_day,
            "average_price": h.average_price, "newest_date": h.newest_date}


def market_json(client: esi_mod.Esi, entries, names, history_days, prices,
                seller: Seller | None = None, places: Mapping[int, ListingPlace] | None = None,
                seller_hint: str | None = None) -> dict:
    """Machine-readable output: ids and names both, numbers unformatted, ages as real numbers."""
    now = client.now().timestamp()
    scopes = []
    for _type_id, _type_name, rows in entries:
        docs = []
        for row in rows:
            q = row.quote
            # Same `_RowCtx` the columns read, so a JSON fee and a table fee cannot disagree.
            ctx = _RowCtx(_type_id, _type_name, row, names, history_days,
                          _reference_of(prices, _type_id), now, seller, places or {})
            doc = {
                "scope": q.scope,
                "region_id": row.region_id,
                "region_name": names.get(row.region_id),
                "location_id": row.location_id,
                "location_name": names.get(row.location_id),
                "min_sell": q.min_sell,
                "max_buy": q.max_buy,
                "spread": q.spread,
                "margin_pct": q.margin_pct,
                "sell_volume": q.sell_volume,
                "buy_volume": q.buy_volume,
                "sell_orders": q.sell_orders,
                "buy_orders": q.buy_orders,
                "best_sell_location_id": q.best_sell_location,
                "best_sell_location_name": names.get(q.best_sell_location),
                "best_sell_region_id": q.best_sell_region,
                "best_sell_region_name": names.get(q.best_sell_region),
                "best_buy_location_id": q.best_buy_location,
                "best_buy_location_name": names.get(q.best_buy_location),
                "best_buy_region_id": q.best_buy_region,
                "best_buy_region_name": names.get(q.best_buy_region),
                "regions_scanned": q.regions_scanned,
                "regions_failed": q.regions_failed,
                "last_modified": market.iso_utc(q.meta.last_modified),
                "expires": market.iso_utc(q.meta.expires),
                "age_seconds": None if q.meta.last_modified is None else round(now - q.meta.last_modified, 1),
                "history": market_history_doc(row, names),
            }
            if row.depth is not None:
                doc["depth"] = row.depth
            if seller is not None:
                # These five keys are absent rather than null without --seller. This command's JSON is a
                # contract too: `net_listing` present-and-null would tell a script "priced, outcome
                # unknown" when nothing was priced because no character was named - and 616090f shipped
                # the guarantee that an unflagged run writes what it always wrote. Null inside a run that
                # did name one keeps a single meaning: this row's listing place cannot support a fee.
                doc.update({
                    "sales_tax_pct": sales_tax_of(ctx),
                    "broker_fee_pct": broker_fee_of(ctx),
                    "net_instant": net_instant_of(ctx),
                    "net_listing": net_listing_of(ctx),
                    "net_edge": net_edge_of(ctx),
                })
            docs.append(doc)
        scopes.append(docs)
    doc = {
        "generated": market.iso_utc(now),
        "history_days": history_days,
    }
    if seller is not None:
        # Whose skills every fee above came from, the broker fee of every station this run priced, and
        # the re-login hint when standings consent is missing. The key stays out entirely without
        # --seller, for the reason given on the per-scope keys.
        doc["seller"] = market_seller_doc(seller, places or {}, names, seller_hint)
    # The reference is per type, not per scope, and the key is present for every requested type -
    # including the ones with a live book, so a script never has to guess which kind of number it
    # is holding. null means either that this run had no reason to read ESI's price document at
    # all (every book had orders, or the empty ones were narrower than a cluster scan, where the
    # answer is a wider scope rather than a figure), or that the document has no row for the type.
    doc["types"] = [{"type_id": type_id, "name": type_name, "scopes": docs,
                     "reference": market_reference_doc(_reference_of(prices, type_id), now)}
                    for (type_id, type_name, _rows), docs in zip(entries, scopes)]
    return doc


def market_csv(client: esi_mod.Esi, entries, names, history_days, prices, columns,
               seller: Seller | None = None, places: Mapping[int, ListingPlace] | None = None):
    """Every row of this run as CSV, in the column order `columns` gives.

    Cells go through `render.csv_cell`, which leaves a number exactly as `str()` prints it - so the
    default column list reproduces the bytes this function has always written, and only the order and
    the selection of columns ever change."""
    now = client.now().timestamp()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([field.name for field in columns])
    for type_id, type_name, rows in entries:
        # Empty cells when the document was never read or has no row: both mean "ESI publishes
        # nothing for this type", which is what a reader of these columns needs either way.
        reference = _reference_of(prices, type_id)
        for row in rows:
            cell = _RowCtx(type_id, type_name, row, names, history_days, reference, now, seller,
                           places or {})
            writer.writerow([field.csv(cell) for field in columns])
    sys.stdout.write(buf.getvalue())


def cmd_market(args):
    """Live order-book prices for item types: public ESI, no login and no stored character."""
    if args.history is not None and args.history < 1:
        raise RuntimeError("--history needs a positive number of days")
    if args.depth is not None and args.depth < 1:
        raise RuntimeError("--depth needs a positive number of orders")
    # The columns are settled before anything is fetched: a mistyped name should cost nothing rather
    # than thirty seconds of order books, and one list decides both renderers, so the table can never
    # disagree with the CSV about what this run prints.
    chosen = select_fields(args)
    if args.json and chosen is not None:
        raise RuntimeError("--fields does not apply to --json: the JSON output carries every field, "
                           "which is the reason to ask for JSON")
    if chosen is not None:
        columns = chosen
    elif args.csv:
        # The default CSV vocabulary grows only when a character was named: without --seller the header
        # is exactly the one shipped at 616090f, five empty trailing cells and all. `--fields` is the
        # exception - naming a fee column is asking for it by name, and that path refuses outright
        # without --seller rather than printing an empty cell to be misread as "no fees".
        columns = list(MARKET_FIELDS) + (list(SELLER_FIELDS) if args.seller else [])
    else:
        columns = text_fields(args)
    expanded, sources = expanded_types(args)   # local index only - a mistyped group costs no request
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    # Resolved here, before the first book, for the same reason the columns are settled above: a name
    # that is not one of the stored characters should cost one skills request rather than thirty order
    # books. `market` itself stays a public command - without --seller no token is ever asked for.
    seller, seller_hint = resolve_seller(client, args.seller) if args.seller else (None, None)
    types = _unique_types([market.resolve_type(client, spec) for spec in args.type] + expanded)
    if not types:
        raise RuntimeError("market needs something to price: name a type (Tritanium or 34), or give "
                           "--group / --category and let the local market type index name them")
    scopes = market_scope_list(client, args)
    # Refused before the first order book - and before even the region list a --global scan needs - so
    # an accidental run of thousands costs one `/universe/ids` call, not thousands.
    _guard_run_size(args, types, len(scopes), args.global_scopes)
    if sources:
        print(_fan_out_notice(types, len(scopes), args.global_scopes, sources, args.history),
              file=sys.stderr)
    regions = market.market_regions(client) if args.global_scopes else []
    # What this run covered, recorded once: it is what decides both the wording of an empty-book
    # footnote and whether `/markets/prices` is worth reading at all.
    coverage = MarketCoverage(tuple(scopes), cluster_scanned=bool(regions))
    entries, warnings = [], []
    for type_id, type_name in types:
        rows, row_warnings = market_type_rows(client, args, type_id, scopes, regions)
        entries.append((type_id, type_name, rows))
        warnings += row_warnings
    ids = {i for _tid, _name, rows in entries for row in rows
           for i in (row.region_id, row.location_id, row.quote.best_sell_location,
                     row.quote.best_buy_location, row.quote.best_sell_region,
                     row.quote.best_buy_region, row.history.region_id if row.history else None)
           if i is not None}
    names = esi_mod.resolve_names(client, ids) if ids else {}

    # A broker fee belongs to the station an order is created at, and the place a listing has to beat
    # is where the cheapest ask already sits - so each row's `best_sell_location` is what gets priced.
    places = listing_places(client, seller, [row.quote.best_sell_location
                                             for _tid, _name, rows in entries for row in rows]) \
        if seller is not None else {}
    owners = {place.owner_corp_id for place in places.values() if place.owner_corp_id is not None}
    if owners:
        # A second name batch, and only under --seller. Corporation ids are far below int32, so unlike
        # an item-sized structure id they cannot poison the request (`universe.INT32_MAX`), and a corp's
        # name never changes, so this is one disk-cached call for every later run too. The owner is
        # printed by name because "standing with 1000035" is not something a player can check.
        names.update(esi_mod.resolve_names(client, owners))

    # `/markets/prices` is one document listing every type CCP prices - over a megabyte - so it is
    # read at most once per run, and only when a footnote is going to quote it: an empty book in a
    # case where no wider order book exists to ask about (a measured vault type, or a whole-cluster
    # scan). Every narrower empty book gets the wider scope named instead of a figure, so neither
    # `--json` nor `--csv` pays for the document on its own any more; a run whose books all had
    # orders sends no request for it in any output format.
    prices = market.price_table(client) if coverage.needs_reference(entries) else None
    for line in warnings:
        print(f"warning: {line}", file=sys.stderr)
    if seller_hint is not None and (args.json or args.csv):
        # Same rule as every other optional-consent hint: stdout stays parseable, so the re-login line
        # goes to stderr. The text table has room for it in its footer instead.
        print(seller_hint, file=sys.stderr)
    if args.json:
        print(json.dumps(market_json(client, entries, names, args.history, prices, seller, places,
                                     seller_hint), indent=2))
    elif args.csv:
        market_csv(client, entries, names, args.history, prices, columns, seller, places)
    else:
        blocks = [market_text(client, type_id, name, rows, names, coverage, args.history, prices,
                              columns, seller, places) for type_id, name, rows in entries]
        # The fee provenance is run-wide - one character's skills, one rate per station - so it goes
        # under every table rather than repeated on each, like the legend below it.
        # A region or cluster row can hold its cheapest ask and its best bid at two different stations, and
        # then `listing edge` is not one trade but a comparison between two markets. Both figures are still
        # exactly what that exit pays where its own best order stands, so the columns stay filled - but only
        # the footer can say the two sides are not the same deal.
        split = sorted({row.quote.scope for _id, _name, rows in entries for row in rows
                        if row.quote.best_sell_location is not None
                        and row.quote.best_buy_location is not None
                        and row.quote.best_sell_location != row.quote.best_buy_location})
        notes = seller_notes(seller, places, names, seller_hint, tuple(split))
        if notes:
            blocks.append("\n".join(notes))
        # The legend follows the stars actually printed rather than `--history` itself: a run can fetch
        # history and select neither starred column, and then it has nothing to explain.
        if any("*" in field.header for field in columns):
            blocks.append("* ESI traded volume is daily and one day behind, and only exists per region: "
                          "a hub row shows its region's trades, the global row shows none.")
        print("\n\n".join(blocks))
