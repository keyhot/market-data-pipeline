import io

import pytest
from PIL import Image

from scripts.shoot import frame_is_blank, target_url


def _png(pixels_fn, size=(64, 64)) -> bytes:
    img = Image.new("RGB", size)
    img.putdata([pixels_fn(i) for i in range(size[0] * size[1])])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_a_uniformly_dark_frame_is_blank():
    # The exact failure mode observed on 2026-09-01: the page's background
    # colour and nothing else, because the shot fired before Pixi drew.
    assert frame_is_blank(_png(lambda i: (19, 23, 34))) is True


def test_a_frame_with_drawn_content_is_not_blank():
    # Bands of light and dark: any real frame of this room has both.
    assert (
        frame_is_blank(
            _png(lambda i: (240, 200, 90) if (i // 64) % 2 else (19, 23, 34))
        )
        is False
    )


def test_a_bright_but_flat_frame_is_still_blank():
    # A solid white error page has a high mean and no structure. Mean alone
    # would pass it, which is why stddev is also a floor.
    assert frame_is_blank(_png(lambda i: (255, 255, 255))) is True


@pytest.mark.parametrize(
    "gate,expected",
    [
        ("world", "http://localhost:8000/world"),
        ("gallery", "http://localhost:8000/world?gallery=1"),
        ("anims", "http://localhost:8000/world?anims=1"),
        ("swell", "http://localhost:8000/world?swell=3"),
    ],
)
def test_target_url_maps_each_gate(gate, expected):
    assert target_url("http://localhost:8000", gate) == expected


def test_target_url_rejects_an_unknown_gate():
    with pytest.raises(ValueError, match="unknown gate"):
        target_url("http://localhost:8000", "nope")
