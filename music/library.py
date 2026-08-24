"""The music bed's library: credits in git, audio files outside it.

The audio itself is ~85 minutes of mp3 that has no business in a repo, so it
lives in ``STREAM_AUDIO_DIR``. What *is* in git is `config/music_tracks.json` —
title, artist, source and licence per file. That split is deliberate: the
manifest is the thing we make a claim about on air, so it gets reviewed and
versioned; the bytes are just bytes.

Neither Mixkit's nor Pixabay's licence requires attribution, so the on-air
credit is a courtesy. Which is exactly why the data behind it has to be right —
a voluntary credit that names the wrong artist is worse than no credit.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent.parent / "config" / "music_tracks.json"
)


class MusicLibraryError(RuntimeError):
    """The manifest is unreadable, malformed, or credits a track we can't play."""


@dataclass(frozen=True)
class Track:
    """One playable track and everything we say about it on air."""

    file: str
    title: str
    artist: str
    source: str
    source_url: str
    license: str
    license_url: str
    duration_seconds: float

    @property
    def credit(self) -> str:
        """The one-line on-air credit: what the overlay renders."""
        return f"{self.title} — {self.artist}"


_REQUIRED = ("file", "title", "artist", "source", "source_url", "license")


def load_manifest(path: str | os.PathLike | None = None) -> list[Track]:
    """Parse the credits manifest. Raises rather than degrading: a bed that
    plays with missing credits is the one failure mode worth refusing."""
    manifest_path = Path(path) if path else DEFAULT_MANIFEST
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MusicLibraryError(f"no music manifest at {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise MusicLibraryError(
            f"malformed music manifest {manifest_path}: {exc}"
        ) from exc

    tracks: list[Track] = []
    for entry in raw.get("tracks", []):
        missing = [k for k in _REQUIRED if not entry.get(k)]
        if missing:
            raise MusicLibraryError(
                f"track {entry.get('file', '?')} is missing {', '.join(missing)}"
            )
        tracks.append(
            Track(
                file=entry["file"],
                title=entry["title"],
                artist=entry["artist"],
                source=entry["source"],
                source_url=entry["source_url"],
                license=entry["license"],
                license_url=entry.get("license_url", ""),
                duration_seconds=float(entry.get("duration_seconds") or 0.0),
            )
        )
    return tracks


def audio_dir() -> Path | None:
    """``STREAM_AUDIO_DIR``, or None when the bed is not configured — the same
    unset-means-off contract the scene spec has always used."""
    value = os.environ.get("STREAM_AUDIO_DIR", "").strip()
    return Path(value).expanduser() if value else None


def playable_tracks(
    tracks: list[Track] | None = None, directory: Path | None = None
) -> list[Track]:
    """Manifest entries whose audio actually exists on this host.

    The manifest travels with the repo and the mp3s do not, so a fresh host
    (`stream-a1`) has credits for tracks it cannot play. Filtering here means
    that host plays a shorter bed instead of a silent one.
    """
    tracks = load_manifest() if tracks is None else tracks
    directory = audio_dir() if directory is None else directory
    if directory is None:
        return []
    return [t for t in tracks if (directory / t.file).is_file()]


def track_path(track: Track, directory: Path | None = None) -> Path:
    directory = audio_dir() if directory is None else directory
    if directory is None:
        raise MusicLibraryError("STREAM_AUDIO_DIR is not set")
    return directory / track.file


def shuffled(
    tracks: list[Track], seed: int, avoid_first: str | None = None
) -> list[Track]:
    """A deterministic shuffle, seeded by the caller so tests and reruns agree.

    ``avoid_first`` keeps the track that just finished off the front of the next
    pass — the one repetition a listener actually notices is the same song twice
    across a wrap.
    """
    order = list(tracks)
    random.Random(seed).shuffle(order)
    if avoid_first and len(order) > 1 and order[0].file == avoid_first:
        order.append(order.pop(0))
    return order
