"""Market orders of stored characters and their corporations, fetched and normalised.

ESI answers orders in two places - the live book and about 90 days of history - and neither one
says what a trader needs to know about a closed order: the history enum has only `cancelled` and
`expired`, so a sale that completed and an order that lapsed with stock still on the book arrive as
the same value. The remaining volume is the only evidence of which happened, so that derivation
lives here once instead of being reinvented by the `orders` command and again by the watcher.

This module only fetches and normalises: no disk writes, no module-level state, no price
comparison (that is `market`'s job). Both callers therefore read identical rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone

from . import esi as esi_mod, exports, render, sso

# The consent features (see sso.OPTIONAL_SCOPES) behind each endpoint pair. The scope constants are
# what commands pass to exports.targets(); the feature names are what the hint lines quote back.
CHARACTER_FEATURE = "orders"
CORPORATION_FEATURE = "corp-orders"
CHARACTER_SCOPE = "esi-markets.read_character_orders.v1"
CORPORATION_SCOPE = "esi-markets.read_corporation_orders.v1"
ROLE_SCOPE = "esi-characters.read_corporation_roles.v1"

# The in-game roles ESI requires on /corporations/{id}/orders. Both work: Accountant is the one a
# director normally hands out, Trader the one a market-oriented member holds.
ORDER_ROLES = ("Accountant", "Trader")

OPEN = "open"
UTC = timezone.utc


class OrderAccess(esi_mod.EsiError):
    """ESI refused an order endpoint: no consent, or consent without the in-game role."""


@dataclass(frozen=True)
class Order:
    """One order, normalised across character/corporation and open/history rows.

    Ids stay raw; names are resolved by whoever renders the row. `state` is `open` for a live book
    row and one of `filled | expired | cancelled` for a history row (see `_terminal_state`)."""

    order_id: int
    owner_key: str
    owner_name: str
    is_buy: bool
    type_id: int
    region_id: int
    location_id: int
    price: float
    volume_total: int
    volume_remain: int
    issued: str
    duration: int
    state: str
    escrow: float | None = None
    min_volume: int | None = None
    range: str | None = None
    issued_by: int | None = None
    wallet_division: int | None = None
    # ESI only reports this on character endpoints, where a member's book can hold an order funded
    # from the corporation wallet - the ISK at stake in such a row is not the member's own.
    is_corporation: bool = False

    @property
    def expires(self) -> str | None:
        """`issued` + `duration` days as an ISO UTC stamp, or None when the row has no lifetime.

        ESI reports `duration` 0 on rows that were never a timed order - the immediate/off-market
        entries that turn up in history. Adding zero days would date their expiry at the moment of
        issue and make every one of them look like it lapsed instantly, so the answer is "there is
        no expiry date", which callers render as "-". There is no closed-at timestamp anywhere in
        ESI, so nothing here pretends to know when a finished order actually ended."""
        issued = _utc(render.parse_opt(self.issued))
        if issued is None or self.duration <= 0:
            return None
        return (issued + timedelta(days=self.duration)).isoformat().replace("+00:00", "Z")

    @property
    def filled(self) -> int:
        """Units that left the order - sold on a sell order, bought on a buy order."""
        return self.volume_total - self.volume_remain


@dataclass(frozen=True)
class OwnerOrders:
    """Everything ESI said about one owner in this cycle.

    `history_ok` is False when the history call failed: a watcher must then never conclude that an
    open order which vanished was closed, because its closing row may simply not have been
    readable."""

    owner_key: str
    owner_name: str
    open: tuple[Order, ...] = ()
    history: tuple[Order, ...] = ()
    history_ok: bool = True


def owner_key(kind: str, ident: int) -> str:
    """Stable identity of an order owner: `char:<id>` or `corp:<id>`.

    Corporation orders belong to the corporation, not to whoever happened to fetch them, so they
    are keyed by corporation id - that is what stops two colleagues logged in on this machine from
    reporting one order twice."""
    return f"{kind}:{int(ident)}"


def _utc(moment):
    """A parsed timestamp as UTC; ESI always sends UTC, so a naive value means the same thing."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _opt_int(value) -> int | None:
    """An optional id or division index; None for absent or non-numeric, and 0 stays 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _terminal_state(row: dict) -> str:
    """What ended a history row: `filled`, `expired` or `cancelled`.

    ESI's enum is only `cancelled | expired`, so the derived `filled` is the sole way to tell a
    completed order from one that ran out of time. An `expired` row with nothing left sold (or
    bought) everything it was for, which is what a trader calls filled; an `expired` row with
    volume left lapsed part-filled, having moved `volume_total - volume_remain` units. A cancelled
    order stays cancelled however much it had already moved - the owner ended it, the market did not.
    A row whose remaining volume cannot be read is reported as `expired`, never as `filled`: claiming
    a sale ESI did not evidence would invent income."""
    state = str(row.get("state") or "").strip().lower()
    if state == "cancelled":
        return "cancelled"
    if state == "expired":
        return "filled" if _opt_int(row.get("volume_remain")) == 0 else "expired"
    # An enum value this compat date does not document: pass it through untouched rather than
    # rename it, so a CCP change shows up in the output instead of being quietly reinterpreted.
    return state or "unknown"


def normalise(row: dict, key: str, name: str, *, closed: bool = False,
              corporation: bool = False) -> Order:
    """One ESI order row as an `Order`, owned by `key`/`name`.

    `closed` says which endpoint the row came from - history rows carry `state` and open rows do
    not, so the caller's knowledge is what decides, not whether a key happens to be present.
    `corporation` says the same about ownership: the corporation endpoints have no `is_corporation`
    key because it is true by definition there, and a reader must not conclude otherwise. The
    optional keys (`escrow`, `min_volume`, `is_buy_order`, `issued_by`, `wallet_division`) are
    absent rather than null whenever they do not apply, and become None/False here."""
    return Order(
        order_id=int(row["order_id"]),
        owner_key=key,
        owner_name=name,
        # ESI omits is_buy_order entirely for sell orders; both sides of that are a sell.
        is_buy=bool(row.get("is_buy_order")),
        type_id=int(row["type_id"]),
        region_id=int(row["region_id"]),
        location_id=int(row["location_id"]),
        price=float(row["price"]),
        volume_total=int(row["volume_total"]),
        volume_remain=int(row["volume_remain"]),
        issued=row["issued"],
        duration=int(row.get("duration") or 0),
        state=_terminal_state(row) if closed else OPEN,
        escrow=_opt_float(row.get("escrow")),
        min_volume=_opt_int(row.get("min_volume")),
        range=row.get("range"),
        issued_by=_opt_int(row.get("issued_by")),
        wallet_division=_opt_int(row.get("wallet_division")),
        is_corporation=corporation or bool(row.get("is_corporation")),
    )


def _rows(rows: list[dict], key: str, name: str, closed: bool,
          corporation: bool = False) -> tuple[Order, ...]:
    """Every row of one endpoint as `Order`s; an endpoint that answers with nothing is empty."""
    return tuple(normalise(row, key, name, closed=closed, corporation=corporation)
                 for row in rows or [])


def fetch_character(client: esi_mod.Esi, record: dict) -> OwnerOrders:
    """A character's live order book plus its ~90 days of history."""
    cid = int(record["character_id"])
    name = record.get("character_name") or str(cid)
    key = owner_key("char", cid)
    token = record["access_token"]
    try:
        rows = client.get(f"/characters/{cid}/orders", token=token)
    except esi_mod.AuthError as err:
        raise OrderAccess(f"ESI refused the order book ({err}) - no {CHARACTER_FEATURE} consent; "
                          f"run: eve-skills login --scopes {CHARACTER_FEATURE}") from None
    # History is the backfill, not the live book: losing it must not lose what can still trade.
    # `history_ok=False` is how the watcher is told to leave vanished orders alone this cycle.
    try:
        history = client.get_all(f"/characters/{cid}/orders/history", token=token)
        history_ok = True
    except esi_mod.EsiError:
        history, history_ok = [], False
    return OwnerOrders(key, name, _rows(rows, key, name, False), _rows(history, key, name, True),
                       history_ok)


def fetch_corporation(client: esi_mod.Esi, record: dict, public: dict) -> OwnerOrders:
    """The orders of the corporation `public` says this character belongs to.

    Any member holding one of the two roles can read them; the rows are the corporation's, so the
    result is keyed by corporation id and identical whichever colleague's token fetched it."""
    corp_id = exports.corp_of(public)
    if not corp_id:
        raise RuntimeError("no corporation id on the public record")
    corp_name = _corp_name(client, corp_id)
    key = owner_key("corp", corp_id)
    token = record["access_token"]
    try:
        rows = client.get_all(f"/corporations/{corp_id}/orders", token=token)
    except esi_mod.AuthError as err:
        raise OrderAccess(_corp_denied(client, record, corp_name, err)) from None
    try:
        history = client.get_all(f"/corporations/{corp_id}/orders/history", token=token)
        history_ok = True
    except esi_mod.EsiError:
        history, history_ok = [], False
    # Every row here is the corporation's, whether or not ESI bothers to say so.
    return OwnerOrders(key, corp_name, _rows(rows, key, corp_name, False, True),
                       _rows(history, key, corp_name, True, True), history_ok)


def _corp_name(client: esi_mod.Esi, corp_id: int) -> str:
    """Corporation name, or an honest `corporation <id>` when ESI does not name it."""
    return esi_mod.resolve_names(client, {corp_id}).get(corp_id) or f"corporation {corp_id}"


def _corp_denied(client: esi_mod.Esi, record: dict, corp_name: str, err: Exception) -> str:
    """Why ESI said no, phrased as the thing the user can actually change.

    One 403 has two unrelated causes here with opposite fixes: consent is OAuth and is granted by
    re-running login, the role lives in-game and only a director can grant it. Guessing wrong sends
    the user to the wrong place, so the roles endpoint is consulted whenever its scope allows."""
    # Named by whoever reports it, so a watcher and a command can each prefix their own context.
    refused = f"ESI refused the corporation order book ({err})"
    if not sso.has_scope(record, CORPORATION_SCOPE):
        return f"{refused} - no {CORPORATION_FEATURE} consent; run: eve-skills login --scopes {CORPORATION_FEATURE}"
    if not sso.has_scope(record, ROLE_SCOPE):
        return (f"{refused} - it needs the {' or '.join(ORDER_ROLES)} role in {corp_name}, and that "
                f"role could not be verified without the roles consent; run: eve-skills login "
                f"--scopes {CORPORATION_FEATURE}")
    try:
        doc = client.get(f"/characters/{record['character_id']}/roles", token=record["access_token"]) or {}
    except esi_mod.EsiError:
        return f"{refused} - the in-game role could not be verified"
    held = {role for role, value in (doc.get("roles") or {}).items() if value}
    if not held & set(ORDER_ROLES):
        return (f"ESI refused the corporation order book because it holds neither "
                f"{' nor '.join(ORDER_ROLES)} in {corp_name}; ask a director to grant one "
                f"(in-game: corporations > roles)")
    return (f"{refused} even though it holds {' and '.join(sorted(held & set(ORDER_ROLES)))} "
            f"in {corp_name}")
