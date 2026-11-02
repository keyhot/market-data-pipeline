"""S22: tests/net_guard.py refuses what the unit suite must not reach, on fakes.

Nothing here opens a connection to anything but a listener the test itself
bound: on an ephemeral loopback port, or on an abstract AF_UNIX name. The
refusals are exercised either through the autouse guard (which raises before
the real connect is reached) or over a tripwire standing in for the real
connect, so a regression fails the test instead of reaching a service."""

import asyncio
import os
import socket
from pathlib import Path

import net_guard
import pytest


@pytest.mark.parametrize(
    "host, port",
    [
        ("127.0.0.1", 9),
        ("127.5.5.5", 9),
        ("::1", 9),
        ("::ffff:127.0.0.1", 9),
        ("localhost", 80),
        ("0.0.0.0", 80),
        ("LOCALHOST", 80),
        ("localhost.", 80),
    ],
)
def test_loopback_is_allowed_on_any_port_not_in_the_live_list(host, port):
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
    socket.socket().connect(("127.0.0.1", 9))  # the tripwire, not a network
    assert reached == [("127.0.0.1", 9)]


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


def test_unix_sockets_and_socketpairs_are_untouched(no_network_outside_integration):
    left, right = socket.socketpair()
    left.sendall(b"x")
    assert right.recv(1) == b"x"
    left.close()
    right.close()
    assert no_network_outside_integration == []


# --- the live-service ports and the Postgres sockets --------------------------

_NEW_PORTS = (8000, 8080, 4455, 11434, 18789, 631, 53)
_LOOPBACKS = (
    "127.0.0.1",
    "127.0.0.4",
    "127.0.0.12",
    "::1",
    "::ffff:127.0.0.1",
    "localhost",
    "LOCALHOST",
    "0.0.0.0",
)


def test_the_live_service_list_is_pinned_and_clear_of_ephemeral_ports():
    assert dict.fromkeys(net_guard.LIVE_SERVICES) == dict.fromkeys(
        (5432, 55432, *_NEW_PORTS)
    )
    assert net_guard.LIVE_PORTS == frozenset(net_guard.LIVE_SERVICES)
    # The Linux default range starts at 32768: a bind(0) test server cannot land
    # below it. 55432 (the CI Postgres) is the one listed port inside it, a
    # bind(0) server lands on it about once in 28,000 runs, and the listener test
    # retries. (Not read from /proc: a host with a widened range would fail here.)
    low = 32768
    assert sorted(port for port in net_guard.LIVE_PORTS if port >= low) == [55432]


@pytest.mark.parametrize("port", _NEW_PORTS)
@pytest.mark.parametrize("host", _LOOPBACKS)
def test_loopback_connects_to_the_live_service_ports_are_refused(host, port):
    reason = net_guard.connect_reason(host, port)
    assert reason
    assert f"loopback port {port} is " in reason
    assert net_guard.LIVE_SERVICES[port] in reason


@pytest.mark.parametrize("port", [9, 1024, 8001, 33000, 40000])
def test_neighbouring_and_ephemeral_loopback_ports_stay_open(port):
    assert net_guard.connect_reason("127.0.0.1", port) is None


def test_a_connect_to_the_api_port_names_the_test_and_the_port_and_never_connects(
    monkeypatch,
):
    reached = []
    monkeypatch.setattr(
        socket.socket, "connect", lambda self, address: reached.append(address)
    )
    monkeypatch.setattr(
        socket.socket, "connect_ex", lambda self, address: reached.append(address)
    )
    attempts = net_guard.install(monkeypatch, "tests/unit/probe.py::test_probe")
    for target in (("127.0.0.4", 8000), ("::1", 8080, 0, 0), ("localhost", 4455)):
        family = socket.AF_INET6 if target[0] == "::1" else socket.AF_INET
        with socket.socket(family) as sock:
            with pytest.raises(net_guard.NetworkBlocked) as refusal:
                sock.connect(target)
            message = str(refusal.value)
            assert "tests/unit/probe.py::test_probe" in message
            assert f"{target[0]}:{target[1]}" in message
            assert f"loopback port {target[1]} is " in message
            with pytest.raises(net_guard.NetworkBlocked):
                sock.connect_ex(target)
    assert reached == []
    assert len(attempts) == 6


def test_a_datagram_to_the_resolver_stub_port_is_refused_and_never_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(socket.socket, "sendto", lambda self, *a: sent.append(a))
    monkeypatch.setattr(
        socket.socket, "sendmsg", lambda self, *a, **k: sent.append((a, k))
    )
    attempts = net_guard.install(monkeypatch, "tests/unit/probe.py::test_probe")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        with pytest.raises(net_guard.NetworkBlocked, match="port 53 is the systemd"):
            datagram.sendto(b"x", ("127.0.0.53", 53))
        with pytest.raises(net_guard.NetworkBlocked, match="sendmsg"):
            datagram.sendmsg([b"x"], [], 0, ("127.0.0.53", 53))
        with pytest.raises(net_guard.NetworkBlocked, match="sendmsg"):
            datagram.sendmsg([b"x"], address=("127.0.0.1", 11434))
        datagram.sendto(b"x", ("127.0.0.1", 9))  # the tripwire, not a network
        datagram.sendmsg([b"x"], [], 0, ("127.0.0.1", 9))
    assert sent == [(b"x", ("127.0.0.1", 9)), (([b"x"], [], 0, ("127.0.0.1", 9)), {})]
    assert len(attempts) == 3


def test_a_port_given_through_dunder_index_is_judged_as_the_int_it_is():
    class Port:  # the C layer takes any __index__ as a port
        def __init__(self, number):
            self.number = number

        def __index__(self):
            return self.number

    assert net_guard.connect_reason("127.0.0.1", Port(5432))
    assert net_guard.connect_reason("127.0.0.1", Port(8000))
    assert net_guard.connect_reason("127.0.0.1", Port(9)) is None


_PG_SOCKETS = [
    "/var/run/postgresql/.s.PGSQL.5432",
    "/tmp/.s.PGSQL.55432",
    b"/run/postgresql/.s.PGSQL.5432",
    Path("/var/run/postgresql/.s.PGSQL.5432"),
    ".s.PGSQL.5433",
    "/tmp/some/other/dir/.s.PGSQL.1",
    b"\0/var/run/postgresql/.s.PGSQL.5432",
    b"\0.s.PGSQL.5432",  # an abstract name with no directory
    b"\0/run/postgresql/.s.PGSQL.5432\0",
    bytearray(b"/var/run/postgresql/.s.PGSQL.5432"),
    memoryview(b"/tmp/.s.PGSQL.55432"),
    b"/var/run/postgresql/.s.PGSQL.5432\0/x",  # the kernel stops at the first NUL
    "/tmp/.s.PGSQL.5432\0junk",
]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
def test_a_unix_connect_to_a_postgres_socket_is_refused_and_never_connects(
    monkeypatch, tmp_path
):
    reached = []
    monkeypatch.setattr(
        socket.socket, "connect", lambda self, address: reached.append(address)
    )
    monkeypatch.setattr(
        socket.socket, "connect_ex", lambda self, address: reached.append(address)
    )
    attempts = net_guard.install(monkeypatch, "tests/unit/probe.py::test_probe")
    # A renamed path that points at a Postgres socket is followed.
    (tmp_path / ".s.PGSQL.5432").touch()
    link = tmp_path / "innocent.sock"
    link.symlink_to(tmp_path / ".s.PGSQL.5432")
    targets = [*_PG_SOCKETS, str(link)]
    for target in targets:
        with socket.socket(socket.AF_UNIX) as sock:
            with pytest.raises(net_guard.NetworkBlocked) as refusal:
                sock.connect(target)
            assert "tests/unit/probe.py::test_probe" in str(refusal.value)
            assert "Postgres socket" in str(refusal.value)
            with pytest.raises(net_guard.NetworkBlocked):
                sock.connect_ex(target)
    assert reached == []
    assert len(attempts) == 2 * len(targets)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
def test_other_unix_paths_still_reach_the_connect(monkeypatch, tmp_path):
    reached = []
    monkeypatch.setattr(
        socket.socket, "connect", lambda self, address: reached.append(address)
    )
    attempts = net_guard.install(monkeypatch, "tests/unit/probe.py::test_probe")
    targets = [
        "/run/user/1000/bus",
        b"/tmp/mdp.sock",
        Path("/tmp/PGSQL.5432"),  # no leading `.s.`
        "/tmp/x.s.PGSQL.5432",  # the name must start with the prefix
        b"\0mdp-abstract",
        str(tmp_path / "plain.sock"),
        bytearray(b"/tmp/mdp.sock"),
        b"/tmp/plain.sock\0/.s.PGSQL.5432",  # the kernel connects to /tmp/plain.sock
    ]
    for target in targets:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(target)  # the tripwire, not a network
    assert reached == targets
    assert attempts == []


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
def test_a_real_unix_connect_to_a_non_postgres_socket_still_works(
    no_network_outside_integration,
):
    # An abstract name: no file, and none of tmp_path's 108-byte path limit.
    name = f"\0mdp-net-guard-{os.getpid()}-unix"
    listener = socket.socket(socket.AF_UNIX)
    client = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(name)
        listener.listen(1)
        client.connect(name)
        server, _ = listener.accept()
        client.sendall(b"ping")
        assert server.recv(4) == b"ping"
        server.close()
    finally:
        client.close()
        listener.close()
    assert no_network_outside_integration == []


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
def test_asyncio_still_runs_and_serves_over_a_unix_socket(
    no_network_outside_integration,
):
    name = f"\0mdp-net-guard-{os.getpid()}-asyncio"

    async def echo(reader, writer):
        writer.write(await reader.readexactly(4))
        await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_unix_server(echo, path=name)
        reader, writer = await asyncio.open_unix_connection(name)
        writer.write(b"ping")
        await writer.drain()
        data = await reader.readexactly(4)
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        return data

    loop = asyncio.new_event_loop()  # its self-pipe is a socketpair
    try:
        assert loop.run_until_complete(main()) == b"ping"
    finally:
        loop.close()
    assert no_network_outside_integration == []
