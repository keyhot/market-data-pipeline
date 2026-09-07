"""Text placement is manifest data, so "no text in the air" is a test.

Spec: this sprint's own brief (task-9), and the design rule it quotes
verbatim: "the text can be written on top of something or in the screen, but
not randomly above something. Thus, the text only where it is necessary."

`floating_text` is the check: every placement in `manifest.text` must land
fully inside the surface (`manifest.text_surfaces`, plus the two band
surfaces `world.text_layout` derives from `bands`/`canvas`) it names. A
placement whose surface is missing entirely, or whose rect escapes the one
it names, is "floating" — text in the air.
"""
import dataclasses

from world.plate import load_manifest
from world.text_layout import floating_text, glyph_overflow


def test_nothing_in_the_shipped_room_floats_in_the_air():
    assert floating_text(load_manifest()) == []


def test_a_label_nudged_off_its_surface_is_caught():
    # Mutation check. Without it the assertion above passes on an empty
    # placement list and a broken containment test alike — the "test that
    # cannot fail" class that shipped four times last sprint.
    manifest = load_manifest()
    placements = [dict(p) for p in manifest.text]
    placements[0]["y"] -= 400
    poisoned = dataclasses.replace(manifest, text=tuple(placements))
    assert floating_text(poisoned) == [placements[0]["id"]]


def test_a_placement_naming_a_surface_that_does_not_exist_is_caught():
    manifest = load_manifest()
    placements = [dict(p) for p in manifest.text]
    placements[0]["surface"] = "no-such-surface"
    poisoned = dataclasses.replace(manifest, text=tuple(placements))
    assert floating_text(poisoned) == [placements[0]["id"]]


def test_the_manifest_declares_no_mood_placement():
    # Ruling (task-9-brief round): this only checks the manifest THIS task
    # authors — of course it has no "mood" id, nothing here ever added one.
    # The claim that actually matters, that the *page* draws no mood label
    # beside a character, is a page-level assertion and belongs to Task 10,
    # which is where the page changes. Tasks 6 and 8 exist to make mood
    # readable from the figure itself; a mood label standing beside a shaded
    # character would be the art admitting it failed.
    ids = {p["id"] for p in load_manifest().text}
    assert not any("mood" in i for i in ids)


def test_the_banner_survives():
    # Deliberate: it is the newcomer's one-line answer to "what is this", and
    # it already sits on the painted quiet strip, so it satisfies the rule.
    assert "banner" in {p["id"] for p in load_manifest().text}


# --- Task 10: the gap `floating_text` cannot see ---------------------------
#
# A placement can be a valid box, fully inside a real surface, and still have
# no room for its own text. `glyph_overflow` is the check that would have
# caught Task 9's first-pass `tube-plinth-*` sizing (9-10px surfaces given an
# 8px line) before a human had to notice it by eye.


def test_the_shipped_room_labels_have_room_for_their_own_text():
    # The real, shipped content at the real, shipped size — not a synthetic
    # string. "TRADER"/"MODEL" are literal (the page draws them verbatim);
    # a two-symbol watchlist is this project's only shipped watchlist, so
    # both real symbols stand in for "prices"/"banner"-style short content.
    placements = {p["id"]: p for p in load_manifest().text}
    for label_id, text in [("name-trader", "TRADER"), ("name-model", "MODEL")]:
        p = placements[label_id]
        assert not glyph_overflow(text, p["size"], p), (
            f"{label_id}: {text!r} at size {p['size']} does not fit its own "
            f"{p['w']}x{p['h']} box"
        )


def test_a_string_too_long_for_its_box_is_caught():
    # Mutation check for the estimate itself: the real name-trader box, with
    # a string no reasonable margin would ever fit, must overflow.
    p = next(p for p in load_manifest().text if p["id"] == "name-trader")
    assert glyph_overflow("THIS LABEL IS WAY TOO LONG FOR THE DESK", p["size"], p)


def test_a_box_too_short_for_its_own_font_size_is_caught():
    # The exact shape of Task 9's own near-miss: a box whose HEIGHT is
    # smaller than the line the box's own declared `size` needs, independent
    # of width. Real text, deliberately undersized box.
    tight = {"w": 9999, "h": 9}
    assert glyph_overflow("MODEL", 14, tight)
