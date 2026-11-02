"""S22: tests/net_guard.py refuses what the unit suite must not reach, on fakes.

Nothing here opens a connection to anything but a listener the test itself
bound on an ephemeral loopback port. The refusals are exercised either through
the autouse guard (which raises before the real connect is reached) or over a
tripwire standing in for the real connect, so a regression fails the test
instead of reaching a service."""

import socket
from pathlib import Path

import net_guard
import pytest


@pytest.mark.parametrize(
    "host, port",
    [
        ("127.0.0.1", 8000),
        ("127.5.5.5", 9),
        ("::1", 9),
        ("::ffff:127.0.0.1", 9),
        ("localhost", 80),
        ("0.0.0.0", 80),
        ("LOCALHOST", 80),
        ("localhost.", 80),
    ],
)
def test_loopback_is_allowed_on_any_port_but_the_postgres_ones(host, port):
    assert net_guard.connect_reason(host, port) is None


@pytest.mark.parametrize(
    "host, port",
    [
        ("93.184.216.34", 80),
        ("10.0.0.5", 443),
        ("2606:2800:220:1::1", 443),
        ("freqtrade", 8080),
        ("query1.finance.yahoo.com", 443),
        ("127.0.0.1", 5432),
        ("127.0.0.1", 55432),
        ("::1", 5432),
        ("localhost", 55432),
        ("0.0.0.0", 5432),
    ],
)
def test_everything_else_is_refused(host, port):
    assert net_guard.connect_reason(host, port)


def test_lookups_refuse_names_and_pass_numeric_and_loopback():
    for host in (None, "", "localhost", "127.0.0.1", "::1", "93.184.216.34"):
        assert net_guard.lookup_reason(host) is None
    for host in ("freqtrade", "freqtrade.invalid", "query1.finance.yahoo.com"):
        assert net_guard.lookup_reason(host)


def test_integration_is_outside_the_guard():
    assert net_guard.applies_to(Path("tests/unit/test_x.py"))
    assert net_guard.applies_to(Path("tests/api/test_x.py"))
    assert not net_guard.applies_to(Path("/repo/tests/integration/test_x.py"))
    root = Path("/work/integration/repo")  # a checkout directory named integration
    assert net_guard.applies_to(root / "tests/unit/test_x.py", root)
    assert not net_guard.applies_to(root / "tests/integration/test_x.py", root)


def test_a_connect_to_the_live_port_fails_with_the_guard_message_and_never_connects(
    monkeypatch,
):
    reached = []
    monkeypatch.setattr(
        socket.socket, "connect", lambda self, address: reached.append(address)
    )
    attempts = net_guard.install(monkeypatch, "tests/unit/probe.py::test_probe")
    with pytest.raises(net_guard.NetworkBlocked) as refusal:
        socket.socket().connect(("127.0.0.1", 5432))
    assert "tests/unit/probe.py::test_probe" in str(refusal.value)
    assert "the unit suite reaches no network" in str(refusal.value)
    assert reached == []
    assert attempts == ["connect 127.0.0.1:5432"]
    socket.socket().connect(("127.0.0.1", 8000))  # the tripwire, not a network
    assert reached == [("127.0.0.1", 8000)]


def test_the_autouse_guard_names_this_test_and_records_a_swallowed_attempt(
    no_network_outside_integration,
):
    attempts = no_network_outside_integration
    sock = socket.socket()
    try:
        with pytest.raises(net_guard.NetworkBlocked, match="test_the_autouse_guard"):
            sock.connect(("127.0.0.1", 55432))
        with pytest.raises(net_guard.NetworkBlocked, match="test_the_autouse_guard"):
            sock.connect_ex(("93.184.216.34", 443))
        with pytest.raises(net_guard.NetworkBlocked):
            socket.getaddrinfo("freqtrade", 8080)
        with pytest.raises(net_guard.NetworkBlocked):
            socket.create_connection(("freqtrade.invalid", 8080))
        with pytest.raises(net_guard.NetworkBlocked):
            socket.gethostbyname_ex("freqtrade")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
            with pytest.raises(net_guard.NetworkBlocked, match="sendto"):
                datagram.sendto(b"x", ("93.184.216.34", 53))
    finally:
        sock.close()
    assert len(attempts) == 6
    attempts.clear()  # provoked on purpose; otherwise teardown fails the test


def test_libcurl_is_refused_before_it_can_resolve_or_connect(
    no_network_outside_integration,
):
    curl_module = pytest.importorskip("curl_cffi.curl")
    handle = curl_module.Curl()
    try:
        with pytest.raises(net_guard.NetworkBlocked, match="libcurl"):
            handle.perform()
    finally:
        handle.close()
    assert len(no_network_outside_integration) == 1
    no_network_outside_integration.clear()  # provoked on purpose


def test_a_listener_the_test_started_itself_still_works(
    no_network_outside_integration,
):
    for _ in range(20):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        if listener.getsockname()[1] not in net_guard.LIVE_PORTS:
            break
        listener.close()
    listener.listen(1)
    client = socket.socket()
    try:
        client.connect(listener.getsockname())
        server, _ = listener.accept()
        client.sendall(b"ping")
        assert server.recv(4) == b"ping"
        server.close()
    finally:
        client.close()
        listener.close()
    assert no_network_outside_integration == []


def test_unix_sockets_and_socketpairs_are_untouched():
    left, right = socket.socketpair()
    left.sendall(b"x")
    assert right.recv(1) == b"x"
    left.close()
    right.close()
