"""Forced-liquidation feeds, parsed (C1 · the liquidations recorder).

No venue serves free liquidation *history* (vault `Spikes/forward-data-collector-spike.md`,
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
500ms; OKX's completeness is unverified. The pre-registration (vault `Docs/liquidation-cascade-experiment.md`)
says how each venue may be used.

Pure: no network, no DB, no clock. The runner supplies all three.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

BINANCE = "binance"
OKX = "okx"
BYBIT = "bybit"

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

