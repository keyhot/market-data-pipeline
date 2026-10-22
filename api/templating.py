"""HTML template rendering shared by the page routers (#44).

Moved verbatim from `api/main.py`. `_TEMPLATES_DIR` is anchored at `api/`
(this file's own directory), exactly where it was when it lived in `main.py`.
"""

import json
import re
from pathlib import Path

from world import visuals
from world.state import GENERIC_TIER_CUTS, tier_cuts, tier_of_js

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# /world's script is native ES modules served from /static/world/ (#42); the
# page loads only the entry, `main.js`, and the browser follows its imports.
WORLD_MODULE_DIR = Path(__file__).parent / "static" / "world"
_MODULE_IMPORT = re.compile(r'^import\s+(?:\{[^}]*\}\s*from\s+)?"\./([\w-]+\.js)";', re.M)


def world_modules() -> list[str]:
    """/world's modules in the order the browser evaluates them: depth-first,
    post-order over the import graph from `main.js` (a module already being
    visited is a cycle and is skipped, as ES module evaluation skips it)."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dep in _MODULE_IMPORT.findall((WORLD_MODULE_DIR / name).read_text()):
            visit(dep)
        order.append(name)

    visit("main.js")
    return order


def world_page_source(page: str | None = None) -> str:
    """`world.html` followed by its modules in evaluation order: the page's
    whole source as one text, as it was one file before #42. The source checks
    and `scripts/sheet_build.py` read this, so none of them needs to know which
    module holds a given declaration. `page` stands in for the template with an
    already-rendered page (a test's served /world)."""
    if page is None:
        page = (_TEMPLATES_DIR / "world.html").read_text()
    return page + "".join(
        (WORLD_MODULE_DIR / name).read_text() for name in world_modules()
    )


# The shared visual identity (world/visuals.py) reaches every page from one
# place. `__THEME_VARS__` is CSS in a <style> block, so it has to be in the
# document before first paint and cannot ride in the JSON config below; the
# tier entries are what `/overlay/events` still reads. Everything `/world`'s
# script reads moved to `_world_config` (#41).
_THEME_REPLACEMENTS = {
    "__THEME_VARS__": visuals.css_variables(),
    "__TIER_STYLES__": visuals.tier_styles_css(),
    # The 0..3 tier scale, from world.state. Severities are rule-specific, so a
    # page that hard-codes absolute cuts disagrees with the server about how
    # big an event is — which is how every `signal_resolved` came out tier 0.
    "__TIER_CUTS_JSON__": json.dumps(
        {
            "cuts": {k: list(v) for k, v in tier_cuts().items()},
            "generic": list(GENERIC_TIER_CUTS),
        }
    ),
    # ...and the three lines that read them, which were written out twice,
    # byte-identical, in two templates. One definition, injected — see
    # `world.state.tier_of_js`. `/world` carries its own copy as page code
    # that reads `tier_cuts` from the config; a test runs it against
    # `severity_tier`, so neither copy can drift.
    "__TIER_OF_JS__": tier_of_js(),
}


def _render_template(name: str, replacements: dict[str, str]) -> str:
    html = (_TEMPLATES_DIR / name).read_text()
    # Page-specific replacements win over theme defaults if a key collides.
    for placeholder, value in {**_THEME_REPLACEMENTS, **replacements}.items():
        html = html.replace(placeholder, value)
    return html
