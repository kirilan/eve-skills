"""orders: the live market orders of stored characters and their corporations."""

from __future__ import annotations

import csv
import io
import json
import sys


from . import esi as esi_mod, exports, market, orders, render


ORDERS_OPEN_COLUMNS = ["owner", "type", "side", "price", "remaining/total", "filled", "station",
                       "region", "issued", "expires in", "escrow"]
# Only corporation rows can fill these, so they appear with --corp and nowhere else.
ORDERS_CORP_COLUMNS = ["division", "issued by"]
ORDERS_CLOSED_COLUMNS = ["owner", "type", "side", "price", "state", "filled/total", "station",
                         "region", "issued", "expires"]
ORDERS_CSV_COLUMNS = ["order_id", "owner_key", "owner_name", "is_buy", "is_corporation", "type_id",
                      "type_name", "region_id", "region_name", "location_id", "location_name",
                      "price", "volume_total", "volume_remain", "filled", "state", "issued",
                      "duration", "expires", "escrow", "min_volume", "range", "wallet_division",
                      "issued_by", "issued_by_name"]


def _stamp(value) -> str:
    """An ESI timestamp as a compact date; "-" when there is none to show."""
    moment = render.parse_opt(value)
    return "-" if moment is None else moment.strftime("%b %d %H:%M")


def order_doc(order, names: dict[int, str]) -> dict:
    """One order as machine-readable data: raw ids *and* their names, numbers unformatted."""
    return {
        "order_id": order.order_id,
        "owner_key": order.owner_key,
        "owner_name": order.owner_name,
        "is_buy": order.is_buy,
        "is_corporation": order.is_corporation,
        "type_id": order.type_id,
        "type_name": names.get(order.type_id),
        "region_id": order.region_id,
        "region_name": names.get(order.region_id),
        "location_id": order.location_id,
        "location_name": names.get(order.location_id),
        "price": order.price,
        "volume_total": order.volume_total,
        "volume_remain": order.volume_remain,
        "filled": order.filled,
        "state": order.state,
        "issued": order.issued,
        "duration": order.duration,
        "expires": order.expires,
        "escrow": order.escrow,
        "min_volume": order.min_volume,
        "range": order.range,
        "wallet_division": order.wallet_division,
        "issued_by": order.issued_by,
        "issued_by_name": names.get(order.issued_by) if order.issued_by else None,
    }


def orders_row(order, names: dict[int, str], now, *, closed: bool, corp: bool) -> list[str]:
    """One table row; `closed` switches to the history columns, `corp` adds the two only
    corporation rows can fill."""
    # A personal book can hold an order funded from the corporation wallet; without the marker its
    # ISK reads as the member's own. Under --corp every row is the corporation's, so it says nothing.
    side = "buy" if order.is_buy else "sell"
    row = [order.owner_name, exports.name_or_id(names, order.type_id),
           f"{side} (corp)" if order.is_corporation and not corp else side, render.isk(order.price)]
    if closed:
        row += [order.state, f"{order.filled:,}/{order.volume_total:,}"]
    else:
        pct = "-" if not order.volume_total else f"{order.filled / order.volume_total * 100:.0f}%"
        row += [f"{order.volume_remain:,}/{order.volume_total:,}", pct]
    row += [exports.name_or_id(names, order.location_id), exports.name_or_id(names, order.region_id),
            _stamp(order.issued)]
    if closed:
        row.append(_stamp(order.expires))
    else:
        # An open order past its expiry is ESI lagging behind the market, not a negative countdown.
        expires = render.parse_opt(order.expires)
        if expires is None:
            left = "-"
        elif expires <= now:
            left = "due"
        else:
            left = render.format_duration((expires - now).total_seconds())
        row += [left, render.isk(order.escrow)]
    if corp:
        row += [render.csv_cell(order.wallet_division), exports.name_or_id(names, order.issued_by)]
    return row


def orders_totals(items) -> dict:
    """ISK at stake in the live book: what the remaining sells would raise, and what the buys hold.

    Escrow is all ESI offers for a buy order's cost and it is optional, so `buy_escrow_missing`
    counts the buys that reported none - the total then says what it leaves out."""
    sells = [o for o in items if not o.is_buy]
    buys = [o for o in items if o.is_buy]
    return {"sell_orders": len(sells),
            "sell_isk": sum(o.price * o.volume_remain for o in sells),
            "buy_orders": len(buys),
            "buy_escrow_isk": sum(o.escrow or 0.0 for o in buys),
            "buy_escrow_missing": sum(1 for o in buys if o.escrow is None)}


def orders_totals_line(totals: dict) -> str:
    unreported = ("" if not totals["buy_escrow_missing"]
                  else f", escrow reported for {totals['buy_orders'] - totals['buy_escrow_missing']}")
    return (f"sell book {render.isk(totals['sell_isk'])} ISK ({totals['sell_orders']} order(s))   "
            f"buy escrow {render.isk(totals['buy_escrow_isk'])} ISK ({totals['buy_orders']} order(s){unreported})")


def order_owners(client: esi_mod.Esi, args, chars) -> tuple[list, list[str]]:
    """One OwnerOrders per owner to show, plus a warning line per owner ESI did not answer for.

    Corporation books are fetched once per corporation: stored characters can be colleagues, and a
    second token would report the identical rows a second time as if they were new."""
    owners, failures, fetched = [], [], set()
    for tok, public in chars:
        if args.corp:
            corp_id = exports.corp_of(public)
            if corp_id in fetched:
                continue
            if corp_id:
                fetched.add(corp_id)
        cname = public.get("name") or tok.get("character_name") or str(tok["character_id"])
        try:
            owners.append(orders.fetch_corporation(client, tok, public) if args.corp
                          else orders.fetch_character(client, tok))
        except (esi_mod.EsiError, RuntimeError) as err:
            # One character's refusal never hides the others' books; orders.py keeps the owner out
            # of its messages precisely so the reporter can name it in its own words.
            failures.append(f"{cname}: {err}")
    return owners, failures


def orders_side_filter(args) -> bool | None:
    """True for buys only, False for sells only, None for the whole book (both flags = no filter)."""
    if args.buy and not args.sell:
        return True
    if args.sell and not args.buy:
        return False
    return None


def cmd_orders(args):
    """The order book of every stored character that consented - or of their corporations."""
    if args.watch:
        from . import cmd_watch   # local: cmd_watch imports this module at import time
        return cmd_watch.cmd_orders_watch(args)
    if args.limit is not None and args.limit < 1:
        raise RuntimeError("--limit needs a positive number of rows")
    feature = orders.CORPORATION_FEATURE if args.corp else orders.CHARACTER_FEATURE
    scope = orders.CORPORATION_SCOPE if args.corp else orders.CHARACTER_SCOPE
    client, chars, hints = exports.targets(args, [(feature, scope)])
    owners, failures = order_owners(client, args, chars)
    for line in failures:
        print(f"warning: {line}", file=sys.stderr)
    type_id = market.resolve_type(client, args.type)[0] if args.type else None
    side = orders_side_filter(args)
    book = [o for owner in owners for o in (owner.history if args.closed else owner.open)
            if (type_id is None or o.type_id == type_id) and (side is None or o.is_buy == side)]
    # Newest first - ESI's own order is oldest-first, the opposite of what a glance at one's own
    # orders looks for. Its UTC stamps sort correctly as written.
    book.sort(key=lambda o: o.issued, reverse=True)
    shown = book[:args.limit] if args.limit else book
    ids = {i for o in shown for i in (o.type_id, o.region_id, o.location_id, o.issued_by) if i}
    names = esi_mod.resolve_names(client, ids) if ids else {}
    # --csv sends the hint lines to stderr inside targets(); --json needs the same treatment here
    # so that stdout stays parseable either way.
    if args.json:
        for line in hints:
            print(line, file=sys.stderr)
        print(json.dumps({
            "generated": market.iso_utc(client.now().timestamp()),
            "owner_kind": "corporation" if args.corp else "character",
            "book": "closed" if args.closed else "open",
            "filters": {"type_id": type_id,
                        "side": None if side is None else ("buy" if side else "sell")},
            "owners": [{"owner_key": o.owner_key, "owner_name": o.owner_name,
                        "history_ok": o.history_ok} for o in owners],
            "matched": len(book),
            "shown": len(shown),
            "orders": [order_doc(o, names) for o in shown],
            # A closed book has nothing still at stake; the derived states are the answer there.
            "totals": None if args.closed else orders_totals(book),
        }, indent=2))
        return
    if args.csv:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(ORDERS_CSV_COLUMNS)
        for order in shown:
            doc = order_doc(order, names)
            # bool as 1/0 like every other CSV in this tool.
            writer.writerow(["" if doc[c] is None else
                             (int(doc[c]) if c in ("is_buy", "is_corporation") else doc[c])
                             for c in ORDERS_CSV_COLUMNS])
        sys.stdout.write(buf.getvalue())
        return
    blocks = list(hints)
    columns = (ORDERS_CLOSED_COLUMNS if args.closed
               else ORDERS_OPEN_COLUMNS + (ORDERS_CORP_COLUMNS if args.corp else []))
    now = client.now()
    if not shown:
        blocks.append("(no closed orders in ESI's ~90-day history)" if args.closed
                      else "(no open orders)")
    else:
        rows = [orders_row(o, names, now, closed=args.closed, corp=args.corp) for o in shown]
        lines = [render.table(columns, rows)]
        if args.closed:
            lines += ["", "* derived state: ESI has no filled value, so an expired order with nothing "
                          "left is shown as filled and one with volume left as expired.",
                      "* ESI has no cancellation timestamp, so a cancelled order's expires is the date "
                      "it would have run to."]
        else:
            if len(book) > len(shown):
                lines.append(f"{len(book) - len(shown)} of {len(book)} matching order(s) hidden by "
                             f"--limit; the totals cover all of them")
            lines += ["", orders_totals_line(orders_totals(book))]
        if not args.corp and any(o.is_corporation for o in shown):
            lines += ["", "* (corp) marks an order funded from a corporation wallet; that ISK is not "
                          "the character's own."]
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
