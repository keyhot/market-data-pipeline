"""Run /world's real ES modules under node (#42 follow-up: node-import tests).

The page is 16 native ES modules under `api/static/world/`. A test that wants
the behaviour of one of them imports it here instead of slicing its text out of
the page: `run_world(body)` copies the module files, byte for byte, next to a
`{"type": "module"}` package.json, stubs the browser globals, and runs `body`
as an ES module in which

    W("shading.js")            the real module namespace (await it)
    need(ns, "a", "b")         ns, or a thrown error naming a missing export
    setPixi({Graphics: ...})   what the modules' bare `PIXI` global resolves to
                               (every key not given is a catch-all stub)
    CONFIG                     the `world-config` block /world serves, with the
                               caller's `config=` overrides applied
    boot()                     load main.js for real, before any W() (see below)

Whatever `body` prints last (one JSON line) comes back parsed.

`boot=True` evaluates `main.js`, so `boot()` and `drawPlate()` really run
against `PIXI.Application` / `PIXI.Assets` fakes. That is how a test reaches
state the modules only assign internally (`app` in boot.js, `plateReady` in
room.js) without a setter: it is set the way the page sets it. With a plate in
the config the plate "loads" and `plateReady` becomes true; `plate_loads=False`
makes the texture fail to load, which leaves `plateReady` false on the
procedural room, exactly as the page does.

Modules are imported via `await import(...)` AFTER the stubs: a static import
would evaluate config.js before `document` exists.
"""

from __future__ import annotations

import atexit
import functools
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from api.templating import WORLD_MODULE_DIR, world_modules

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRIVER_MARK = "// world-module-driver\n"


@functools.lru_cache(maxsize=1)
def _sandbox() -> Path:
    """The modules, copied verbatim beside a package.json that makes node read
    `.js` as ESM on any node version (the same arrangement as
    tests/api/test_world_modules.py)."""
    path = Path(tempfile.mkdtemp(prefix="world-modules-"))
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    (path / "package.json").write_text('{"type": "module"}')
    for name in world_modules():
        (path / name).write_bytes((WORLD_MODULE_DIR / name).read_bytes())
    return path


@functools.lru_cache(maxsize=1)
def _served_config_text() -> str:
    from fastapi.testclient import TestClient

    from api import main

    page = TestClient(main.app).get("/world").text
    block = re.search(
        r'<script type="application/json" id="world-config">(.*?)</script>',
        page,
        re.S,
    )
    assert block, "/world no longer carries the world-config block"
    return block.group(1)


def served_config() -> dict:
    """The config block /world serves, parsed fresh (safe to mutate)."""
    return json.loads(_served_config_text())


_PRELUDE = r"""
const SANDBOX = __SANDBOX__;
const CONFIG = __CONFIG__;
const PLATE_LOADS = __PLATE_LOADS__;
const fake = new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => 0
    : k === "then" ? undefined
    : k === Symbol.iterator ? function* () {}
    : fake,
  set: () => true, apply: () => fake, construct: () => fake,
});
const element = (id) => id === "world-config"
  ? { textContent: JSON.stringify(CONFIG) } : fake;
globalThis.document = new Proxy({}, {
  get: (t, k) => k === "getElementById" ? element : fake,
});
globalThis.window = fake;
globalThis.history = fake;
globalThis.location = { search: "", href: "http://test/world", reload() {} };
globalThis.fetch = () => new Promise(() => {});
globalThis.EventSource = function () { return fake; };
globalThis.setTimeout = globalThis.setInterval = () => 0;
globalThis.requestAnimationFrame = () => 0;
globalThis.CustomEvent = function () { return fake; };
process.on("unhandledRejection", (e) => {
  console.error("UNHANDLED REJECTION", e && e.stack || e);
  process.exit(3);
});
const setPixi = (parts) => {
  globalThis.PIXI = new Proxy(parts, { get: (t, k) => k in t ? t[k] : fake });
  return globalThis.PIXI;
};
setPixi({});
const need = (ns, ...names) => {
  for (const n of names) {
    if (!(n in ns)) throw new Error("module has no export " + n);
  }
  return ns;
};
const settle = async () => {
  for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r));
};
// The page enters its module graph at main.js, and only there is the
// evaluation order valid (room.js and ambient.js import each other; entering
// at room.js reads ROOM_LIGHT before it exists). So the first W()/boot() loads
// main.js, which runs boot(); the caller's own PIXI is put back afterwards.
let mainLoaded = false;
const loadMain = async (parts) => {
  if (mainLoaded) throw new Error("main.js is already loaded: boot() comes first");
  mainLoaded = true;
  const saved = globalThis.PIXI;
  setPixi(parts);
  await import(SANDBOX + "main.js");
  await settle();
  globalThis.PIXI = saved;
};
const W = async (name) => {
  if (!mainLoaded) await loadMain({});
  return import(SANDBOX + name);
};
class FakeApplication {
  constructor() {
    this.screen = { width: 1920, height: 960 };
    this.stage = fake; this.ticker = fake; this.canvas = fake;
    this.renderer = fake;
  }
  async init() {}
}
const boot = () => loadMain({
  Application: FakeApplication,
  Assets: { load: async () => {
    if (!PLATE_LOADS) throw new Error("texture failed to load");
    return fake;
  } },
});
"""


def world_driver(
    body: str,
    *,
    config: dict | None = None,
    plate_loads: bool = True,
) -> str:
    """The complete ES-module source for one node run of `body`."""
    cfg = served_config()
    cfg.update(config or {})
    return (
        DRIVER_MARK
        + _PRELUDE.replace("__SANDBOX__", json.dumps(_sandbox().as_uri() + "/"))
        .replace("__CONFIG__", json.dumps(cfg))
        .replace("__PLATE_LOADS__", "true" if plate_loads else "false")
        + body
    )


def run_driver(driver: str, *, timeout: int = 60):
    """Run a `world_driver` script; the last stdout line, parsed as JSON."""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", driver],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def run_world(body: str, *, config=None, plate_loads=True, timeout: int = 60):
    return run_driver(
        world_driver(body, config=config, plate_loads=plate_loads), timeout=timeout
    )


@functools.lru_cache(maxsize=None)
def function_source(module: str, name: str) -> str:
    """One exported function's own source text, as the engine reports it
    (`Function.prototype.toString`) after importing the real module. Lets a
    test read what a function does without brace-matching the page text."""
    return run_world(
        f'const ns = need(await W("{module}"), "{name}");'
        f"console.log(JSON.stringify(ns.{name}.toString()));"
    )
