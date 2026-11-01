"""#42: `/world`'s script is native ES modules under `api/static/world/`.

The page carries one module tag; the browser follows the imports. These pin
what that needs and what the split promised: the modules are served same-origin
with a JavaScript MIME type (a module served as text/plain is refused outright:
a white frame), no code stayed inline beside the JSON config block, every
import resolves to an export, and the modules evaluate in exactly the order the
single inline script ran its statements, so no binding is read before it is
initialised. The content checks in test_world_page.py read the same modules
through `world_page_source()` / `_world_source()`.
"""

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from api import main
from api.templating import WORLD_MODULE_DIR, world_modules

client = TestClient(main.app)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S)
_IMPORT = re.compile(r'^import\s+(?:\{([^}]*)\}\s*from\s+)?"([^"]+)";', re.M)
_EXPORT = re.compile(r"^export \{([^}]*)\};", re.M)


def _source(name: str) -> str:
    return (WORLD_MODULE_DIR / name).read_text()


def _names(group: str) -> set[str]:
    return {n.strip() for n in group.split(",") if n.strip()}


def test_the_page_loads_pixi_then_one_same_origin_module():
    """Pixi stays a classic, parser-blocking tag (KI-045: vendored, never a
    CDN), so `PIXI` exists before any module runs; module scripts are deferred,
    so the entry runs after the config block and the whole body are parsed —
    where the inline script used to sit."""
    page = client.get("/world").text
    with_src = [attrs for attrs, _ in _SCRIPT.findall(page) if "src=" in attrs]
    assert with_src == [
        ' src="/static/pixi-8.19.0.min.js"',
        ' type="module" src="/static/world/main.js"',
    ]


def test_no_script_stays_inline_but_the_config_block_and_the_boot_fallback():
    """The JSON block stays inline because the server renders it per request
    (#41) and the page must not fetch its own config; it is data, not code.
    The one inline code is the boot fallback, which must run when no module
    does (tested below); none of the room's code may drift back beside it."""
    page = client.get("/world").text
    inline = [(attrs, body) for attrs, body in _SCRIPT.findall(page) if body.strip()]
    assert [attrs for attrs, _ in inline] == [
        ' type="application/json" id="world-config"',
        ' id="world-boot-fallback"',
    ]
    json.loads(inline[0][1])
    assert len(inline[1][1].strip().splitlines()) < 30, "the boot fallback grew"


_FALLBACK_DRIVER = r"""
const [code, search, ran] = process.argv.slice(2);
const out = { delay: null, replaced: null };
const fallback = { textContent: "Waking the world…" };
globalThis.window = ran === "1" ? { __worldModules: true } : {};
globalThis.location = {
  search, href: "http://test/world" + search, replace: (u) => { out.replaced = u; },
};
globalThis.document = { getElementById: (id) => (id === "fallback" ? fallback : null) };
globalThis.setTimeout = (fn, ms) => { out.delay = ms; fn(); };
new Function(code)();
out.text = fallback.textContent;
console.log(JSON.stringify(out));
"""


@needs_node
def test_the_boot_fallback_retries_a_module_graph_that_never_ran(tmp_path):
    """retryBoot moved into the modules (#42), so a module that never loads (the
    API restarting between the page and its scripts: the very case retryBoot
    exists for) latched "Waking the world…". The page's inline fallback retries
    it on retryBoot's own `boot_retry` counter, backoff and cap, and stands
    down once main.js has run, so a boot() failure is never reloaded twice.
    `scripts/check_world_boot.py modblock` drives it in Chrome."""
    page = client.get("/world").text
    code = next(b for a, b in _SCRIPT.findall(page) if 'id="world-boot-fallback"' in a)
    backoff = json.loads(
        re.search(r"const RETRY_BACKOFF_MS = (\[[^\]]*\]);", _source("boot.js")).group(
            1
        )
    )
    assert (
        json.loads(re.search(r"var BACKOFF_MS = (\[[^\]]*\]);", code).group(1))
        == backoff
    )
    driver = tmp_path / "fallback.cjs"
    driver.write_text(_FALLBACK_DRIVER)

    def run(search, ran=False):
        result = subprocess.run(
            [NODE, str(driver), code, search, "1" if ran else "0"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert run("", ran=True)["replaced"] is None
    assert run("?boot_retry=2", ran=True)["replaced"] is None
    for attempt, wait in enumerate(backoff):
        out = run(f"?boot_retry={attempt}" if attempt else "")
        assert out["replaced"] == f"http://test/world?boot_retry={attempt + 1}"
        assert out["delay"] > wait
    for search in (f"?boot_retry={len(backoff)}", "?boot_retry=abc", "?boot_retry=-1"):
        out = run(search)
        assert out["replaced"] is None, search
        assert "still running" in out["text"], search

    main_js = _source("main.js")
    assert main_js.index("window.__worldModules = true;") < main_js.index(
        "boot().catch("
    )


def test_only_the_world_modules_are_served_no_cache():
    """The modules must revalidate on every load (the page beside them is
    rendered fresh, so a cached module is a stale half of a deploy); a 304 on
    the ETag keeps that cheap. The rest of /static keeps its old headers."""
    for name in world_modules():
        assert (
            client.get(f"/static/world/{name}").headers.get("cache-control")
            == "no-cache"
        )
    first = client.get("/static/world/main.js")
    again = client.get(
        "/static/world/main.js", headers={"if-none-match": first.headers["etag"]}
    )
    assert again.status_code == 304
    assert again.headers.get("cache-control") == "no-cache"
    for path in ("/static/pixi-8.19.0.min.js", "/static/world-plate-btc-eth.png"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "cache-control" not in response.headers, path


def test_every_module_on_disk_is_loaded_and_served_as_javascript():
    modules = world_modules()
    assert sorted(p.name for p in WORLD_MODULE_DIR.iterdir()) == sorted(modules), (
        "a file in api/static/world/ that main.js never reaches (or a stray file)"
    )
    for name in modules:
        response = client.get(f"/static/world/{name}")
        assert response.status_code == 200, name
        ctype = response.headers["content-type"].split(";")[0].strip()
        assert ctype in ("text/javascript", "application/javascript"), (name, ctype)
        assert response.text == _source(name)


def test_modules_import_only_each_other_by_relative_path():
    """KI-045 for modules: an `import ... from "https://..."` would put a third
    party back in the on-air load path, which the template-level CDN check
    cannot see."""
    for name in world_modules():
        text = _source(name)
        specs = [spec for _, spec in _IMPORT.findall(text)]
        assert all(re.fullmatch(r"\./[\w-]+\.js", s) for s in specs), (name, specs)
        assert not re.search(r"\bimport\s*\(", text), f"{name}: dynamic import"
        assert not re.search(r"unpkg|jsdelivr|cdnjs", text), name


def test_every_import_is_exported_by_the_module_it_names():
    exports = {
        name: set().union(*[_names(g) for g in _EXPORT.findall(_source(name))])
        for name in world_modules()
    }
    for name in world_modules():
        for group, spec in _IMPORT.findall(_source(name)):
            missing = _names(group or "") - exports[spec[2:]]
            assert not missing, (
                f"{name} imports {sorted(missing)} that {spec} never exports"
            )


def test_the_modules_evaluate_in_the_single_scripts_order():
    """ES modules evaluate depth-first, dependencies first. Each module imports
    its predecessor FIRST, so evaluation runs the chain front to back and every
    forward import (a function calling one defined later) is a cycle back to a
    module already on the stack, resolved at call time, never at evaluation."""
    order = world_modules()
    assert order[-1] == "main.js"
    for prev, name in zip(order, order[1:]):
        first = _IMPORT.search(_source(name))
        assert first and first.group(2) == f"./{prev}", (
            f"{name} must import {prev} first to keep the evaluation order"
        )


_DRIVER = r"""
const CONFIG = process.argv[2];
const fake = new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => 0
    : k === "then" ? undefined
    : k === Symbol.iterator ? function* () {}
    : fake,
  set: () => true, apply: () => fake, construct: () => fake,
});
const element = (id) => id === "world-config" ? { textContent: CONFIG } : fake;
globalThis.document = new Proxy({}, {
  get: (t, k) => k === "getElementById" ? element : fake,
});
globalThis.PIXI = fake;
globalThis.window = fake;
globalThis.history = fake;
globalThis.location = { search: "", href: "http://test/world", reload() {} };
globalThis.fetch = () => new Promise(() => {});
globalThis.EventSource = function () { return fake; };
globalThis.setTimeout = globalThis.setInterval = () => 0;
globalThis.requestAnimationFrame = () => 0;
globalThis.CustomEvent = function () { return fake; };
await import("./main.js");
console.log("evaluated");
"""


@needs_node
def test_the_module_graph_links_and_evaluates_in_node(tmp_path):
    """The import graph is linked and evaluated for real: a missing export is a
    link-time SyntaxError and a binding read before its module ran is a
    ReferenceError, both fatal to the whole page. DOM and PIXI are stubbed; the
    config is the one /world serves."""
    page = client.get("/world").text
    config = re.search(
        r'<script type="application/json" id="world-config">(.*?)</script>', page, re.S
    )
    (tmp_path / "package.json").write_text('{"type": "module"}')
    for name in world_modules():
        (tmp_path / name).write_text(_source(name))
    (tmp_path / "driver.mjs").write_text(_DRIVER)
    result = subprocess.run(
        [NODE, str(tmp_path / "driver.mjs"), config.group(1)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("evaluated"), result.stdout + result.stderr
