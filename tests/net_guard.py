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
  * a connect to loopback port 5432 or 55432 is refused (the live and the
    throwaway Postgres): a test may talk to a server it started itself on an
    ephemeral loopback port, not to a service that is already there;
  * a DNS lookup of a name that is not loopback is refused, so a service name
    like `freqtrade` fails the test on a box where it does not resolve too;
  * libcurl (curl_cffi, which yfinance 1.x uses) does its own DNS and sockets
    in C, past Python's socket module, so its perform entry points are refused;
  * a datagram sent to such an address is refused the same way;
  * AF_UNIX sockets and socketpair are untouched.

A refusal raises NetworkBlocked, which names the test. Code that catches
Exception (record_trader_events does) would swallow it and the test would pass,
so every refusal is also recorded and the autouse fixture fails the test at
teardown when any were. A test that provokes a refusal on purpose clears the
list it is handed.

Not covered (none of these occurs in the suite today): a module- or session-
scoped fixture, an import at collection time or a subprocess that connects; a
thread that outlives its test; a loopback service on another port (Redis,
freqtrade published on localhost) or a Postgres reached over its AF_UNIX
socket; reverse DNS (gethostbyaddr, getnameinfo, getfqdn); native stacks other
than libcurl and libpq (uvloop, c-ares, grpc).
"""

import ipaddress
import socket
from pathlib import Path

LIVE_PORTS = frozenset({5432, 55432})
_INET = (socket.AF_INET, socket.AF_INET6)


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
    if port in LIVE_PORTS:
        return f"loopback port {port} is a Postgres, not a server the test started"
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
            if self.family in _INET and isinstance(address, tuple) and address:
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

    real_sendto = socket.socket.sendto

    def sendto(self, data, *args):
        # A datagram to an IP literal never calls connect.
        address = args[-1] if args else None
        if self.family in _INET and isinstance(address, tuple) and address:
            host, port = address[0], address[1] if len(address) > 1 else None
            reason = connect_reason(host, port)
            if reason:
                refuse(f"sendto {host}:{port}", reason)
        return real_sendto(self, data, *args)

    monkeypatch.setattr(socket.socket, "sendto", sendto)

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
