# Streaming Runbook (Sprint 11)

Zero-to-live procedure for the stream. Everything code-side is automated; the
few user-machine steps are marked **[user step]**.

## 1. OBS install + websocket

- OBS Studio is installed via apt (32.1.2 on this host). obs-websocket v5 ships
  built in since OBS 28.
- **[user step]** First launch: run `obs`, skip/complete the auto-config wizard,
  then Tools → WebSocket Server Settings → *Enable WebSocket server*, port 4455,
  set the password to the `OBS_WS_PASSWORD` value from `.env`.
  - Alternative (no UI): drop this into
    `~/.config/obs-studio/plugin_config/obs-websocket/config.json` before
    launching OBS (values must match `.env`):

    ```json
    {
        "alerts_enabled": false,
        "auth_required": true,
        "first_load": false,
        "server_enabled": true,
        "server_password": "<OBS_WS_PASSWORD from .env>",
        "server_port": 4455
    }
    ```

- Verify: `poetry run python scripts/stream_ctl.py status` → JSON with
  `obs_version`. Exit code 2 means the websocket is unreachable.

## 2. Environment keys (`.env`, never committed)

| Key | Meaning |
|---|---|
| `OBS_WS_URL` | websocket URL, default `ws://127.0.0.1:4455` |
| `OBS_WS_PASSWORD` | websocket password (matches OBS setting) |
| `STREAM_PAGE_BASE` | where browser sources load pages, default `http://localhost:8000` |
| `OBS_STREAM_SERVER` | RTMP ingest, default YouTube `rtmp://a.rtmp.youtube.com/live2` |
| `OBS_STREAM_KEY` | **[user step]** platform stream key — credentials never touch the repo or chat |
| `STREAM_AUDIO_DIR` | directory of DMCA-safe audio loops (optional) |
| `STREAM_EVENT_SPOOL` | JSONL fallback for lifecycle events when Postgres is down |

## 3. Scene build

```bash
docker compose up -d          # stack must be up: pages are the scene
poetry run python scripts/stream_ctl.py build
```

Layout constants live in `scripts/stream_scene.py` (`SCENE_SPEC`): chart
1920×840 top, signals strip 1920×120 at the bottom edge, events rail 480×840
on the right. `build` is idempotent — re-running updates settings, never
duplicates sources. Browser sources run 30fps with shutdown-when-hidden off
(SSE must stay alive).

## 4. Audio bed

- **[user step]** Put DMCA-safe loops into a local folder and set
  `STREAM_AUDIO_DIR`. Source options, simplest first: StreamBeats (free, no
  attribution), YouTube Audio Library (check per-track attribution), locally
  generated loops.
- Re-run `stream_ctl build` — the `audio-bed` VLC source appears (VLC must be
  installed for OBS's VLC source; otherwise install `vlc` via apt).
- Target level: −18 dB in the OBS mixer — headroom for a future TTS voice.

### Licensing record (the DMCA trap is legal, not technical)

| Track/folder | Source | License basis |
|---|---|---|
| _(fill as audio lands — zero tracks of unknown provenance)_ | | |

## 5. Go-live checklist

1. Stack healthy: `curl localhost:8000/health` green, latest 1m bar fresh.
2. OBS reachable: `stream_ctl status` exit 0.
3. Scene built: `stream_ctl build` returns `"created": []`.
4. Audio playing, level ≈ −18 dB.
5. **[user step]** Stream key in `.env` (`OBS_STREAM_KEY`), then
   `stream_ctl configure-output`.
6. Bitrate sane for measured upload bandwidth: start 4500 kbps 1080p30, or
   2500 kbps 720p30 on thin uplinks. Encoder: check what OBS auto-config picked
   (VAAPI if available on this box, else x264 veryfast).
7. `stream_ctl start` — records a `stream_started` world event.
8. Verify on the platform's own dashboard/player. **First stream unlisted.**
9. Watchdog running (below).

Platform note: YouTube = unlimited length + automatic VODs; Twitch = better
discovery, stricter VOD retention. User picks; server URL goes in
`OBS_STREAM_SERVER`.

## 6. Watchdog

`scripts/stream_watchdog.py` polls every 30s: relaunches OBS when unreachable,
rebuilds the scene after recovery, restarts an inactive stream, records
`stream_dropped` (never restarts) on dropped-frame ratio ≥ 5%. Backoff: max one
restart attempt per 5 minutes. All transitions land in `world_events` (spooled
to `STREAM_EVENT_SPOOL` if Postgres is down, flushed on reconnect).

Run as a systemd user service, `~/.config/systemd/user/stream-watchdog.service`:

```ini
[Unit]
Description=Stream watchdog (market-data-pipeline)

[Service]
WorkingDirectory=%h/Projects/market-data-pipeline
ExecStart=/usr/bin/env poetry run python scripts/stream_watchdog.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now stream-watchdog
loginctl enable-linger "$USER"   # keep it running without an open session
```

## 7. Soak test

24h with the whole chain live, watchdog on, nothing babysat. Mid-soak, kill OBS
once (`pkill -x obs`) so the report includes a measured recovery. Then:

```bash
poetry run python scripts/soak_report.py --hours 24 > docs/soak-report-$(date +%F).md
```

Uptime is computed from `stream_*` world events — the log built this sprint is
the measurement tool. Every failure gets a line: what died, detection, recovery
time, needed fix. Failures found here are the soak's success output.

## 8. Troubleshooting

- **Websocket auth failure**: password mismatch between `.env` and OBS →
  re-check Tools → WebSocket Server Settings; `stream_ctl` exit 2 = unreachable
  (OBS down or wrong port), auth errors surface as connection errors too.
- **Encoder overload** (dropped frames, high CPU): lower preset (x264
  veryfast → superfast) or drop to 720p30; the watchdog records
  `stream_dropped(reason=dropped_frames)` but intentionally never restarts —
  restarts make congestion worse.
- **RTMP disconnects**: watchdog restarts the stream (backoff 5 min); check the
  platform's ingest status page and upload bandwidth before suspecting OBS.
- **Overlays blank in scene**: stack down (`docker compose up -d`) or
  `STREAM_PAGE_BASE` wrong; browser sources need `localhost:8000` reachable
  from the host OBS runs on.
