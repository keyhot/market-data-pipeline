#!/usr/bin/env python3
"""Offline screenshots for the README, from example data.

Runs the real FastAPI app and the real pages on a throwaway local port, with
every storage read replaced by a seeded fake, and photographs them with the
CDP session from scripts/shoot.py. The numbers in the pictures are synthetic:
a seeded random walk and made-up events. Caption them "example data".

Nothing here can reach a database or the network:

* DATABASE_URL is removed from the environment, never set;
* storage.db.get_pool, ConnectionPool and psycopg.connect are replaced with
  functions that raise, before the app is imported;
* the scheduler, the Binance ingester and CSV snapshots are switched off;
* the server binds 127.0.0.1:8765, not the stack's port 8000.

    poetry run python tools/readme_shots.py --out docs/images
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
SEED = 20261101
BARS = 250
SYMBOLS = {"BTCUSDT": 64_000.0, "ETHUSDT": 3_200.0}
SIGNAL_P_MIN = 0.52

# name, path, viewport, seconds to wait after the page reports it has drawn
SHOTS = [
    ("hero-room", "/world", (1920, 960), 5.0),
    ("charts", "/charts?interval=1m", (1280, 800), 3.0),
    ("cast", "/world?gallery=1", (1920, 960), 5.0),
]
WIDTH = 1280
WEBP_QUALITY = 80
MAX_BYTES = 400 * 1024


def _refuse_database() -> None:
    """Make a database connection impossible, before anything can try one."""
    for name in ("DATABASE_URL", "MDP_TEST_DATABASE_URL"):
        os.environ.pop(name, None)
    os.environ["SCHEDULER_ENABLED"] = "0"
    os.environ["WS_INGEST_ENABLED"] = "0"
    os.environ["CSV_WRITE_ENABLED"] = "0"
    os.environ["POSTGRES_WRITE_ENABLED"] = "0"

    import psycopg

    import storage.db as db

    def refuse(*_args, **_kwargs):
        raise RuntimeError("readme_shots: database access is refused")

    db.get_pool = refuse
    db.ConnectionPool = refuse
    db.connect_read_only = refuse
    psycopg.connect = refuse


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def make_bars(symbol: str, start: float, rng: random.Random) -> list[dict]:
    """A seeded random walk of 1m bars ending at the current minute."""
    t0 = _now() - timedelta(minutes=BARS - 1)
    price, out = start, []
    for i in range(BARS):
        open_ = price
        close = open_ * (1 + rng.gauss(0.00002, 0.0006))
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.0003)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.0003)))
        out.append(
            {
                "timestamp": (t0 + timedelta(minutes=i)).isoformat(),
                "open": round(open_, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "volume": round(rng.uniform(5, 60), 3),
            }
        )
        price = close
    return out


def make_signals(bars: list[dict], rng: random.Random) -> list[dict]:
    """Model calls every ~12 bars, resolved 15 bars later, newest first."""
    out = []
    for i in range(5, BARS - 1, 12):
        up = rng.random() < 0.5
        p = round(rng.uniform(SIGNAL_P_MIN, 0.9), 4)
        resolved = i + 15 < BARS
        outcome = None
        if resolved:
            went_up = bars[i + 15]["close"] > bars[i]["close"]
            outcome = "win" if went_up == up else "loss"
        out.append(
            {
                "signal_timestamp": bars[i]["timestamp"],
                "model_version": "example",
                "horizon_bars": 15,
                "direction": "up" if up else "down",
                "probability": p,
                "resolved_at": bars[i + 15]["timestamp"] if resolved else None,
                "outcome": outcome,
            }
        )
    return list(reversed(out))


def make_events(rng: random.Random) -> list[dict]:
    """Made-up world events, newest first, in the shape get_world_events gives."""
    now = _now()
    kinds = ["big_move", "volatility_spike", "streak", "signal_resolved"]
    rows = []
    for n in range(120):
        when = now - timedelta(minutes=2 + 3 * n)
        kind = rng.choices(kinds, weights=[3, 2, 2, 5])[0]
        symbol = rng.choice(list(SYMBOLS))
        payload: dict = {}
        severity = round(rng.uniform(2.0, 4.0), 2)
        if kind == "big_move":
            payload = {"return": round(rng.gauss(0, 0.004), 5), "sigmas": severity}
        elif kind == "streak":
            payload = {
                "bars": rng.randint(4, 8),
                "direction": rng.choice(["up", "down"]),
            }
        elif kind == "signal_resolved":
            win = rng.random() < 0.46
            p = round(rng.uniform(SIGNAL_P_MIN, 0.9), 3)
            severity = round(abs(p - 0.5) * 2 * (1 if win else 2), 3)
            payload = {
                "outcome": "win" if win else "loss",
                "direction": rng.choice(["up", "down"]),
                "probability": p,
                "realized_return": round(
                    abs(rng.gauss(0, 0.0008)) * (1 if win else -1), 5
                ),
            }
        rows.append(
            {
                "id": 1000 - n,
                "occurred_at": when.isoformat(),
                "event_type": kind,
                "symbol": symbol,
                "severity": severity,
                "payload": payload,
            }
        )
    return rows


def _install_fakes() -> None:
    """Replace every storage name the routers imported, then the ledger check."""
    import api.main
    import api.routers.charts as charts
    import api.routers.ops as ops
    import api.routers.sse as sse
    import api.routers.stored as stored
    import api.routers.world as world

    rng = random.Random(SEED)
    bars = {s: make_bars(s, start, rng) for s, start in SYMBOLS.items()}
    signals = {s: make_signals(bars[s], rng) for s in SYMBOLS}
    events = make_events(rng)

    def get_price_bars(symbol, interval="1m", limit=100):
        return bars.get(symbol.upper(), [])[-limit:]

    def get_signals(symbol, interval="1m", limit=50):
        return signals.get(symbol.upper(), [])[:limit]

    def get_signal_accuracy(symbol, interval="1m", window=50):
        done = [x for x in get_signals(symbol, interval, 500) if x["outcome"]]
        wins = sum(x["outcome"] == "win" for x in done[:window])
        n = min(len(done), window)
        return {
            "symbol": symbol.upper(),
            "interval": interval,
            "window": window,
            "resolved": n,
            "wins": wins,
            "losses": n - wins,
            "hit_rate": wins / n if n else None,
            "current_streak": 1,
            "streak_outcome": done[0]["outcome"] if done else None,
        }

    def get_model_accuracy(symbols, interval="1m", window=50):
        recent = [e for e in events if e["event_type"] == "signal_resolved"][:window]
        wins = sum(e["payload"]["outcome"] == "win" for e in recent)
        return {
            "window": window,
            "resolved": len(recent),
            "wins": wins,
            "losses": len(recent) - wins,
            "hit_rate": wins / len(recent),
            "per_symbol": {},
        }

    def get_world_events(limit=50, event_type=None, symbol=None, since=None):
        rows = [
            e
            for e in events
            if (event_type is None or e["event_type"] == event_type)
            and (symbol is None or e["symbol"] == symbol.upper())
        ]
        return rows[:limit]

    def get_latest_closes(symbols):
        return [
            {
                "symbol": s,
                "timestamp": bars[s][-1]["timestamp"],
                "close": bars[s][-1]["close"],
            }
            for s in symbols
            if s in bars
        ]

    for mod in (world, stored, sse):
        for name, fn in (
            ("get_price_bars", get_price_bars),
            ("get_world_events", get_world_events),
            ("get_model_accuracy", get_model_accuracy),
            ("get_signals", get_signals),
            ("get_signal_accuracy", get_signal_accuracy),
        ):
            if hasattr(mod, name):
                setattr(mod, name, fn)
    charts.get_latest_closes = get_latest_closes
    ops.get_now_playing = lambda: None
    api.main._run_ledger_check = lambda: None


def _to_webp(png: bytes, out: Path) -> Path:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width > WIDTH:
        img = img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "WEBP", quality=WEBP_QUALITY, method=6)
    if out.stat().st_size > MAX_BYTES:
        raise SystemExit(f"{out.name} is {out.stat().st_size} bytes; over the cap")
    return out


def _capture(session, path: str, viewport: tuple[int, int], settle: float) -> bytes:
    from scripts.shoot import _READY_JS, frame_is_blank

    session.cmd("Page.enable")
    session.cmd(
        "Emulation.setDeviceMetricsOverride",
        width=viewport[0],
        height=viewport[1],
        deviceScaleFactor=1,
        mobile=False,
    )
    session.cmd("Page.navigate", url=BASE + path)
    for _ in range(120):
        time.sleep(0.25)
        value = (
            session.cmd("Runtime.evaluate", expression=_READY_JS, returnByValue=True)
            .get("result", {})
            .get("value")
        )
        if value in ("drawn", "canvas"):
            break
    else:
        raise RuntimeError(f"{path} never reported a drawn frame")
    time.sleep(settle)
    png = base64.b64decode(session.cmd("Page.captureScreenshot", format="png")["data"])
    if frame_is_blank(png):
        raise SystemExit(f"REFUSED: the capture of {path} is blank; nothing written")
    return png


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument("--only", nargs="*", help="shot names to take")
    args = parser.parse_args()

    _refuse_database()
    _install_fakes()

    import uvicorn

    from api.main import app
    from scripts.shoot import _Session

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        raise SystemExit("the local server did not start")

    profile = Path(tempfile.mkdtemp(prefix="readme-shots-chrome-"))
    session = _Session(9333, profile)
    try:
        for name, path, viewport, settle in SHOTS:
            if args.only and name not in args.only:
                continue
            png = _capture(session, path, viewport, settle)
            out = _to_webp(png, args.out / f"{name}.webp")
            print("wrote", out, out.stat().st_size, "bytes")
    finally:
        session.close()
        shutil.rmtree(profile, ignore_errors=True)
        server.should_exit = True
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
