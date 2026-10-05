"""R3-a A0: fetch listing METADATA only (no prices, no returns).

Sources (all free, public, keyless):
- data.binance.vision S3 listing: UM monthly fundingRate directory names and,
  per symbol, the *file names* of its monthly fundingRate archives (the first
  file's month is a month-resolution listing proxy; delisted symbols kept).
- Binance fapi exchangeInfo: `onboardDate` (ms) per symbol (live + settling).
- Kraken Futures instruments: `openingDate` per PF_ contract (live only).
- Kraken Futures tickers: bid/ask/volume24h (spread + volume metadata only).

Nothing here downloads a kline, a funding value or a mark price. Cached under
data/archive/r3a_meta/ (gitignored). Rerunnable.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "archive" / "r3a_meta"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"


def _get(url: str) -> bytes:
    for attempt in range(4):
        try:
            return urllib.request.urlopen(url, timeout=60).read()
        except Exception:  # noqa: BLE001 - retry transient network errors
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def s3_list(prefix: str) -> list[str]:
    """Return child prefixes + keys under `prefix` (names only)."""
    out: list[str] = []
    marker = ""
    while True:
        q = urllib.parse.urlencode({"delimiter": "/", "prefix": prefix, "marker": marker})
        text = _get(f"{S3}?{q}").decode()
        prefixes = re.findall(r"<Prefix>([^<]+)</Prefix>", text)[1:]
        keys = re.findall(r"<Key>([^<]+)</Key>", text)
        out += prefixes + keys
        if "<IsTruncated>true" in text:
            marker = (prefixes + keys)[-1]
        else:
            return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    dirs = s3_list("data/futures/um/monthly/fundingRate/")
    symbols = sorted(d.rstrip("/").split("/")[-1] for d in dirs)
    first_month: dict[str, str | None] = {}
    cache = OUT / "s3_first_month.json"
    if cache.exists():
        first_month = json.loads(cache.read_text())
    for i, sym in enumerate(symbols):
        if sym in first_month:
            continue
        keys = s3_list(f"data/futures/um/monthly/fundingRate/{sym}/")
        months = sorted(m for k in keys
                        for m in re.findall(r"fundingRate-(\d{4}-\d{2})\.zip$", k))
        first_month[sym] = months[0] if months else None
        if i % 50 == 0:
            cache.write_text(json.dumps(first_month))
            print(f"{i}/{len(symbols)}", file=sys.stderr)
    cache.write_text(json.dumps(first_month, indent=0, sort_keys=True))
    (OUT / "binance_exchangeinfo.json").write_bytes(
        _get("https://fapi.binance.com/fapi/v1/exchangeInfo"))
    (OUT / "kraken_instruments.json").write_bytes(
        _get("https://futures.kraken.com/derivatives/api/v3/instruments"))
    (OUT / "kraken_tickers.json").write_bytes(
        _get("https://futures.kraken.com/derivatives/api/v3/tickers"))
    print(f"symbols={len(symbols)} with_first_month="
          f"{sum(v is not None for v in first_month.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
