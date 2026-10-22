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
from pathlib import Path

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


def test_no_script_stays_inline_but_the_config_block():
    """The JSON block stays inline because the server renders it per request
    (#41) and the page must not fetch its own config; it is data, not code."""
    page = client.get("/world").text
    inline = [(attrs, body) for attrs, body in _SCRIPT.findall(page) if body.strip()]
    assert [attrs for attrs, _ in inline] == [' type="application/json" id="world-config"']
    json.loads(inline[0][1])


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
            assert not missing, f"{name} imports {sorted(missing)} that {spec} never exports"


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
    config = re.search(r'<script type="application/json" id="world-config">(.*?)</script>', page, re.S)
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
