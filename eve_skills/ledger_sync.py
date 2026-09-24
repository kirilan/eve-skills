"""`ledger sync`: copy what ESI still remembers into the ledger before it forgets.

Sources, per player corporation that at least one stored character belongs to:

* industry jobs, including completed ones (ESI keeps those ~90 days)
* the journal and transactions of all seven wallet divisions (30 days)
* open and historical market orders (broker fees are matched to them by their `issued` stamp)
* blueprints (an invented copy's ME/TE is the only record of which decryptor made it)

and, for each stored character in such a corporation, the same four from their personal side - so a
sale that went through a personal wallet by accident is still booked. Personal transactions that ESI
flags `is_personal: false` were made on the corporation's behalf and are already in its wallet;
they are dropped here rather than de-duplicated later.

Every document is read with whichever consenting character ESI accepts, exactly as `industry status`
does, and each one commits on its own: a refused or failed document costs that document and is
reported, never the rest of the sync. Finally CCP's reference prices are stored for every type the
ledger has met, once per UTC day - the first of them is what the ledger values stock it never saw
bought at.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import alphadata, esi as esi_mod, exports, industry, ledger, ledger_db, market, orders, sso

WALLET_DIVISIONS = range(1, 8)

# ESI serves at most this many transactions per request and pages further back with `from_id`.
TRANSACTION_PAGE = 2500
# A safety stop for the `from_id` walk. 30 days of even a very busy trader is a handful of pages.
MAX_TRANSACTION_PAGES = 40

# Opening stock is valued at the median daily average of this region's history over this many days up
# to the cutover. The Forge because Jita is where the business buys and sells; a median because a
# thin market's single odd day (a 238M "average" for a hull that trades at 226k) must not become
# the value of everything the ledger draws from opening stock.
OPENING_REGION = 10000002
OPENING_WINDOW_DAYS = 30

# NPC corporations have ids in this range and none of the corporation endpoints answer for them.
NPC_CORPORATIONS = range(1_000_000, 2_000_000)

SCOPE_CORP_JOBS = "esi-industry.read_corporation_jobs.v1"
SCOPE_CHAR_JOBS = "esi-industry.read_character_jobs.v1"
SCOPE_CORP_WALLET = "esi-wallet.read_corporation_wallets.v1"
SCOPE_CHAR_WALLET = "esi-wallet.read_character_wallet.v1"
SCOPE_CORP_BLUEPRINTS = "esi-corporations.read_blueprints.v1"
SCOPE_CHAR_BLUEPRINTS = "esi-characters.read_blueprints.v1"


@dataclass
class SourceResult:
    """One document of one owner: what was read, what was new, and how far back it reaches."""
    source: str
    seen: int = 0
    new: int = 0
    oldest: str | None = None
    newest: str | None = None


@dataclass
class SyncReport:
    synced_at: str
    corporations: list[int] = field(default_factory=list)
    characters: list[int] = field(default_factory=list)
    sources: list[SourceResult] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    prices: int = 0
    opening_prices: int = 0
    cutover: str | None = None

    def to_json(self) -> dict:
        return {"synced_at": self.synced_at, "corporations": self.corporations,
                "characters": self.characters, "prices_stored": self.prices,
                "opening_prices_stored": self.opening_prices, "cutover": self.cutover,
                "sources": [vars(s) for s in self.sources], "problems": self.problems}


def iso(moment) -> str:
    """ESI's own timestamp spelling, which is what every stored date uses and compares against."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def problem_text(problem: dict) -> str:
    """One line a person can act on for a document the sync could not read."""
    where = f"{problem.get('owner')} {problem.get('section')}"
    if problem["code"] == "missing_consent":
        return f"{where}: no {problem['feature']} consent; run: eve-skills login --scopes {problem['feature']}"
    if problem["code"] == "esi_refused":
        return (f"{where}: ESI refused {problem['refused_character_ids']} "
                f"(corporation wallets need the Accountant or Junior Accountant role)")
    return f"{where}: {problem.get('message', 'unavailable')}"


def _feature(scope: str) -> str:
    return next((name for name, scopes in sso.OPTIONAL_SCOPES.items() if scope in scopes), scope)


class _Reader:
    """Reads one owner's documents with the first candidate ESI accepts, remembering refusals."""

    def __init__(self, client: esi_mod.Esi, report: SyncReport, owner: str, candidates):
        self.client = client
        self.report = report
        self.owner = owner
        self.candidates = candidates

    def read(self, section: str, scope: str, path: str, *, paginated: bool):
        refused = []
        for tok in self.candidates:
            if scope not in set(tok.get("scopes") or []):
                continue
            try:
                fetch = self.client.get_all_meta if paginated else self.client.get_meta
                rows, _meta = fetch(path, token=tok["access_token"])
                return rows, tok
            except esi_mod.AuthError:
                refused.append(int(tok["character_id"]))
            except esi_mod.EsiError as err:
                self.report.problems.append({"owner": self.owner, "section": section, "code": "esi_error",
                                             "message": str(err)})
                return None, None
        self.report.problems.append({"owner": self.owner, "section": section,
                                     "code": "esi_refused" if refused else "missing_consent",
                                     "feature": _feature(scope), "scope": scope,
                                     "refused_character_ids": refused})
        return None, None

    def transactions(self, section: str, scope: str, path: str, conn: sqlite3.Connection, wallet: str):
        """Walk `from_id` back until a page is short, empty, or reaches rows the ledger already holds."""
        first, tok = self.read(section, scope, path, paginated=False)
        if first is None:
            return None
        rows = list(first)
        page = first
        for _ in range(MAX_TRANSACTION_PAGES):
            if len(page) < TRANSACTION_PAGE:
                break
            oldest = min(int(r["transaction_id"]) for r in page)
            if conn.execute("SELECT 1 FROM transactions WHERE wallet = ? AND transaction_id = ?",
                            (wallet, oldest)).fetchone():
                break
            sep = "&" if "?" in path else "?"
            try:
                page = self.client.get(f"{path}{sep}from_id={oldest - 1}", token=tok["access_token"])
            except esi_mod.EsiError as err:
                self.report.problems.append({"owner": self.owner, "section": section, "code": "esi_error",
                                             "message": str(err)})
                break
            rows.extend(page)
        return rows


def _span(rows, key: str | None) -> tuple[str | None, str | None]:
    stamps = [r.get(key) for r in rows if key and r.get(key)]
    return (min(stamps), max(stamps)) if stamps else (None, None)


def _store(conn, report, source, rows, writer, date_key):
    with conn:
        new = writer(rows)
        oldest, newest = _span(rows, date_key)
        ledger_db.log_sync(conn, source, report.synced_at, len(rows), new, oldest, newest)
    report.sources.append(SourceResult(source, len(rows), new, oldest, newest))


def _sync_owner(conn, client, report, *, owner: str, base: str, candidates, corporation: bool):
    """Jobs, orders, blueprints and wallets of one corporation or one character."""
    reader = _Reader(client, report, owner, candidates)
    now = report.synced_at

    jobs, _tok = reader.read("jobs", SCOPE_CORP_JOBS if corporation else SCOPE_CHAR_JOBS,
                             f"{base}/industry/jobs?include_completed=true", paginated=corporation)
    if jobs is not None:
        _store(conn, report, f"{owner}:jobs", jobs,
               lambda rows: ledger_db.upsert_jobs(conn, owner, rows, now), "start_date")

    order_scope = orders.CORPORATION_SCOPE if corporation else orders.CHARACTER_SCOPE
    open_rows, _tok = reader.read("orders", order_scope, f"{base}/orders", paginated=corporation)
    history, _tok = reader.read("order_history", order_scope, f"{base}/orders/history", paginated=True)
    order_rows = (open_rows or []) + (history or [])
    if open_rows is not None or history is not None:
        _store(conn, report, f"{owner}:orders", order_rows,
               lambda rows: ledger_db.upsert_orders(conn, owner, rows, now), "issued")

    blueprints, _tok = reader.read("blueprints", SCOPE_CORP_BLUEPRINTS if corporation else SCOPE_CHAR_BLUEPRINTS,
                                   f"{base}/blueprints", paginated=True)
    if blueprints is not None:
        _store(conn, report, f"{owner}:blueprints", blueprints,
               lambda rows: ledger_db.upsert_blueprints(conn, owner, rows, now), None)

    if corporation:
        wallets = [(f"{owner}:{division}", f"{base}/wallets/{division}") for division in WALLET_DIVISIONS]
        scope = SCOPE_CORP_WALLET
    else:
        wallets = [(owner, f"{base}/wallet")]
        scope = SCOPE_CHAR_WALLET
    for wallet, prefix in wallets:
        journal, _tok = reader.read(f"journal {wallet}", scope, f"{prefix}/journal", paginated=True)
        if journal is None:
            break   # the same refusal would repeat for every division
        _store(conn, report, f"{wallet}:journal", journal,
               lambda rows, w=wallet: ledger_db.insert_journal(conn, w, rows), "date")
        tx = reader.transactions(f"transactions {wallet}", scope, f"{prefix}/transactions", conn, wallet)
        if tx is None:
            break
        if not corporation:
            # Made on the corporation's behalf: already booked from its wallet.
            tx = [r for r in tx if r.get("is_personal", True)]
        _store(conn, report, f"{wallet}:transactions", tx,
               lambda rows, w=wallet: ledger_db.insert_transactions(conn, w, rows), "date")


def relevant_types(conn: sqlite3.Connection) -> set[int]:
    """Every type the ledger can put a price on: traded types, job products, and each job's inputs."""
    types = {r[0] for r in conn.execute("SELECT DISTINCT type_id FROM transactions")}
    types |= {r[0] for r in conn.execute("SELECT DISTINCT product_type_id FROM jobs "
                                         "WHERE product_type_id IS NOT NULL")}
    blueprints = {r[0] for r in conn.execute("SELECT DISTINCT blueprint_type_id FROM jobs "
                                             "WHERE blueprint_type_id IS NOT NULL")}
    try:
        recipes = industry.recipes_by_blueprint(alphadata.blueprint_materials())
    except (FileNotFoundError, ValueError):
        recipes = {}
    try:
        invention = alphadata.blueprint_invention()
    except (FileNotFoundError, ValueError):
        invention = {"blueprints": {}, "decryptors": {}}
    for blueprint in blueprints:
        recipe = recipes.get(blueprint)
        if recipe is not None:
            types |= set(recipe.materials)
        row = invention["blueprints"].get(str(blueprint))
        if row is not None:
            types |= {int(t) for t in row["m"]}
    types |= {int(t) for t in invention["decryptors"]}
    return types


def sync(conn: sqlite3.Connection, client: esi_mod.Esi, *, character: str | None = None,
         personal: bool = True) -> SyncReport:
    """One pass over every source; returns what was read. Never raises for a single document."""
    records = sso.list_characters()
    if not records:
        raise RuntimeError("not logged in - run: eve-skills login")
    wanted = sso.resolve_character(character) if character else None
    report = SyncReport(synced_at=iso(client.now()))

    members: dict[int, list[dict]] = {}
    for rec in records:
        cid = int(rec["character_id"])
        try:
            tok = sso.get_access_token(cid)
            public = client.get(f"/characters/{cid}")
        except (RuntimeError, esi_mod.EsiError) as err:
            report.problems.append({"owner": f"char:{cid}", "section": "character", "code": "esi_error",
                                    "message": str(err)})
            continue
        corp_id = exports.corp_of(public)
        if corp_id is None or corp_id in NPC_CORPORATIONS:
            continue
        members.setdefault(corp_id, []).append(tok)
    if wanted is not None:
        members = {corp: toks for corp, toks in members.items()
                   if any(int(t["character_id"]) == wanted for t in toks)}
        # The named character reads first, so its consent is the one exercised when it has it.
        for corp, toks in members.items():
            toks.sort(key=lambda t: int(t["character_id"]) != wanted)
    if not members:
        raise RuntimeError("no stored character is in a player corporation - the ledger books a "
                           "corporation's industry and trade")

    for corp_id, toks in members.items():
        report.corporations.append(corp_id)
        _sync_owner(conn, client, report, owner=f"corp:{corp_id}", base=f"/corporations/{corp_id}",
                    candidates=toks, corporation=True)
        if personal:
            for tok in toks:
                cid = int(tok["character_id"])
                report.characters.append(cid)
                _sync_owner(conn, client, report, owner=f"char:{cid}", base=f"/characters/{cid}",
                            candidates=[tok], corporation=False)

    try:
        table = market.price_table(client)
    except esi_mod.EsiError as err:
        report.problems.append({"owner": "public", "section": "prices", "code": "esi_error",
                                "message": str(err)})
    else:
        day = report.synced_at[:10]
        rows = []
        for type_id in sorted(relevant_types(conn)):
            ref = table.reference(type_id)
            if ref is not None and (ref.average_price is not None or ref.adjusted_price is not None):
                rows.append((type_id, ref.average_price, ref.adjusted_price))
        with conn:
            report.prices = ledger_db.insert_prices(conn, day, rows)

    with conn:
        ledger_db.set_meta(conn, "last_sync", report.synced_at)
        if ledger_db.get_meta(conn, "first_sync") is None:
            ledger_db.set_meta(conn, "first_sync", report.synced_at)
        if ledger_db.cutover(conn) is None:
            first = conn.execute("SELECT MIN(d) FROM (SELECT MIN(date) AS d FROM transactions "
                                 "UNION ALL SELECT MIN(start_date) FROM jobs)").fetchone()[0]
            if first:
                ledger_db.set_meta(conn, "cutover", first[:10])
    report.cutover = ledger_db.cutover(conn)
    if report.cutover:
        report.opening_prices = _opening_prices(conn, client, report)
    _record_snapshot(conn, report)
    return report


def _record_snapshot(conn: sqlite3.Connection, report: SyncReport) -> None:
    """Store this sync's figures (`ledger.snapshot`) so progress is a series, not a recollection."""
    try:
        recipes = industry.recipes_by_blueprint(alphadata.blueprint_materials())
    except (FileNotFoundError, ValueError):
        recipes = {}
    try:
        invention = alphadata.blueprint_invention()
    except (FileNotFoundError, ValueError):
        invention = {"blueprints": {}, "decryptors": {}}
    facts = ledger.load(conn)
    book = ledger.replay(facts, recipes, invention, report.synced_at)
    with conn:
        ledger_db.store_snapshot(conn, report.synced_at,
                                 ledger.snapshot(book, facts, report.synced_at, since=report.cutover))


# `skills --watch --ledger` syncs at most this often: wallet documents are cached by ESI for an hour,
# so polling faster would only re-read the same rows.
WATCH_SYNC_SECONDS = 3600


def sync_if_due(client: esi_mod.Esi, interval: float = WATCH_SYNC_SECONDS) -> tuple[str | None, list[str]]:
    """For a watch loop: sync when the last one is `interval` old. (status line, problem lines).

    Never raises - an overnight watch must survive an ESI outage or a locked ledger - and reads the
    last sync from the ledger itself, so a manual sync in between also resets the clock."""
    try:
        conn = ledger_db.connect()
        try:
            last = ledger_db.last_sync(conn)
            if last:
                age = (client.now() - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()
                if age < interval:
                    return f"ledger: last synced {last}", []
            report = sync(conn, client)
        finally:
            conn.close()
    except (RuntimeError, esi_mod.EsiError, sqlite3.Error, OSError) as err:
        return None, [f"ledger sync failed: {err}"]
    new = sum(s.new for s in report.sources)
    return f"ledger: synced {report.synced_at}, {new:,} new rows", [problem_text(p) for p in report.problems]


def _opening_prices(conn: sqlite3.Connection, client: esi_mod.Esi, report: SyncReport) -> int:
    """Fix an opening price for every type the ledger has met and not yet priced at the cutover.

    Only types CCP's price document lists are asked about: the rest - blueprints above all, which
    the ledger meets as every copy and invention job's product - have no market to ask, and
    `/markets/{region}/history` answers them with an error rather than an empty list."""
    priced = {r[0] for r in conn.execute("SELECT DISTINCT type_id FROM prices")}
    missing = sorted((relevant_types(conn) & priced) - ledger_db.opening_price_types(conn))
    if not missing:
        return 0
    end = date.fromisoformat(report.cutover)
    start = end - timedelta(days=OPENING_WINDOW_DAYS)
    paths = {f"/markets/{OPENING_REGION}/history?type_id={t}": t for t in missing}
    answers = client.get_many(list(paths))
    fallback = {r[0]: r[1] for r in conn.execute(
        "SELECT type_id, average_price FROM prices WHERE average_price IS NOT NULL ORDER BY day DESC")}
    rows = []
    for path, type_id in paths.items():
        answer = answers.get(path)
        if isinstance(answer, Exception):
            report.problems.append({"owner": "public", "section": f"history {type_id}", "code": "esi_error",
                                    "message": str(answer)})
            continue    # retried by the next sync rather than fixed at a worse figure
        window = [float(r["average"]) for r in answer or []
                  if r.get("average") is not None and start.isoformat() < str(r.get("date")) <= end.isoformat()]
        if window:
            rows.append((type_id, statistics.median(window), "forge_history", end.isoformat()))
        elif type_id in fallback:
            rows.append((type_id, float(fallback[type_id]), "ccp_average", report.synced_at[:10]))
    with conn:
        return ledger_db.insert_opening_prices(conn, rows, report.synced_at)
