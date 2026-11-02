"""S22: the unit and API suites reach no network service.

Before this, nothing stopped a unit test from opening a real connection. The
scheduler service tests ran the real `trader:mirror` job, which asks the default
FREQTRADE_URL host `freqtrade`; that only failed because the name does not
resolve on a developer box. And with DATABASE_URL unset, storage.db falls back
to the live Postgres on localhost:5432 (KI-083, KI-106). This module is the one
place the rules live; tests/conftest.py applies them per test and
tests/unit/test_net_guard.py pins them on fakes.

The rules, for every test outside tests/integration:
  * a connect to any non-loopback address is refused;
  * a connect to a loopback port where a service of this host already
    listens is refused (LIVE_SERVICES: both Postgres ports, the API on 8000,
    freqtrade on 8080, the OBS websocket on 4455, and the host's own ollama,
    openclaw gateway, CUPS and resolver stub): a test may talk to a server it
    started itself on an ephemeral loopback port, not to a service that is
    already there. Loopback is 127.0.0.0/8 (the API answers on 127.0.0.1-12),
    ::1, ::ffff:127.x, 0.0.0.0 and the name localhost;
  * an AF_UNIX connect to a Postgres socket (a name starting `.s.PGSQL.`, also
    through a symlink or an abstract name) is refused; other AF_UNIX paths pass;
  * a DNS lookup of a name that is not loopback is refused, so a service name
    like `freqtrade` fails the test on a box where it does not resolve too;
  * libcurl (curl_cffi, which yfinance 1.x uses) does its own DNS and sockets
    in C, past Python's socket module, so its perform entry points are refused;
  * a datagram sent (sendto, sendmsg) to such an address is refused the same way;
  * other AF_UNIX sockets, asyncio's own socketpair and loopback servers on
    ephemeral ports are untouched.

A refusal raises NetworkBlocked, which names the test. Code that catches
Exception (record_trader_events does) would swallow it and the test would pass,
so every refusal is also recorded and the autouse fixture fails the test at
teardown when any were. A test that provokes a refusal on purpose clears the
list it is handed.

The list is static on purpose. Narrowing loopback to "ports this process
listens on" would break a test server started in a subprocess; reading the
host's listeners at run time would deny the test's own server, which sits on
an ephemeral port. So the list holds only fixed ports. All but one sit below
the Linux ephemeral range (32768-60999), so a bind(0) server cannot land on
them; 55432, the CI Postgres, is inside it, and a bind(0) server lands there
about once in 28,000 runs (the listener test retries). Listeners that sit in
that range (editor and agent tooling) cannot be listed and stay reachable.

Not covered (none of these occurs in the suite today): a module- or session-
scoped fixture, an import at collection time or a subprocess that connects; a
thread that outlives its test; a loopback service on a port not in
LIVE_SERVICES (Redis would be 6379; none is deployed), a service whose port a
.env overrides, or one on an ephemeral port; a Postgres reached over its
AF_UNIX socket by libpq, which connects in C past this hook (conftest refuses
psycopg.connect and ConnectionPool for that), or through a hard link to a
Postgres socket (a symlink is followed, a hard link is not); AF_UNIX datagrams;
a fake Postgres the test serves itself on a `.s.PGSQL.*` socket (refused too:
name it otherwise); `_socket.socket` methods called directly; reverse DNS
(gethostbyaddr, getnameinfo, getfqdn); native stacks other than libcurl and
libpq (uvloop, c-ares, grpc).
"""

import ipaddress
import operator
import os
import socket
from pathlib import Path

# Loopback ports where a service of this host listens, and what it is. The
# first five are this repo's own (docker-compose.yml, .env.example,
# config/freqtrade/config.json, scripts/stream_ctl.py, .github/workflows/ci.yml);
# the rest are other services `ss -ltn` showed on the development host.
LIVE_SERVICES = {
    5432: "the live Postgres",
    55432: "the throwaway test Postgres",
    8000: "the market-data API (and its 127.0.0.1-12 browser-source shards)",
    8080: "the freqtrade sidecar",
    4455: "the OBS websocket",
    11434: "ollama",
    18789: "the openclaw gateway",
    631: "CUPS",
    53: "the systemd-resolved stub",
}
LIVE_PORTS = frozenset(LIVE_SERVICES)
PG_SOCKET_PREFIX = ".s.PGSQL."
_INET = (socket.AF_INET, socket.AF_INET6)
_UNIX = getattr(socket, "AF_UNIX", None)


class NetworkBlocked(RuntimeError):
    """A test tried to reach a network service."""


def applies_to(path, root=None) -> bool:
    """tests/integration is the suite that talks to Postgres on :55432. Judged
    relative to the rootdir, so a checkout under a directory that happens to be
    called `integration` does not switch the guard off."""
    path = Path(path)
    if root is not None:
        try:
            path = path.relative_to(root)
        except ValueError:
            pass
    return "integration" not in path.parts


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = host.lower().rstrip(".")
    if host in ("", "localhost") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    return ip.is_loopback or ip.is_unspecified


def connect_reason(host, port) -> str | None:
    """Why a connect to host:port is refused, or None when it may proceed."""
    if not isinstance(host, (str, bytes)):
        return None
    if not _is_local(host):
        return f"{host!r} is not a loopback address"
    try:
        port = int(operator.index(port))  # the C layer accepts any __index__
    except TypeError:
        pass
    if port in LIVE_SERVICES:
        return (
            f"loopback port {port} is {LIVE_SERVICES[port]}, "
            "not a server the test started"
        )
    return None


def unix_reason(address) -> str | None:
    """Why an AF_UNIX connect to address is refused: a Postgres socket, by its
    name, `.s.PGSQL.<port>`, whatever the directory. Judged the way the kernel
    reads the address: a file path ends at its first NUL, a symlink is followed,
    and an abstract name (leading NUL; libpq's `@dir`) is judged by its last
    component. bytes, bytearray and memoryview are all accepted by the C layer.
    Nothing is opened or connected to."""
    try:
        if isinstance(address, (bytearray, memoryview)):
            address = bytes(address)
        path = os.fsdecode(os.fspath(address))
    except (TypeError, ValueError):
        return None
    if path.startswith("\0"):
        names = [os.path.basename(path.strip("\0"))]
    else:
        path = path.split("\0", 1)[0]
        names = [os.path.basename(path)]
        try:
            names.append(os.path.basename(os.path.realpath(path)))
        except OSError:
            pass
    if any(name.startswith(PG_SOCKET_PREFIX) for name in names):
        return f"{path!r} is a Postgres socket, not a server the test started"
    return None


def lookup_reason(host) -> str | None:
    """Why resolving host is refused. Numeric addresses need no lookup; a
    non-loopback one is refused when it is connected to."""
    if host is None or not isinstance(host, (str, bytes)):
        return None
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if _is_local(host):
        return None
    try:
        ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return f"resolving {host!r} would query DNS"
    return None


def install(monkeypatch, nodeid: str) -> list[str]:
    """Patch the socket and libcurl entry points. Returns the list that every
    refusal is appended to."""
    attempts: list[str] = []

    def refuse(what: str, reason: str):
        attempts.append(what)
        raise NetworkBlocked(
            f"{nodeid}: the unit suite reaches no network ({what}: {reason}). "
            "Stub the client at its seam, as tests/unit/test_scheduler_service.py "
            "does for the scheduler jobs; a server the test starts itself on an "
            "ephemeral loopback port is allowed. See tests/net_guard.py."
        )

    def guarded_connect(real, kind):
        def connect(self, address):
            if _UNIX is not None and self.family == _UNIX:
                reason = unix_reason(address)
                if reason:
                    refuse(f"{kind} {address!r}", reason)
            elif self.family in _INET and isinstance(address, tuple) and address:
                host = address[0]
                port = address[1] if len(address) > 1 else None
                reason = connect_reason(host, port)
                if reason:
                    refuse(f"{kind} {host}:{port}", reason)
            return real(self, address)

        return connect

    def guarded_lookup(real, name):
        def lookup(host, *args, **kwargs):
            reason = lookup_reason(host)
            if reason:
                refuse(f"{name} {host}", reason)
            return real(host, *args, **kwargs)

        return lookup

    monkeypatch.setattr(
        socket.socket, "connect", guarded_connect(socket.socket.connect, "connect")
    )
    monkeypatch.setattr(
        socket.socket,
        "connect_ex",
        guarded_connect(socket.socket.connect_ex, "connect_ex"),
    )
    monkeypatch.setattr(
        socket, "getaddrinfo", guarded_lookup(socket.getaddrinfo, "getaddrinfo")
    )
    monkeypatch.setattr(
        socket, "gethostbyname", guarded_lookup(socket.gethostbyname, "gethostbyname")
    )
    monkeypatch.setattr(
        socket,
        "gethostbyname_ex",
        guarded_lookup(socket.gethostbyname_ex, "gethostbyname_ex"),
    )

    def check_datagram(sock, address, kind):
        # A datagram to an IP literal never calls connect.
        if sock.family in _INET and isinstance(address, tuple) and address:
            host, port = address[0], address[1] if len(address) > 1 else None
            reason = connect_reason(host, port)
            if reason:
                refuse(f"{kind} {host}:{port}", reason)

    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg

    def sendto(self, data, *args):
        check_datagram(self, args[-1] if args else None, "sendto")
        return real_sendto(self, data, *args)

    def sendmsg(self, buffers, *args, **kwargs):
        # sendmsg(buffers[, ancdata[, flags[, address]]])
        address = args[2] if len(args) > 2 else kwargs.get("address")
        check_datagram(self, address, "sendmsg")
        return real_sendmsg(self, buffers, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)

    def guarded_curl(what):
        def perform(*args, **kwargs):
            refuse(what, "libcurl does its own DNS and sockets, past this guard")

        return perform

    try:
        import curl_cffi.aio
        import curl_cffi.curl
    except ImportError:  # yfinance's dependency; absent means nothing to guard
        return attempts
    monkeypatch.setattr(
        curl_cffi.curl.Curl, "perform", guarded_curl("curl_cffi Curl.perform")
    )
    monkeypatch.setattr(
        curl_cffi.aio.AsyncCurl,
        "add_handle",
        guarded_curl("curl_cffi AsyncCurl.add_handle"),
    )
    return attempts
