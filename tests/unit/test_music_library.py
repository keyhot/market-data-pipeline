"""The manifest is the on-air claim about someone else's work, so it gets
checked like data, not like config."""

import json

import pytest

from music import library
from music.library import MusicLibraryError, Track, load_manifest, playable_tracks

REAL_MANIFEST = library.DEFAULT_MANIFEST


def _entry(**over):
    base = {
        "file": "a.mp3",
        "title": "A",
        "artist": "Someone",
        "source": "Mixkit",
        "source_url": "https://mixkit.co/free-stock-music/",
        "license": "Mixkit Stock Music Free License",
        "license_url": "https://mixkit.co/license/#musicFree",
        "duration_seconds": 100,
    }
    base.update(over)
    return base


def _write(tmp_path, entries):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"tracks": entries}))
    return path


# --- the shipped manifest -------------------------------------------------

def test_the_shipped_manifest_parses():
    assert len(load_manifest()) >= 1


def test_every_shipped_track_names_a_title_artist_source_and_licence():
    for t in load_manifest():
        assert t.title.strip() and t.artist.strip(), t.file
        assert t.source_url.startswith("https://"), t.file
        assert t.license.strip() and t.license_url.startswith("https://"), t.file


def test_no_shipped_track_carries_an_unescaped_html_entity():
    """The credits were scraped from HTML; `Don&#39;t` on air would be a tell."""
    for t in load_manifest():
        assert "&#" not in t.title and "&amp;" not in t.title, t.file
        assert "&#" not in t.artist and "&amp;" not in t.artist, t.file


def test_shipped_files_are_unique():
    files = [t.file for t in load_manifest()]
    assert len(files) == len(set(files))


def test_only_licences_that_permit_a_stream_are_shipped():
    allowed = {"Mixkit Stock Music Free License", "Pixabay Content License"}
    assert {t.license for t in load_manifest()} <= allowed


# --- parsing contract -----------------------------------------------------

def test_a_track_missing_its_artist_is_refused(tmp_path):
    path = _write(tmp_path, [_entry(artist="")])
    with pytest.raises(MusicLibraryError, match="artist"):
        load_manifest(path)


def test_a_missing_manifest_is_refused(tmp_path):
    with pytest.raises(MusicLibraryError, match="no music manifest"):
        load_manifest(tmp_path / "nope.json")


def test_a_malformed_manifest_is_refused(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json")
    with pytest.raises(MusicLibraryError, match="malformed"):
        load_manifest(path)


def test_the_credit_line_is_title_then_artist():
    track = Track(**{k: v for k, v in _entry().items()})
    assert track.credit == "A — Someone"


# --- what this host can actually play -------------------------------------

def test_only_tracks_whose_audio_exists_are_playable(tmp_path):
    """The manifest ships in git and the mp3s do not, so a fresh host has
    credits for tracks it cannot play. It should play a shorter bed, not fail."""
    (tmp_path / "here.mp3").write_bytes(b"\xff\xfb\x00\x00")
    tracks = [Track(**_entry(file="here.mp3")), Track(**_entry(file="gone.mp3"))]
    assert [t.file for t in playable_tracks(tracks, tmp_path)] == ["here.mp3"]


def test_no_audio_dir_means_no_bed(monkeypatch):
    monkeypatch.delenv("STREAM_AUDIO_DIR", raising=False)
    assert library.audio_dir() is None
    assert playable_tracks([Track(**_entry())]) == []


def test_audio_dir_expands_a_home_relative_path(monkeypatch):
    monkeypatch.setenv("STREAM_AUDIO_DIR", "~/Music/market-stream")
    assert "~" not in str(library.audio_dir())
