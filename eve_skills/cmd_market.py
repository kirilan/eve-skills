"""market: live order-book prices for item types (public ESI, no login)."""

from __future__ import annotations

import csv
import io
import json
import sys
from dataclasses import dataclass


from . import esi as esi_mod, exports, market, render, sso


@dataclass
class MarketRow:
    """One quote plus the ids and traded-volume figures that explain where it came from."""

    quote: market.Quote
    region_id: int | None = None
    location_id: int | None = None
    history: market.HistoryStats | None = None


MARKET_COLUMNS = ["scope", "min sell", "max buy", "spread", "margin %", "sell vol", "buy vol",
                  "sells", "buys", "best sell at", "best buy at"]
MARKET_CSV_COLUMNS = ["type_id", "type_name", "scope", "region_id", "region_name", "location_id",
                      "location_name", "min_sell", "max_buy", "spread", "margin_pct", "sell_volume",
                      "buy_volume", "sell_orders", "buy_orders", "best_sell_location_id",
                      "best_sell_location_name", "best_sell_region_id", "best_sell_region_name",
                      "best_buy_location_id", "best_buy_location_name", "best_buy_region_id",
                      "best_buy_region_name", "regions_scanned", "regions_failed", "last_modified",
                      "expires", "age_seconds", "history_days", "history_rows", "history_total_volume",
                      "history_volume_per_day", "history_average_price", "history_newest_date",
                      # ESI's published reference, never a quote: appended so the header above keeps
                      # its meaning for anyone whose script already reads these columns by name.
                      "reference_average_price", "reference_adjusted_price",
                      "reference_last_modified", "reference_age_seconds"]


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
    rows = [MarketRow(market.quote(client, type_id, scope), scope.region_id, scope.location_id)
            for scope in scopes]
    if regions:
        rows.append(MarketRow(market.quote_cluster(client, type_id, regions)))
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
                history_days, prices) -> str:
    columns = list(MARKET_COLUMNS) + (["traded/day*", "traded total*"] if history_days else [])
    table_rows = []
    for row in rows:
        q = row.quote
        cells = [q.scope, render.isk(q.min_sell), render.isk(q.max_buy), render.isk(q.spread),
                 "-" if q.margin_pct is None else f"{q.margin_pct:.2f}",
                 f"{q.sell_volume:,}", f"{q.buy_volume:,}", str(q.sell_orders), str(q.buy_orders),
                 exports.name_or_id(names, q.best_sell_location),
                 exports.name_or_id(names, q.best_buy_location)]
        if history_days:
            cells += [f"{row.history.volume_per_day:,.0f}" if row.history else "-",
                      f"{row.history.total_volume:,}" if row.history else "-"]
        table_rows.append(cells)
    now = client.now().timestamp()
    lines = [f"{type_name} (id {type_id})", render.table(columns, table_rows)]
    # One freshness line per scope: scopes are fetched separately and can be minutes apart in age,
    # so a single stamp for the whole block would quietly claim they are all as old as the oldest.
    lines += [f"  {row.quote.scope}: {market.freshness_line(row.quote.meta, now)}" for row in rows]
    case = coverage.empty_book_case(type_id, rows)
    if case is not None:
        lines += market_empty_book_notes(case, coverage.scopes, names)
        if case in REFERENCE_BOOK_CASES:
            # `cmd_market` fetches `/markets/prices` for exactly these two cases and no others, so a
            # table is in hand whenever the footnote has earned one.
            lines += market_reference_notes(prices.reference(type_id), now)
    return "\n".join(lines)


def market_history_doc(row: MarketRow, names) -> dict | None:
    if row.history is None:
        return None
    h = row.history
    return {"region_id": h.region_id, "region_name": names.get(h.region_id), "days": h.days,
            "rows": h.rows, "total_volume": h.total_volume, "volume_per_day": h.volume_per_day,
            "average_price": h.average_price, "newest_date": h.newest_date}


def market_json(client: esi_mod.Esi, entries, names, history_days, prices) -> dict:
    """Machine-readable output: ids and names both, numbers unformatted, ages as real numbers."""
    now = client.now().timestamp()
    scopes = []
    for _type_id, _type_name, rows in entries:
        docs = []
        for row in rows:
            q = row.quote
            docs.append({
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
            })
        scopes.append(docs)
    return {
        "generated": market.iso_utc(now),
        "history_days": history_days,
        # The reference is per type, not per scope, and the key is present for every requested type -
        # including the ones with a live book, so a script never has to guess which kind of number it
        # is holding. null means either that this run had no reason to read ESI's price document at
        # all (every book had orders, or the empty ones were narrower than a cluster scan, where the
        # answer is a wider scope rather than a figure), or that the document has no row for the type.
        "types": [{"type_id": type_id, "name": type_name, "scopes": docs,
                   "reference": market_reference_doc(_reference_of(prices, type_id), now)}
                  for (type_id, type_name, _rows), docs in zip(entries, scopes)],
    }


def market_csv(client: esi_mod.Esi, entries, names, history_days, prices):
    now = client.now().timestamp()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(MARKET_CSV_COLUMNS)
    for type_id, type_name, rows in entries:
        # Empty cells when the document was never read or has no row: both mean "ESI publishes
        # nothing for this type", which is what a reader of these columns needs either way.
        reference = _reference_of(prices, type_id)
        for row in rows:
            q = row.quote
            h = row.history
            writer.writerow([
                type_id, type_name, q.scope, render.csv_cell(row.region_id), names.get(row.region_id) or "",
                render.csv_cell(row.location_id), names.get(row.location_id) or "",
                render.csv_cell(q.min_sell), render.csv_cell(q.max_buy), render.csv_cell(q.spread), render.csv_cell(q.margin_pct),
                q.sell_volume, q.buy_volume, q.sell_orders, q.buy_orders,
                render.csv_cell(q.best_sell_location), names.get(q.best_sell_location) or "",
                render.csv_cell(q.best_sell_region), names.get(q.best_sell_region) or "",
                render.csv_cell(q.best_buy_location), names.get(q.best_buy_location) or "",
                render.csv_cell(q.best_buy_region), names.get(q.best_buy_region) or "",
                q.regions_scanned, q.regions_failed, market.iso_utc(q.meta.last_modified) or "",
                market.iso_utc(q.meta.expires) or "",
                render.csv_cell(None if q.meta.last_modified is None else round(now - q.meta.last_modified, 1)),
                render.csv_cell(history_days if h else None), render.csv_cell(h.rows if h else None),
                render.csv_cell(h.total_volume if h else None), render.csv_cell(h.volume_per_day if h else None),
                render.csv_cell(h.average_price if h else None), h.newest_date if h else "",
                # Appended, never interleaved: the columns above mean what they always meant.
                render.csv_cell(reference.average_price if reference else None),
                render.csv_cell(reference.adjusted_price if reference else None),
                market.iso_utc(reference.meta.last_modified) if reference else "",
                render.csv_cell(None if reference is None or reference.meta.last_modified is None
                     else round(now - reference.meta.last_modified, 1)),
            ])
    sys.stdout.write(buf.getvalue())


def cmd_market(args):
    """Live order-book prices for item types: public ESI, no login and no stored character."""
    if args.history is not None and args.history < 1:
        raise RuntimeError("--history needs a positive number of days")
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    types = list(dict.fromkeys(market.resolve_type(client, spec) for spec in args.type))
    scopes = market_scope_list(client, args)
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
    # `/markets/prices` is one document listing every type CCP prices - over a megabyte - so it is
    # read at most once per run, and only when a footnote is going to quote it: an empty book in a
    # case where no wider order book exists to ask about (a measured vault type, or a whole-cluster
    # scan). Every narrower empty book gets the wider scope named instead of a figure, so neither
    # `--json` nor `--csv` pays for the document on its own any more; a run whose books all had
    # orders sends no request for it in any output format.
    prices = market.price_table(client) if coverage.needs_reference(entries) else None
    for line in warnings:
        print(f"warning: {line}", file=sys.stderr)
    if args.json:
        print(json.dumps(market_json(client, entries, names, args.history, prices), indent=2))
    elif args.csv:
        market_csv(client, entries, names, args.history, prices)
    else:
        blocks = [market_text(client, type_id, name, rows, names, coverage, args.history, prices)
                  for type_id, name, rows in entries]
        if args.history:
            blocks.append("* ESI traded volume is daily and one day behind, and only exists per region: "
                          "a hub row shows its region's trades, the global row shows none.")
        print("\n\n".join(blocks))
