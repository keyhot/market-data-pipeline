"""The HTTP surface of `api.main:app`, pinned (#44).

`api/main.py` was one 1,100-line module with 23 routes and no `APIRouter`; the
split into `api/routers/*` is a pure move, and this file is how "pure" is
checked: it snapshots what a client (and the metrics middleware, which keys on
`route.path`) can observe, taken from the code BEFORE the split, and it must
pass unchanged after it.

What is pinned, and why each piece:

- every route's path, methods, name, endpoint name, sync-vs-async (a `def`
  runs in the threadpool, an `async def` on the loop, so moving between them
  is a behaviour change), status code, response model / response class,
  dependencies, tags and schema visibility;
- the whole OpenAPI document (paths, parameters, response schemas, operation
  ids -- those come from endpoint names, so a renamed function shows up here);
- user middleware, in order, and the exception handlers;
- which route a concrete request dispatches to. The inventory is compared as a
  SORTED list because the split necessarily regroups routes, so list order is
  not pinned; instead this table proves no URL changed hands (no two of these
  paths overlap today, so registration order cannot decide a match).

Regenerate ONLY for an intended change to the API surface:

    PYTHONPATH=. poetry run python tests/api/test_route_inventory.py --write
"""

import asyncio
import json
import sys
from pathlib import Path

from fastapi.datastructures import DefaultPlaceholder
from fastapi.routing import APIRoute
from starlette.routing import Match, Mount, Route

from api.main import app

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
INVENTORY_FILE = SNAPSHOT_DIR / "route_inventory.json"
OPENAPI_FILE = SNAPSHOT_DIR / "openapi.json"


def _unwrap(value):
    return value.value if isinstance(value, DefaultPlaceholder) else value


def _describe(obj) -> str | None:
    if obj is None:
        return None
    return getattr(obj, "__name__", None) or repr(obj)


def _route_entry(route) -> dict:
    entry = {
        "type": type(route).__name__,
        "path": route.path,
        "name": route.name,
        "methods": sorted(getattr(route, "methods", None) or []),
    }
    if isinstance(route, APIRoute):
        entry.update(
            {
                "endpoint": route.endpoint.__name__,
                "is_async": asyncio.iscoroutinefunction(route.endpoint),
                "status_code": route.status_code,
                "response_model": _describe(route.response_model),
                "response_class": _describe(_unwrap(route.response_class)),
                "tags": list(route.tags),
                "include_in_schema": route.include_in_schema,
                "deprecated": route.deprecated,
                "operation_id": route.operation_id,
                "unique_id": route.unique_id,
                "route_dependencies": [
                    _describe(d.dependency) for d in route.dependencies
                ],
                "resolved_dependencies": [
                    _describe(d.call) for d in route.dependant.dependencies
                ],
                "path_params": sorted(p.name for p in route.dependant.path_params),
                "query_params": sorted(p.name for p in route.dependant.query_params),
                "header_params": sorted(p.name for p in route.dependant.header_params),
                "cookie_params": sorted(p.name for p in route.dependant.cookie_params),
                "body_params": sorted(p.name for p in route.dependant.body_params),
                "request_param_name": route.dependant.request_param_name,
            }
        )
    elif isinstance(route, Mount):
        entry["endpoint"] = type(route.app).__name__
        entry["directory"] = Path(route.app.directory).name
    elif isinstance(route, Route):
        entry["endpoint"] = route.endpoint.__name__
    return entry


def _middleware_entry(middleware) -> dict:
    kwargs = getattr(middleware, "kwargs", None)
    if kwargs is None:
        kwargs = middleware.options
    args = getattr(middleware, "args", ())
    return {
        "cls": middleware.cls.__name__,
        "args": [_describe(a) for a in args],
        "kwargs": {k: _describe(v) for k, v in sorted(kwargs.items())},
    }


def _unwrap_lifespan(ctx):
    """`(root, nested)`: the function the app was built with, and the type name
    of every context FastAPI wrapped around it.

    `app.include_router(r)` (any router, even one with no hooks) replaces the
    app's lifespan with `merged_lifespan(original, nested=r.lifespan_context)`,
    so after the split `app.router.lifespan_context` is a stack of those
    wrappers over `api.main.lifespan`. Unwrapping them is what lets the
    inventory keep pinning "the lifespan is `lifespan`" instead of the wrapper.
    """
    nested = []
    while getattr(ctx, "__name__", "") == "merged_lifespan":
        inner = ctx.__wrapped__
        free = dict(
            zip(
                inner.__code__.co_freevars, (c.cell_contents for c in inner.__closure__)
            )
        )
        nested.append(type(free["nested_context"]).__name__)
        ctx = free["original_context"]
    return ctx, nested


def build_inventory() -> dict:
    routes = sorted(
        (_route_entry(r) for r in app.routes),
        key=lambda e: (e["path"], e["methods"], e["name"]),
    )
    return {
        "title": app.title,
        "routes": routes,
        "middleware": [_middleware_entry(m) for m in app.user_middleware],
        "exception_handlers": {
            _describe(exc): _describe(handler)
            for exc, handler in sorted(
                app.exception_handlers.items(), key=lambda kv: _describe(kv[0])
            )
        },
        "lifespan": _describe(_unwrap_lifespan(app.router.lifespan_context)[0]),
    }


# (method, concrete path) -> the route name that must serve it. One row per
# route, plus the neighbours most likely to steal a URL from each other.
DISPATCH = [
    ("POST", "/world/heartbeat", "world_heartbeat"),
    ("GET", "/health", "health"),
    ("GET", "/metrics", "metrics"),
    ("GET", "/ticker/AAPL/1mo", "ticker"),
    ("GET", "/tickers/1mo", "tickers"),
    ("GET", "/news/AAPL", "news"),
    ("GET", "/bars/BTCUSDT", "bars"),
    ("GET", "/stored/events/AAPL/dividends", "stored_events"),
    ("GET", "/stored/news/AAPL", "stored_news"),
    ("GET", "/stream/bars/BTCUSDT", "stream_bars"),
    ("GET", "/world/events", "world_events"),
    ("GET", "/world/state", "world_state"),
    ("GET", "/signals/BTCUSDT", "signals"),
    ("GET", "/stream/world/events", "stream_world_events"),
    ("GET", "/overlay/signals", "overlay_signals"),
    ("GET", "/music/now-playing", "music_now_playing"),
    ("GET", "/overlay/events", "overlay_events"),
    ("GET", "/standby", "standby_page"),
    ("GET", "/world", "world_page"),
    ("GET", "/chart/BTCUSDT", "chart"),
    ("GET", "/charts", "charts"),
    ("GET", "/dashboard", "dashboard"),
    ("GET", "/events/AAPL/dividends", "event"),
    ("GET", "/static/world-plate-btc-eth.json", "static"),
    ("GET", "/openapi.json", "openapi"),
    ("GET", "/docs", "swagger_ui_html"),
    # Near misses that must NOT resolve to a route of ours.
    ("GET", "/world/heartbeat", None),  # POST only
    ("POST", "/world", None),
    ("GET", "/stream/bars", None),
    ("GET", "/stored/events/AAPL", None),
    ("GET", "/events/AAPL", None),
    ("GET", "/nope", None),
]


def _dispatch(method: str, path: str) -> str | None:
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    partial = None
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.name
        if match == Match.PARTIAL and partial is None:
            partial = route
    # A PARTIAL match is a path hit with the wrong method: Starlette answers
    # 405, not a handler, so for this table it is "no route serves it".
    return None


def test_route_inventory_is_unchanged():
    expected = json.loads(INVENTORY_FILE.read_text())
    assert build_inventory() == expected


def test_there_are_twenty_three_api_routes():
    api_routes = [
        r
        for r in app.routes
        if isinstance(r, APIRoute) and r.path not in {"/openapi.json", "/docs"}
    ]
    assert len(api_routes) == 23


def test_openapi_document_is_unchanged():
    expected = json.loads(OPENAPI_FILE.read_text())
    # Round-trip through JSON so tuples/ordering artefacts cannot differ.
    assert json.loads(json.dumps(app.openapi())) == expected


def test_openapi_path_set_is_unchanged():
    expected = json.loads(OPENAPI_FILE.read_text())
    got = app.openapi()
    assert sorted(got["paths"]) == sorted(expected["paths"])
    assert {
        (path, method) for path, ops in got["paths"].items() for method in ops
    } == {(path, method) for path, ops in expected["paths"].items() for method in ops}


def test_every_url_still_dispatches_to_the_same_route():
    got = [(m, p, _dispatch(m, p)) for m, p, _ in DISPATCH]
    assert got == DISPATCH


def test_routers_add_no_startup_or_shutdown_behaviour():
    # The only thing a router contributes to the app's lifespan is FastAPI's
    # own do-nothing default; nobody registered an on_startup/on_shutdown hook.
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []
    _, nested = _unwrap_lifespan(app.router.lifespan_context)
    assert set(nested) <= {"_DefaultLifespan"}


def test_lifespan_starts_and_stops_the_scheduler_once_in_order(monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as main

    calls = []
    monkeypatch.setattr(main, "scheduler_enabled", lambda: True)
    monkeypatch.setattr(main, "ws_ingest_enabled", lambda: False)
    monkeypatch.setattr(
        main.scheduler_service, "start", lambda: calls.append("start")
    )
    monkeypatch.setattr(
        main.scheduler_service, "shutdown", lambda: calls.append("shutdown")
    )
    with TestClient(main.app) as client:
        assert calls == ["start"]
        assert client.get("/standby").status_code == 200
    assert calls == ["start", "shutdown"]


def test_route_names_are_unique():
    # Metrics key on `route.path`, OpenAPI operation ids on the endpoint name;
    # a duplicate name after a split (two routers each defining `health`)
    # would silently rename an operation id.
    names = [r.name for r in app.routes if isinstance(r, APIRoute)]
    assert len(names) == len(set(names))


if __name__ == "__main__":
    if "--write" not in sys.argv:
        raise SystemExit("pass --write to overwrite the snapshots")
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    INVENTORY_FILE.write_text(json.dumps(build_inventory(), indent=2) + "\n")
    OPENAPI_FILE.write_text(
        json.dumps(json.loads(json.dumps(app.openapi())), indent=2, sort_keys=True)
        + "\n"
    )
    print(f"wrote {INVENTORY_FILE} and {OPENAPI_FILE}")
