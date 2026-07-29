"""Broadcast lifecycle package (Sprint 14 — Autonomous Broadcast & Watchability).

Mirrors the watchdog/director shape: a pure `tick()` decision function over
injected state, a thin runner, and a systemd unit. The `YouTubeLiveClient`
wraps YouTube Data API v3 so every decision tests against a `FakeYouTubeClient`
with no network.
"""
