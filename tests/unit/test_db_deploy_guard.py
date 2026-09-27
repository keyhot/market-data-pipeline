"""The dev/prod guard on the connection pool.

After the hosting migration there are two Postgres instances and one of them
is the world's memory. `world_events` is append-only, so a write from a dev
session into the production database cannot be deleted — and with an SSH
tunnel open for visual QA, both databases answer on `localhost:5432`. The URL
cannot tell them apart, so the database identifies itself and the pool opens
read-only whenever the process and the database disagree about which is which.
"""

import pytest

import storage.db as db


@pytest.fixture(autouse=True)
def reset_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    yield
    monkeypatch.setattr(db, "_pool", None)


class TestReadOnlyRequired:
    def test_matching_roles_allow_writes(self):
        assert db.read_only_required("dev", "dev") is False

    def test_dev_process_against_prod_database_is_read_only(self):
        # The tunnel case: `ssh -L 5432:localhost:5432 stream-a1` open for
        # visual QA, and DATABASE_URL still says localhost.
        assert db.read_only_required("dev", "prod") is True

    def test_prod_process_against_dev_database_is_read_only(self):
        # The mirror image: the box restored from a laptop dump and nobody
        # re-stamped the role. Failing loudly beats writing the world's
        # memory into a throwaway database.
        assert db.read_only_required("prod", "dev") is True

    def test_unmarked_database_is_not_forced_read_only(self):
        # Volumes created before deployment_identity existed report None.
        assert db.read_only_required("dev", None) is False


class TestDeployRole:
    def test_defaults_to_dev(self, monkeypatch):
        monkeypatch.delenv("DEPLOY_ROLE", raising=False)
        assert db.deploy_role() == "dev"

    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_ROLE", "prod")
        assert db.deploy_role() == "prod"

    def test_is_case_and_whitespace_insensitive(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_ROLE", "  PROD ")
        assert db.deploy_role() == "prod"


class TestPoolWiring:
    def _capture(self, monkeypatch, *, database_role, deploy_role):
        captured = {}

        class FakePool:
            def __init__(self, url, **kwargs):
                captured["url"] = url
                captured["kwargs"] = kwargs

        monkeypatch.setattr(db, "ConnectionPool", FakePool)
        monkeypatch.setattr(db, "_database_role", lambda url: database_role)
        monkeypatch.setenv("DEPLOY_ROLE", deploy_role)
        db.get_pool()
        return captured

    def test_mismatch_opens_the_pool_read_only(self, monkeypatch):
        captured = self._capture(
            monkeypatch, database_role="prod", deploy_role="dev"
        )
        assert (
            captured["kwargs"]["kwargs"]["options"]
            == "-c default_transaction_read_only=on"
        )

    def test_match_opens_a_normal_writable_pool(self, monkeypatch):
        captured = self._capture(
            monkeypatch, database_role="prod", deploy_role="prod"
        )
        assert "options" not in captured["kwargs"].get("kwargs", {})

    def test_mismatch_is_logged_at_error(self, monkeypatch, caplog):
        with caplog.at_level("ERROR", logger=db.__name__):
            self._capture(monkeypatch, database_role="prod", deploy_role="dev")
        assert any(
            "read-only" in record.getMessage().lower() for record in caplog.records
        )


class TestRoleProbeLatency:
    """The guard added a connect to the cold path, and the cold path is what
    `/health` answers with. `scripts/stream_watchdog.py` reads `/health` with
    `content_timeout_seconds = 5.0` and counts a real outage after 3 consecutive
    failures (KI-024), so the probe plus ping() has to fit inside that budget
    when Postgres is unreachable — otherwise a Postgres outage would also start
    manufacturing false *content* outages.
    """

    _WATCHDOG_CONTENT_TIMEOUT = 5.0
    _PING_TIMEOUT = 2.0  # storage.db.ping's default

    def test_probe_plus_ping_fits_inside_the_watchdog_content_timeout(self):
        worst_case = db._ROLE_PROBE_TIMEOUT_SECONDS + self._PING_TIMEOUT
        assert worst_case < self._WATCHDOG_CONTENT_TIMEOUT

    def test_the_probe_actually_passes_that_timeout_to_psycopg(self, monkeypatch):
        captured = {}

        def fake_connect(url, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("no database here")

        monkeypatch.setattr(db.psycopg, "connect", fake_connect)
        assert db._database_role("postgresql://u:p@127.0.0.1:1/d") is None
        assert captured["connect_timeout"] == db._ROLE_PROBE_TIMEOUT_SECONDS

    def test_the_probe_bounds_its_query_as_well_as_its_connect(self, monkeypatch):
        # KI-069 review: `connect_timeout` ends at the handshake. A database
        # that accepts connections but answers slowly (a lock on
        # deployment_identity, an overloaded server) held the probe's SELECT
        # — and `_pool_lock`, and the cold `/health` behind it — for as long
        # as it liked: 6.93s measured, past the watchdog's 5s.
        captured = {}

        def fake_connect(url, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("no database here")

        monkeypatch.setattr(db.psycopg, "connect", fake_connect)
        db._database_role("postgresql://u:p@127.0.0.1:1/d")
        expected_ms = int(db._ROLE_PROBE_TIMEOUT_SECONDS * 1000)
        assert f"-c statement_timeout={expected_ms}" in captured["options"]


class TestClientSideBounds:
    """KI-080: `statement_timeout` is a server-side cancel and
    `connect_timeout` ends at the handshake, so a network path that drops
    silently mid-query left the client waiting on its own TCP stack with no
    limit. The bounds libpq enforces on the client go on every connection —
    the pool's and the cold role probe's — and sit beside the read-only
    guard's `options`, never in place of them."""

    def _pool_kwargs(self, monkeypatch, *, database_role="dev"):
        captured = {}

        class FakePool:
            def __init__(self, url, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(db, "ConnectionPool", FakePool)
        monkeypatch.setattr(db, "_database_role", lambda url: database_role)
        monkeypatch.setenv("DEPLOY_ROLE", "dev")
        db.get_pool()
        return captured["kwargs"]

    def test_the_pool_bounds_connect_and_a_silent_socket(self, monkeypatch):
        kwargs = self._pool_kwargs(monkeypatch)
        assert kwargs["connect_timeout"] >= 2  # libpq's floor
        assert kwargs["keepalives"] == 1
        assert kwargs["tcp_user_timeout"] > 0

    def test_keepalive_detection_matches_the_user_timeout(self, monkeypatch):
        # On Linux `tcp_user_timeout` also ends keepalive probing, so a
        # user timeout shorter than idle + interval * count would silently
        # cut the probes short, and a longer one would be a bound that never
        # fires first. They are one number, stated twice.
        k = self._pool_kwargs(monkeypatch)
        detect_s = k["keepalives_idle"] + k["keepalives_interval"] * k[
            "keepalives_count"
        ]
        assert k["tcp_user_timeout"] == detect_s * 1000

    def test_the_bounds_sit_beside_the_read_only_guard(self, monkeypatch):
        kwargs = self._pool_kwargs(monkeypatch, database_role="prod")
        assert kwargs["options"] == db._READ_ONLY_OPTIONS
        assert kwargs["keepalives"] == 1

    def test_the_role_probe_carries_the_same_socket_bounds(self, monkeypatch):
        captured = {}

        def fake_connect(url, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("no database here")

        monkeypatch.setattr(db.psycopg, "connect", fake_connect)
        db._database_role("postgresql://u:p@127.0.0.1:1/d")
        for key, value in db._SOCKET_BOUNDS.items():
            assert captured[key] == value, key
        # ...without losing its own, shorter connect bound.
        assert captured["connect_timeout"] == db._ROLE_PROBE_TIMEOUT_SECONDS


class TestPingBoundsItsQuery:
    def test_ping_sets_a_statement_bound_before_select(self, monkeypatch):
        # A server too loaded to answer `SELECT 1` held ping for as long as
        # it liked on a warm pool: the acquire timeout had already been met.
        executed = []

        class FakeConn:
            def execute(self, sql, *args):
                executed.append(sql)

        class FakeCtx:
            def __enter__(self):
                return FakeConn()

            def __exit__(self, *exc):
                return False

        class FakePool:
            def connection(self, timeout):
                return FakeCtx()

        monkeypatch.setattr(db, "_pool", FakePool())
        assert db.ping(timeout_seconds=1.5) is True
        assert executed[0] == "SET LOCAL statement_timeout = 1500"
        assert executed[-1] == "SELECT 1"

    def test_a_tiny_timeout_never_becomes_unbounded(self, monkeypatch):
        # statement_timeout = 0 means "no limit", so rounding must not reach it.
        executed = []

        class FakeConn:
            def execute(self, sql, *args):
                executed.append(sql)

        class FakeCtx:
            def __enter__(self):
                return FakeConn()

            def __exit__(self, *exc):
                return False

        class FakePool:
            def connection(self, timeout):
                return FakeCtx()

        monkeypatch.setattr(db, "_pool", FakePool())
        db.ping(timeout_seconds=0.0004)
        assert executed[0] == "SET LOCAL statement_timeout = 1"
