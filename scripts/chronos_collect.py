"""Collect Chronos-2 zero-shot forecasts at every LightGBM walk-forward row.

Two subcommands, deliberately in two environments:

  export    (poetry env — reads Postgres) writes the 1m closes the forecasts
            may see, up to the last OOS row, next to the LightGBM OOS cache:
                poetry run python scripts/chronos_collect.py export --symbol BTCUSDT

  forecast  (.venv-forecast or a Kaggle notebook — needs torch, no Postgres)
            forecasts `horizon` bars ahead at every `ts` of the OOS cache and
            appends raw quantiles in chunks, resuming where a killed run
            stopped:
                nice -n 19 .venv-forecast/bin/python scripts/chronos_collect.py \\
                    forecast --symbol BTCUSDT --threads 3

torch never enters the project environment. Meaning — which closes a forecast
may see, how quantiles become P(up) — lives in `model/zero_shot.py` under
test; this file only moves data. On Kaggle (GPU, ~minutes instead of the
~7 h per symbol a niced CPU core takes on the streaming host), upload this
script, `model/zero_shot.py` and the `closes_*`/`oos_*` CSVs as one dataset:
    !pip install -q "chronos-forecasting>=2.0"
    !python /kaggle/input/<dataset>/chronos_collect.py forecast --symbol BTCUSDT \
        --device cuda --dir /kaggle/input/<dataset> --out-dir /kaggle/working
then copy `chronos_*.csv` + `.meta.json` back into `model/artifacts/`.

Pre-registered for the spike (do NOT tune these on the OOS rows): context 512
bars, horizon 15 (= `DEFAULT_HORIZON_BARS`), the model's native quantiles, and
`cross_learning=False` — with it on, the windows in one batch share
information, and later windows would leak into earlier forecasts.

Vault: `Spikes/chronos-forecaster-spike.md`.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from model.zero_shot import context_window, forecast_positions
except ImportError:  # Kaggle: zero_shot.py uploaded beside this script
    from zero_shot import context_window, forecast_positions

MODEL_ID = "amazon/chronos-2"
# 2026-06-05 README-only commit; the weights predate it (2025-10). Both are
# before the first stored bar (2026-07-17), so the scored rows postdate
# pretraining — the contamination check in the spike note.
MODEL_REVISION = "29ec3766d36d6f73f0696f85560a422f50e8498c"
CONTEXT_LEN = 512
HORIZON = 15
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"


def _paths(directory: Path, symbol: str, interval: str,
           out_dir: Path | None = None) -> dict:
    out_dir = out_dir or directory  # Kaggle inputs are read-only
    return {
        "oos": directory / f"oos_{symbol}_{interval}.csv",
        "closes": directory / f"closes_{symbol}_{interval}.csv",
        "out": out_dir / f"chronos_{symbol}_{interval}.csv",
        "meta": out_dir / f"chronos_{symbol}_{interval}.meta.json",
    }


def export(symbol: str, interval: str, directory: Path) -> None:
    from storage.postgres_store import get_price_bars

    paths = _paths(directory, symbol, interval)
    oos = pd.read_csv(paths["oos"], parse_dates=["ts"])
    bars = pd.DataFrame(get_price_bars(symbol, interval, 1_000_000))
    frame = pd.DataFrame({
        "ts": pd.to_datetime(bars["timestamp"], utc=True),
        "close": bars["close"].astype(float),
    }).sort_values("ts")
    frame = frame[frame.ts <= oos.ts.max()]
    frame.to_csv(paths["closes"], index=False)
    print(f"{len(frame):,} closes {frame.ts.min()} -> {frame.ts.max()} "
          f"for {len(oos):,} OOS rows -> {paths['closes']}")


def forecast(symbol: str, interval: str, directory: Path, threads: int,
             batch: int, chunk: int, device: str, out_dir: Path | None) -> None:
    import torch
    from chronos import Chronos2Pipeline

    torch.set_num_threads(threads)
    paths = _paths(directory, symbol, interval, out_dir)
    closes = pd.read_csv(paths["closes"], parse_dates=["ts"])
    index = pd.DatetimeIndex(closes.ts)
    values = closes.close.to_numpy(dtype=np.float64)
    wanted = pd.DatetimeIndex(pd.read_csv(paths["oos"], parse_dates=["ts"]).ts)

    done = set()
    if paths["out"].exists():
        done = set(pd.read_csv(paths["out"], parse_dates=["ts"]).ts)
    todo = wanted[~wanted.isin(done)]
    positions = forecast_positions(index, todo)
    # Validate every window up front: a short context fails now, not at hour 3.
    for pos in (positions.min(), positions.max()) if len(positions) else ():
        context_window(values, int(pos), CONTEXT_LEN)

    pipeline = Chronos2Pipeline.from_pretrained(
        MODEL_ID, revision=MODEL_REVISION, device_map=device
    )
    levels = [float(q) for q in pipeline.quantiles]
    paths["meta"].write_text(json.dumps({
        "model": MODEL_ID, "revision": MODEL_REVISION, "context_len": CONTEXT_LEN,
        "horizon": HORIZON, "quantile_levels": levels, "cross_learning": False,
        "torch": torch.__version__, "device": device,
    }, indent=2))
    print(f"{symbol}: {len(todo):,} to forecast ({len(done):,} cached), "
          f"{len(levels)} quantiles, {threads} threads", flush=True)

    columns = [f"q{q:g}" for q in levels]
    started = time.monotonic()
    for lo in range(0, len(todo), chunk):
        pos = positions[lo : lo + chunk]
        windows = np.stack([context_window(values, int(p), CONTEXT_LEN) for p in pos])
        with torch.no_grad():
            out = pipeline.predict(
                windows[:, None, :].astype(np.float32),
                prediction_length=HORIZON, batch_size=batch, cross_learning=False,
            )
        # each element: (n_variates=1, n_quantiles, prediction_length)
        step = np.stack([o[0, :, HORIZON - 1].float().cpu().numpy() for o in out])
        rows = pd.DataFrame(step, columns=columns)
        rows.insert(0, "close", values[pos])
        rows.insert(0, "ts", todo[lo : lo + chunk])
        rows.to_csv(paths["out"], mode="a", index=False,
                    header=not paths["out"].exists())
        n = lo + len(pos)
        rate = n / (time.monotonic() - started)
        print(f"  {n:,}/{len(todo):,}  {rate:.0f}/s  "
              f"eta {(len(todo) - n) / rate / 60:.1f} min", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("export", "forecast"))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where forecasts go (default --dir; on Kaggle "
                             "/kaggle/working, since inputs are read-only)")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--batch", type=int, default=256)
    parser.add_argument("--chunk", type=int, default=2048)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    if args.command == "export":
        export(symbol, args.interval, args.dir)
    else:
        forecast(symbol, args.interval, args.dir, args.threads, args.batch,
                 args.chunk, args.device, args.out_dir)


if __name__ == "__main__":
    main()
