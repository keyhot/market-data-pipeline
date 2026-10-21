"""HTML template rendering shared by the page routers (#44).

Moved verbatim from `api/main.py`. `_TEMPLATES_DIR` is anchored at `api/`
(this file's own directory), exactly where it was when it lived in `main.py`.
"""

import json
from pathlib import Path

from world import visuals
from world.state import GENERIC_TIER_CUTS, tier_cuts, tier_of_js

_TEMPLATES_DIR = Path(__file__).parent / "templates"


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
