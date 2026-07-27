"""Director service (Sprint 13): salience-driven OBS scene switching and
in-character commentary over the append-only world_events stream. Pure
decisions live in ``policy.py``; all I/O (OBS, TTS, DB, HTTP) lives in
``service.py`` — the same split as scripts/stream_watchdog.py.
"""
