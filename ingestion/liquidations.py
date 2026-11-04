"""Forced-liquidation feeds, parsed (C1 · the liquidations recorder).

No venue serves free liquidation *history*
(vault `Spikes/forward-data-collector-spike.md`,
`Spikes/literature-for-open-experiments.md`), so the only way to test
"cascade → reversal" is to record the live feeds forward. A gap in the record
is permanent, and that shapes both halves of this module:

- the parsers turn venue frames into one `Liquidation` shape, keeping the raw
  frame so any derived column can be recomputed later;
- `CoverageTracker` decides which minutes were *provably* recorded. A minute
  counts only when liveness proofs (any frame, including our own ping's pong)
  arrived no more than `max_silence_s` apart from before it started until
  after it ended. A suspend, an OOM kill or a `kill -9` therefore leaves no
  coverage row, rather than a gap row that only a surviving process could
  write — absence is the evidence. It also lets the analysis tell "no
  liquidations" (a covered minute with zero events) from "not recording".

Binance `!forceOrder@arr` is **sampled**: at most one liquidation per symbol
per second (documented), so its counts are a lower bound. Bybit's
`allLiquidation` (since 2025-02) claims every liquidation, pushed every
500ms; OKX's completeness is unverified. The pre-registration
(vault `Docs/liquidation-cascade-experiment.md`)
says how each venue may be used.

**Kraken Futures** has no public liquidation push feed; instead its public
`GET /derivatives/api/v3/history` trade-history endpoint tags ordinary fills
with a `type` that is `liquidation`/`partial liquidation` among regular
`fill`s (vault `Docs/dataset-scouting-2026-10-05.md`). It is capped at the
most recent 100 trades per request with no forward-paging parameter, so the
recorder must poll it repeatedly and notice when more than 100 trades landed
between polls (`kraken_gap`). A real recorded response
(`tests/fixtures/liquidations/kraken_trade_history_pf_xbtusd.json`) pinned two
surprises the docs page gets wrong: the array comes back ascending by time
(oldest first), not descending, and `trade_id` counts down within the
request window rather than being a stable id — Kraken's `uid` is the only
field safe to dedupe on, so it rides along as `Liquidation.venue_trade_id`.
Only the `liquidation`/`partial liquidation` rows are kept: storing every
fill on a busy perpetual would be orders of magnitude more volume than this
project's other three venues combined for data this project's experiments
don't use (plain fills, no cascade signal) — the flag is the only part of
the feed anyone has a pre-registered use for.

Pure: no network, no DB, no clock. The runner supplies all three.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

BINANCE = "binance"
OKX = "okx"
BYBIT = "bybit"
KRAKEN = "kraken"

# The side of the POSITION that was liquidated. On Binance and OKX the frame
# carries the forced ORDER's side: a long is closed by a sell. Bybit's
# `allLiquidation` carries the POSITION's side instead ("When you receive a
# Buy update, this means that a long position has been liquidated" — Bybit v5
# docs), so the same word means the opposite there. See parse_bybit.
LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class Liquidation:
    venue: str
    symbol: str  # venue-native: BTCUSDT on Binance, BTC-USDT-SWAP on OKX
    event_ts: datetime
    side: str  # LONG | SHORT — the position that was closed out
    price: float
    size: float  # venue-native units (see size_unit)
    size_unit: str  # "base" | "contracts"
    qty_base: float | None  # base-asset quantity, when it can be derived
    notional: float | None  # quote-currency value, when it can be derived
    source: str  # "ws" | "rest"
    raw: dict = field(compare=False, hash=False)
    # Kraken's own trade uid (NULL for the other venues): the one field they
    # hand back that is actually stable across polls, unlike their `trade_id`
    # (see parse_kraken_history) — the idempotency key for storage.
    venue_trade_id: str | None = None


def _ms(value) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _liquidated_side(order_side: str) -> str | None:
    side = order_side.strip().lower()
    if side == "sell":
        return LONG
    if side == "buy":
        return SHORT
    return None


def parse_binance(message: str | bytes) -> list[Liquidation]:
    """A `forceOrder` frame → at most one liquidation.

    Uses the *filled* quantity (`z`) at the *average* fill price (`ap`): the
    order price `p` is the bankruptcy-side limit, not where it traded. A frame
    that isn't a force order (or is malformed) yields nothing.
    """
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    # Combined-stream frames wrap the event in {"stream":..., "data":...}.
    event = payload.get("data", payload)
    if not isinstance(event, dict) or event.get("e") != "forceOrder":
        return []
    order = event.get("o") or {}
    try:
        side = _liquidated_side(str(order["S"]))
        size = float(order["z"])
        price = float(order["ap"])
        ts = _ms(order["T"])
        symbol = str(order["s"]).upper()
    except (KeyError, TypeError, ValueError):
        return []
    if side is None or size <= 0 or price <= 0:
        return []
    return [
        Liquidation(
            venue=BINANCE,
            symbol=symbol,
            event_ts=ts,
            side=side,
            price=price,
            size=size,
            size_unit="base",
            qty_base=size,
            notional=size * price,
            source="ws",
            raw=event,
        )
    ]


@dataclass(frozen=True)
class OkxContract:
    """What turns an OKX contract count into a quantity (public/instruments)."""

    ct_val: float
    ct_mult: float
    ct_type: str  # "linear" (ctVal in base) | "inverse" (ctVal in quote)


def okx_contracts(instruments_payload: dict) -> dict[str, OkxContract]:
    """`GET /api/v5/public/instruments?instType=SWAP` → instId → contract."""
    out: dict[str, OkxContract] = {}
    for row in instruments_payload.get("data") or []:
        try:
            out[str(row["instId"])] = OkxContract(
                ct_val=float(row["ctVal"]),
                ct_mult=float(row.get("ctMult") or 1),
                ct_type=str(row.get("ctType") or ""),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _okx_quantities(
    contract: OkxContract | None, size: float, price: float
) -> tuple[float | None, float | None]:
    if contract is None:
        return None, None
    units = size * contract.ct_val * contract.ct_mult
    if contract.ct_type == "linear":
        return units, units * price
    if contract.ct_type == "inverse":
        return units / price, units
    return None, None


def parse_okx_data(
    data: list, contracts: dict[str, OkxContract], source: str
) -> list[Liquidation]:
    """The `data` array shared by the WS push and the REST response."""
    out: list[Liquidation] = []
    for group in data or []:
        if not isinstance(group, dict) or group.get("instType") != "SWAP":
            continue
        inst_id = str(group.get("instId") or "")
        if not inst_id:
            continue
        for detail in group.get("details") or []:
            try:
                side = _liquidated_side(str(detail["side"]))
                size = float(detail["sz"])
                price = float(detail["bkPx"])
                ts = _ms(detail["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if side is None or size <= 0 or price <= 0:
                continue
            qty_base, notional = _okx_quantities(contracts.get(inst_id), size, price)
            raw = {k: v for k, v in group.items() if k != "details"}
            raw["detail"] = detail
            out.append(
                Liquidation(
                    venue=OKX,
                    symbol=inst_id,
                    event_ts=ts,
                    side=side,
                    price=price,
                    size=size,
                    size_unit="contracts",
                    qty_base=qty_base,
                    notional=notional,
                    source=source,
                    raw=raw,
                )
            )
    return out


def parse_okx(
    message: str | bytes, contracts: dict[str, OkxContract]
) -> tuple[str, list[Liquidation]]:
    """An OKX public-WS frame → (kind, liquidations).

    kind is "pong" (the reply to our text `ping`), "subscribed" (the server
    accepted the subscription), "event" (other acks), "error" (a refused
    request), "data" (a liquidation push) or "other".
    Every kind but "error" is a liveness proof; only "data" carries rows.
    "pong" is not JSON, so it must be matched before parsing — treating it as
    a parse failure would log noise every 10s.
    """
    text = message.decode() if isinstance(message, bytes) else message
    if text == "pong":
        return "pong", []
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return "other", []
    if not isinstance(payload, dict):
        return "other", []
    if "event" in payload:
        # A refused subscription is a live socket carrying nothing: the runner
        # must reconnect, not count the minutes it spends there as covered.
        if payload["event"] == "error":
            return "error", []
        return ("subscribed" if payload["event"] == "subscribe" else "event"), []
    if (payload.get("arg") or {}).get("channel") != "liquidation-orders":
        return "other", []
    return "data", parse_okx_data(payload.get("data") or [], contracts, "ws")


def parse_bybit(message: str | bytes) -> tuple[str, list[Liquidation]]:
    """A Bybit v5 public-linear frame → (kind, liquidations).

    kind: "pong", "subscribed", "error" (a refused op), "data" or "other".
    `S` is the POSITION side (Buy = a long was liquidated), the reverse of
    the order-side convention the other venues use. `p` is the bankruptcy
    price, not a fill price, so `notional` here is size × bankruptcy price.
    """
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return "other", []
    if not isinstance(payload, dict):
        return "other", []
    if "op" in payload:
        if not payload.get("success", False):
            return "error", []
        if payload["op"] == "subscribe":
            return "subscribed", []
        if payload["op"] == "ping" or payload.get("ret_msg") == "pong":
            return "pong", []
        return "other", []
    if not str(payload.get("topic", "")).startswith("allLiquidation."):
        return "other", []
    out: list[Liquidation] = []
    for item in payload.get("data") or []:
        try:
            position = str(item["S"]).strip().lower()
            size = float(item["v"])
            price = float(item["p"])
            ts = _ms(item["T"])
            symbol = str(item["s"]).upper()
        except (KeyError, TypeError, ValueError):
            continue
        side = {"buy": LONG, "sell": SHORT}.get(position)
        if side is None or size <= 0 or price <= 0:
            continue
        out.append(
            Liquidation(
                venue=BYBIT,
                symbol=symbol,
                event_ts=ts,
                side=side,
                price=price,
                size=size,
                size_unit="base",
                qty_base=size,
                notional=size * price,
                source="ws",
                raw=item,
            )
        )
    return "data", out


KRAKEN_LIQUIDATION_TYPES = frozenset({"liquidation", "partial liquidation"})


@dataclass(frozen=True)
class KrakenTrade:
    """One row of Kraken's trade-history response, regardless of `type`.

    Kept separate from `Liquidation` because most rows are plain `fill`s we
    never store; this is only what `kraken_gap` needs to judge coverage.
    """

    symbol: str
    event_ts: datetime
    uid: str
    type: str
    side: str | None
    price: float
    size: float
    raw: dict = field(compare=False, hash=False)


def _kraken_ts(value: str) -> datetime:
    # "2026-10-05T15:48:56.419439Z" — fromisoformat wants "+00:00", not "Z".
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def parse_kraken_history(payload: dict, symbol: str) -> list[KrakenTrade]:
    """`GET /derivatives/api/v3/history` response → every row, parsed.

    A malformed row is dropped rather than failing the whole poll — one bad
    row must not blind the recorder to the 99 good ones next to it.
    """
    out: list[KrakenTrade] = []
    for row in payload.get("history") or []:
        try:
            out.append(
                KrakenTrade(
                    symbol=symbol,
                    event_ts=_kraken_ts(row["time"]),
                    uid=str(row["uid"]),
                    type=str(row.get("type") or ""),
                    side=(str(row["side"]) if row.get("side") else None),
                    price=float(row["price"]),
                    size=float(row["size"]),
                    raw=row,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class KrakenContract:
    """What turns a Kraken size into a base-asset quantity (public/instruments).

    Kraken's "flexible_futures" product (PF_*) is linear and base-denominated:
    `contractSize` is in units of the underlying (1 for PF_XBTUSD/PF_ETHUSD —
    confirmed live 2026-10-05, `tests/fixtures/liquidations/kraken_instruments.json`),
    unlike the older PI_* inverse product where a contract is a fixed USD
    notional. The real trade-history fixture's sizes (0.0001-0.0011) are
    fractional BTC, consistent with base-denomination, not whole-USD contracts.
    """

    contract_size: float
    kind: str  # e.g. "flexible_futures"


def kraken_contracts(instruments_payload: dict) -> dict[str, KrakenContract]:
    """`GET /derivatives/api/v3/instruments` → symbol → contract."""
    out: dict[str, KrakenContract] = {}
    for row in instruments_payload.get("instruments") or []:
        try:
            out[str(row["symbol"])] = KrakenContract(
                contract_size=float(row["contractSize"]),
                kind=str(row.get("type") or ""),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def kraken_liquidations(
    trades: list[KrakenTrade], contract: KrakenContract | None = None
) -> list[Liquidation]:
    """The subset worth storing: `type` is a (partial) liquidation.

    Side follows the same order-side convention as Binance/OKX (the forced
    order closing a long is a sell) since Kraken's docs don't say otherwise
    and the field is named identically; unverified against a real liquidated
    row, since none appeared in any sample pulled so far.

    `contract` resolves size units: PF_* contracts are base-denominated
    (see `KrakenContract`), so when it is known, `size_unit` is "base" and
    `qty_base`/`notional` are derived; unknown contract keeps the old,
    conservative "contracts"/None behaviour (same pattern as OKX's unknown
    contract case).
    """
    out: list[Liquidation] = []
    for t in trades:
        if t.type not in KRAKEN_LIQUIDATION_TYPES or t.side is None:
            continue
        side = _liquidated_side(t.side)
        if side is None or t.price <= 0 or t.size <= 0:
            continue
        if contract is not None:
            size_unit = "base"
            qty_base = t.size * contract.contract_size
            notional = qty_base * t.price
        else:
            size_unit = "contracts"
            qty_base = None
            notional = None
        out.append(
            Liquidation(
                venue=KRAKEN,
                symbol=t.symbol,
                event_ts=t.event_ts,
                side=side,
                price=t.price,
                size=t.size,
                size_unit=size_unit,
                qty_base=qty_base,
                notional=notional,
                source="rest",
                raw=t.raw,
                venue_trade_id=t.uid,
            )
        )
    return out


def kraken_new_trades(
    trades: list[KrakenTrade], last_ts: datetime | None
) -> list[KrakenTrade]:
    """Rows not yet forwarded: everything, the first poll; newer ones after."""
    if last_ts is None:
        return list(trades)
    return [t for t in trades if t.event_ts > last_ts]


def kraken_gap(trades: list[KrakenTrade], last_ts: datetime | None) -> bool:
    """True when this 100-row window can't reach back to the last poll.

    The response has no `since`/forward-paging parameter, so a busy market
    can push more than 100 trades between two polls; when that happens the
    oldest row this poll got back is already newer than the last trade the
    previous poll forwarded, and everything in between is permanently gone.
    """
    if not trades or last_ts is None:
        return False
    oldest = min(t.event_ts for t in trades)
    return oldest > last_ts


@dataclass(frozen=True)
class KrakenMinuteAgg:
    """One instrument-minute's worth of ALL trade prints, not just liquidations.

    Built so R3-b can later ask "was there real, tradeable size at the 1m
    candle's wick?" — a question the liquidation-flagged rows alone can't
    answer. `size_near_min`/`size_near_max` are the size of prints within
    `near_bps` of this aggregate's own min/max, which is exact for a minute
    whose trades all arrive in one poll and a conservative (never inflated)
    approximation when a busy minute's trades split across polls — see
    `aggregate_kraken_minutes`.
    """

    symbol: str
    minute: int  # epoch seconds, floor of the minute
    min_price: float
    max_price: float
    trade_count: int
    total_size: float
    size_near_min: float
    size_near_max: float
    liq_count: int
    liq_size: float


KRAKEN_WICK_NEAR_BPS = 2.0


def aggregate_kraken_minutes(
    trades: list[KrakenTrade], near_bps: float = KRAKEN_WICK_NEAR_BPS
) -> list[KrakenMinuteAgg]:
    """Group ALREADY-DEDUPED (by uid) trades into one row per (symbol, minute).

    Dedup is the caller's job (`ingestion/liquidation_recorder.py` tracks
    seen uids across polls) — this function is pure aggregation and would
    double-count a trade handed to it twice. `size_near_min`/`size_near_max`
    are computed against THIS CALL's own min/max: when a minute's trades
    split across two polls, a print near the eventual minute-low that isn't
    near *this poll's* low is undercounted here, and the Postgres upsert
    (storage/liquidation_store.py) sums the two calls' near-sizes rather than
    recomputing them against the merged min/max — a documented, conservative
    approximation, not an exact reconstruction.
    """
    groups: dict[tuple[str, int], list[KrakenTrade]] = {}
    for t in trades:
        key = (t.symbol, _floor_minute(t.event_ts.timestamp()))
        groups.setdefault(key, []).append(t)
    out: list[KrakenMinuteAgg] = []
    near_frac = near_bps / 10_000.0
    for (symbol, minute), rows in groups.items():
        lo = min(r.price for r in rows)
        hi = max(r.price for r in rows)
        total_size = sum(r.size for r in rows)
        near_min = sum(
            r.size for r in rows if lo > 0 and (r.price - lo) / lo <= near_frac
        )
        near_max = sum(
            r.size for r in rows if hi > 0 and (hi - r.price) / hi <= near_frac
        )
        liq_rows = [r for r in rows if r.type in KRAKEN_LIQUIDATION_TYPES]
        out.append(
            KrakenMinuteAgg(
                symbol=symbol,
                minute=minute,
                min_price=lo,
                max_price=hi,
                trade_count=len(rows),
                total_size=total_size,
                size_near_min=near_min,
                size_near_max=near_max,
                liq_count=len(liq_rows),
                liq_size=sum(r.size for r in liq_rows),
            )
        )
    return out


# A normal poll (every few seconds) never needs more than a couple of
# minutes' worth of coverage rows. This bounds the pathological case — a
# `last_ts` that is old for a reason other than a short-lived gap (a process
# that sat dead for days before restarting with stale state) — so one call
# can't emit an unbounded number of rows; the stretch older than the cap is
# left unclaimed (neither covered nor an explicit gap row), same as the
# "absence is the evidence" convention for the other venues.
KRAKEN_COVERAGE_MAX_MINUTES = 1440  # 1 day


def kraken_coverage_minutes(
    next_minute: int | None, trades: list[KrakenTrade], gap: bool
) -> tuple[list[tuple[int, bool, int]], int | None]:
    """Whole minutes this poll can vouch for, and the new claim cursor.

    `next_minute` is the symbol's first minute not yet claimed (epoch s,
    floored) — a cursor of its OWN, not derived from the last forwarded
    trade's timestamp. Deriving it as "the minute after `last_ts`" (KI-104)
    skipped the minute `last_ts` sat in: that minute was still open when the
    previous poll saw it, and the next poll started one minute later, so on
    a market that prints every minute NO minute was ever claimed — live, the
    only row in 2.5h was the one minute with zero trades.

    Returns `([(minute_epoch, covered, rows_received), ...], next_minute')`:
    one row per whole minute from `next_minute` up to (not including) the
    still-open minute holding this window's newest trade — it hasn't
    finished, matching `CoverageTracker`'s rule. `rows_received` counts the
    trades THIS poll's window holds in that minute (diagnostic only; it
    undercounts a minute whose start predates the window — earlier polls
    saw those trades).

    `covered` is False for the whole stretch when `kraken_gap` found one
    (conservative: the gap's exact extent is unknown). On a gap the lost
    trades lie between the previous `last_ts` and this window's oldest
    trade, which can fall INSIDE the still-open newest minute, so the false
    stretch runs through the oldest trade's minute and the cursor moves past
    it — that minute is written false now and never re-claimed as covered.

    The first poll for a symbol (`next_minute is None`) claims nothing: its
    window's newest minute may begin before the window does, so the cursor
    starts one minute after it.
    """
    if not trades:
        return [], next_minute
    newest_minute = _floor_minute(max(t.event_ts for t in trades).timestamp())
    if next_minute is None:
        return [], newest_minute + 60
    end_minute = newest_minute
    if gap:
        oldest_minute = _floor_minute(min(t.event_ts for t in trades).timestamp())
        end_minute = max(end_minute, oldest_minute + 60)
    start_minute = max(next_minute, end_minute - KRAKEN_COVERAGE_MAX_MINUTES * 60)
    if start_minute >= end_minute:
        return [], next_minute
    counts: dict[int, int] = {}
    for t in trades:
        m = _floor_minute(t.event_ts.timestamp())
        if start_minute <= m < end_minute:
            counts[m] = counts.get(m, 0) + 1
    covered = not gap
    out = []
    minute = start_minute
    while minute < end_minute:
        out.append((minute, covered, counts.get(minute, 0)))
        minute += 60
    return out, end_minute


def _floor_minute(t: float) -> int:
    return int(t // 60) * 60


class CoverageTracker:
    """Which whole minutes a connection was provably alive for.

    Feed it every liveness proof (`proof(t)`) and the receive time of every
    liquidation it delivered (`count(t)`); `confirmed()` hands back each newly
    proven minute exactly once as `(minute_start_epoch_s, rows_received)`.
    Counting by receive time, not exchange time, keeps the count a statement
    about this connection; the events table holds exchange time.

    A proof more than `max_silence_s` after the previous one breaks the chain:
    nothing between them is proven, so the unbroken stretch restarts at the
    new proof. `reset()` (a disconnect) does the same. Minutes are only ever
    confirmed whole — a stretch that begins at 12:00:07 proves 12:01 onward.
    """

    def __init__(self, max_silence_s: float = 30.0):
        self._max_silence = max_silence_s
        self._alive_since: float | None = None
        self._last_proof: float | None = None
        self._next_minute: int | None = None  # first minute not yet emitted
        self._counts: dict[int, int] = {}
        self._flowing = False

    def reset(self) -> None:
        self._flowing = False
        self._alive_since = None
        self._last_proof = None
        self._next_minute = None
        self._counts.clear()

    def proof(self, t: float) -> None:
        if self._last_proof is None or t - self._last_proof > self._max_silence:
            self._alive_since = t
            self._next_minute = _floor_minute(t) + 60 if t % 60 else int(t)
            self._counts.clear()
        self._last_proof = max(t, self._last_proof or t)

    @property
    def last_proof(self) -> float | None:
        return self._last_proof

    def data(self) -> None:
        """This connection's subscription has delivered a data frame."""
        self._flowing = True

    def count(self, t: float, n: int = 1) -> None:
        minute = _floor_minute(t)
        if self._next_minute is not None and minute >= self._next_minute:
            self._counts[minute] = self._counts.get(minute, 0) + n

    def confirmed(self) -> list[tuple[int, int]]:
        """Minutes that ended at or before the latest proof, oldest first."""
        if not self._flowing or self._next_minute is None or self._last_proof is None:
            return []
        out = []
        minute = self._next_minute
        while minute + 60 <= self._last_proof:
            out.append((minute, self._counts.pop(minute, 0)))
            minute += 60
        self._next_minute = minute
        return out

