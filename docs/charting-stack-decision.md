# L3 Charting Stack Decision

**Decision: TradingView Lightweight Charts** (standalone CDN bundle), rendered
client-side in plain HTML pages served by FastAPI. Data comes from the existing
`/bars/{symbol}` JSON endpoint — no server-side templating engine, no build step,
no new Python dependencies.

## Why

- Candlestick-native: OHLC series are a first-class type, not a plugin.
- Tiny (~45 KB gzipped) and dependency-free; loads from a CDN `<script>` tag.
- Already the pick for the L4 stream overlays in docs/architecture-vision.md —
  choosing it now means L3 pages are directly reusable as stream scenes.
- Battle-tested at TradingView; handles pan/zoom/crosshair out of the box.

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Plotly.js | Great candlesticks, but ~3.5 MB and pulls a large API surface for two pages. |
| Chart.js + chartjs-chart-financial | Financial charts live in a plugin with sparse maintenance. |
| Apache ECharts | Capable, but heavier and the candlestick styling fights the defaults. |
| Server-rendered PNG (matplotlib/mplfinance) | No interactivity; dead end for the live L4 overlays. |

## Constraints

- License: Apache 2.0 **with an attribution requirement** — the TradingView
  attribution link must stay visible on chart pages.
- CDN script means chart pages need internet access in the browser. Acceptable
  for L3; revisit (vendor the file) when the L4 stream box goes always-on.
- Version pinned exactly (`lightweight-charts@4.2.0`) with a Subresource
  Integrity hash on the script tag, so a compromised or shifted CDN file
  fails closed instead of executing.

## How it's wired (Sprint 7)

`GET /chart/{symbol}` serves `api/templates/chart.html` with the symbol
substituted; the page fetches `/bars/{symbol}?limit=250` and feeds a
candlestick series. `GET /dashboard` is a server-rendered table (no JS library)
linking to per-symbol charts.
