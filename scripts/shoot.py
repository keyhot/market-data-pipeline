#!/usr/bin/env python3
"""The looking glass — a screenshot of the room that cannot silently lie.

Aesthetics is the one thing no assertion grades, so an art sprint's real
evidence is a picture. This produces one in ~15 seconds with no OBS, no docker
build and no deploy, against a local uvicorn on the working branch.

**Why CDP and not `google-chrome --screenshot`.** Measured on 2026-09-01: the
simple form returns a 14,050-byte image of an *empty canvas* for
`/world?gallery=1`, and does so identically at `--virtual-time-budget` 8000 and
25000, with and without `--disable-gpu`, and under swiftshader. Virtual time
does not wait for PixiJS to finish booting, and nothing in the exit status says
so. The same page over CDP returns 60,963 bytes of actual cast. A harness that
can hand back the wrong picture without saying so is worse than no harness.

Two independent guards, because either alone has a hole:

1. **Readiness is polled, not timed** — the page is asked whether it has drawn,
   and only then photographed.
2. **The result is inspected** — a frame that is uniformly dark (the exact
   failure above) or uniformly bright (an error page) is *rejected*, not
   written. `frame_is_blank` is the pure half and is what the tests cover.

The Chrome driving itself is NOT covered by tests — it is a subprocess and a
websocket, i.e. a seam. It is exercised every time anyone runs this script,
which for this sprint is constantly.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GATES = {
    "world": "/world",
    "gallery": "/world?gallery=1",
    "anims": "/world?anims=1",
    "swell": "/world?swell=3",
}

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "visual-qa"


def target_url(base: str, gate: str) -> str:
    """The URL for a named surface. Unknown gates are a typo, not a default."""
    if gate not in GATES:
        raise ValueError(f"unknown gate {gate!r}; expected one of {sorted(GATES)}")
    return base.rstrip("/") + GATES[gate]


def frame_is_blank(png_bytes: bytes, *, min_stddev: float = 6.0, min_mean: float = 12.0) -> bool:
    """True when the image carries no drawn content.

    Both floors are needed. A frame that shot before Pixi drew is the page
    background — low mean, near-zero spread. A frame of an error page is white
    — high mean, near-zero spread. Structure is what both lack, so stddev is
    the real test and the mean floor only catches a black frame that somehow
    dithered.
    """
    from PIL import Image, ImageStat

    stat = ImageStat.Stat(Image.open(io.BytesIO(png_bytes)).convert("L"))
    return stat.stddev[0] < min_stddev or stat.mean[0] < min_mean


class _Session:
    """A minimal Chrome DevTools Protocol client. No new dependency."""

    def __init__(self, port: int, profile: Path):
        self._proc = subprocess.Popen(
            [
                "google-chrome", "--headless=new", "--no-sandbox", "--hide-scrollbars",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",     # without this the WS handshake 403s
                f"--user-data-dir={profile}",   # without this it may attach to a live profile
                "--window-size=1920,960", "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import websocket

        page = None
        for _ in range(80):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.25)
        if page is None:
            self.close()
            raise RuntimeError("chrome never exposed a page target")
        self._ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
        self._id = 0

    def cmd(self, method: str, **params):
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == self._id:
                return msg.get("result", {})

    def close(self):
        try:
            self._proc.terminate()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()


# The page sets this once it has drawn at least one frame (Task 2 adds it).
# Falling back to a canvas-exists probe keeps this script useful against an
# older build rather than hanging on a flag that isn't there yet.
_READY_JS = (
    "(() => { if (window.__worldDrawn) return 'drawn';"
    " const c = document.querySelector('canvas');"
    " return c && c.width > 0 ? 'canvas' : 'none'; })()"
)


def shoot(gate: str, out: Path, base: str, *, settle: float = 3.0, port: int = 9333) -> Path:
    profile = Path(tempfile.mkdtemp(prefix="shoot-chrome-"))
    session = _Session(port, profile)
    try:
        session.cmd("Page.enable")
        session.cmd("Page.navigate", url=target_url(base, gate))
        for _ in range(120):          # 30s ceiling
            time.sleep(0.25)
            value = session.cmd(
                "Runtime.evaluate", expression=_READY_JS, returnByValue=True
            ).get("result", {}).get("value")
            if value in ("drawn", "canvas"):
                break
        else:
            raise RuntimeError("page never reported a drawn frame")
        time.sleep(settle)            # let the plate texture and first candles land
        png = base64.b64decode(session.cmd("Page.captureScreenshot", format="png")["data"])
    finally:
        session.close()
        shutil.rmtree(profile, ignore_errors=True)

    if frame_is_blank(png):
        raise SystemExit(
            f"REFUSED: the capture of {gate!r} is blank. The page did not draw. "
            "Nothing was written — fix the page, do not lower the floor."
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=sorted(GATES))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--settle", type=float, default=3.0)
    args = parser.parse_args()
    out = args.out or DEFAULT_OUT_DIR / f"{args.gate}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    print("wrote", shoot(args.gate, out, args.base, settle=args.settle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
