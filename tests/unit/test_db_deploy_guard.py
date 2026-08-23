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
