"""KI-080 against a real Postgres: a socket that stops passing packets.

A userspace proxy forwards to the real database, then starts reading and
dropping. That is the shape of the planned SSH-tunnel topology: psycopg's
socket goes to a local peer that keeps ACKing, so no TCP-level bound
(keepalives, `tcp_user_timeout`) ever fires. Before the fix `/health` hung
here indefinitely (measured past 15s); it must now answer inside its budget
and say the database is not connected.

Reads only, no locks and no writes, so unlike test_health_budget.py this is
safe against any database; it still runs only against the throwaway one
MDP_TEST_DATABASE_URL names (tests/integration/conftest.py, KI-106).
"""

import socket
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from storage import db
from storage.db import get_pool

_BUDGET = 1.0
_SLACK = 0.75


class _StallProxy:
    def __init__(self, host: str, port: int):
        self._upstream = (host, port)
        self.stalled = threading.Event()
        self._socks: list[socket.socket] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(self._upstream)
            self._socks += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, src, dst):
        try:
            while data := src.recv(65536):
                if not self.stalled.is_set():
                    dst.sendall(data)
        except OSError:
            pass

    def close(self):
        self._listener.close()
        for s in self._socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
                s.close()
            except OSError:
                pass


@pytest.fixture
def proxied_pool(monkeypatch):
    real = conninfo_to_dict(get_pool().conninfo)
    proxy = _StallProxy(real.get("host", "localhost"), int(real.get("port", 5432)))
    url = make_conninfo(**{**real, "host": "127.0.0.1", "port": str(proxy.port)})
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    try:
        yield proxy
    finally:
        # Closing the proxy's sockets errors out whatever is still stuck.
        proxy.close()
        from api.routers import ops

        ops._health_db_wait_idle()
        if db._pool is not None:
            db._pool.close(timeout=1)


def test_health_answers_within_its_budget_through_a_stalled_socket(proxied_pool):
    from api.main import app

    client = TestClient(app)
    with patch("api.routers.ops._HEALTH_DB_BUDGET_SECONDS", _BUDGET):
        warm = client.get("/health").json()["data"]
        assert warm["postgres"]["connected"] is True  # the proxy forwards

        proxied_pool.stalled.set()
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["postgres"]["connected"] is False
    assert data["world_unavailable_reason"] == "not_connected"
    assert elapsed < _BUDGET + _SLACK
