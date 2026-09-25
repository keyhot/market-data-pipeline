"""KI-074: the manifest validators CI runs must also run where the manifest is
actually read.

The manifest is data, and editing it in a deployment is the intended
workflow. So a glow rect re-overlapping a live chart (KI-056) or a text
placement escaping its surface is an on-air mis-render the moment it is
saved — and before this, nothing but an unrelated pytest run would say so.
`CLAUDE.md` already decided this class: a startup warning naming the
problem, never a silent mis-render. `watchlist_disagreements` was the only
check wired in; these grade the other three through the same `/world` path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as main
from world.plate import load_manifest

MANIFEST = Path(main.__file__).resolve().parent / "static" / "world-plate-btc-eth.json"

client = TestClient(main.app)


def _warnings_for(monkeypatch, caplog, tmp_path, edit) -> list[str]:
    raw = json.loads(MANIFEST.read_text())
    edit(raw)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw))
    manifest = load_manifest(path)
    assert manifest is not None, "the edited manifest no longer loads at all"
    monkeypatch.setattr(main, "load_manifest", lambda: manifest)
    with caplog.at_level(logging.WARNING, logger=main.logger.name):
        assert client.get("/world").status_code == 200
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_shipped_manifest_raises_no_warnings(monkeypatch, caplog, tmp_path):
    assert _warnings_for(monkeypatch, caplog, tmp_path, lambda raw: None) == []


def test_a_glow_over_a_live_chart_is_a_startup_warning(monkeypatch, caplog, tmp_path):
    def edit(raw):
        chart = next(s for s in raw["screens"] if s.get("role") == "chart")
        raw["glow"][0].update(x=chart["x"], y=chart["y"], w=40, h=40)

    warnings = _warnings_for(monkeypatch, caplog, tmp_path, edit)
    glow_id = json.loads(MANIFEST.read_text())["glow"][0]["id"]
    assert any(glow_id in w and "chart" in w for w in warnings), warnings


def test_a_text_placement_off_its_surface_is_a_startup_warning(monkeypatch, caplog, tmp_path):
    def edit(raw):
        history = next(t for t in raw["text"] if t["id"] == "history")
        history["x"] = 5000

    warnings = _warnings_for(monkeypatch, caplog, tmp_path, edit)
    assert any("history" in w and "surface" in w for w in warnings), warnings


def test_a_banner_box_too_small_for_its_copy_is_a_startup_warning(monkeypatch, caplog, tmp_path):
    def edit(raw):
        banner = next(t for t in raw["text"] if t["id"] == "banner")
        banner["w"] = 200

    warnings = _warnings_for(monkeypatch, caplog, tmp_path, edit)
    assert any("banner" in w and "fit" in w for w in warnings), warnings


def test_the_banner_copy_checked_is_the_copy_the_page_draws():
    """The overflow check is only as good as its text: it must be the
    template's own sentence behind the page's own symbol prefix, not a
    second copy of it that can drift."""
    copy = main._banner_copy(["BTCUSDT", "ETHUSDT"])
    assert copy.startswith("BTC · ETH — ")
    page = " ".join(client.get("/world").text.split())
    assert copy.removeprefix("BTC · ETH — ") in page


@pytest.mark.parametrize("symbols", [["BTCUSDT", "ETHUSDT"]])
def test_the_watchlist_warning_still_fires(monkeypatch, caplog, tmp_path, symbols):
    def edit(raw):
        raw["symbols"] = raw["symbols"][:1]
        raw["tubes"] = raw["tubes"][:1]

    warnings = _warnings_for(monkeypatch, caplog, tmp_path, edit)
    assert any("disagrees with watchlist" in w for w in warnings), warnings
