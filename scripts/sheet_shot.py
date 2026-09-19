#!/usr/bin/env python3
"""Photograph the model sheet.

`scripts/shoot.py` is the room's looking glass and its `GATES` are the room's
surfaces; the sheet is not one of them and does not belong in that list. What
it does own — and what is worth reusing rather than re-learning — is the pair
of guards: readiness is POLLED (`_READY_JS`), and the resulting frame is
INSPECTED (`frame_is_blank`) so a harness can never hand back a picture of
nothing while reporting success. Both are imported.

The sheet is static, so this also owns the two steps the app would otherwise
do: it rebuilds `prelude.js` out of `world.html` (so the figure is always lit
by the page as it stands right now, never by a stale copy) and serves the
worktree over `http.server` on a throwaway port.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import http.server
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.sheet_build import SHEET_H, SHEET_W  # noqa: E402
from scripts.shoot import _READY_JS, _Session, frame_is_blank  # noqa: E402

PAGE_PATH = "/tools/charsheet/index.html"
DEFAULT_OUT = ROOT / "data" / "visual-qa" / "char-redesign"

# The sheet's own canvas, not the room's 1920x960. Judging a sheet in a
# viewport smaller than itself crops the columns it exists to compare -- and
# a cropped sheet still looks like a finished picture, which is the failure
# mode this whole harness was adopted to end. So the number is IMPORTED from
# the one place that defines it rather than kept in step by hand.
VIEWPORT = (SHEET_W, SHEET_H)


@contextlib.contextmanager
def _serving(directory: Path):
    """The worktree over HTTP on a free port, for as long as the shot takes."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        httpd.allow_reuse_address = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd.server_address[1]
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def shoot_sheet(out: Path, *, settle: float = 1.5) -> Path:
    import shutil
    import tempfile

    from scripts.sheet_build import main as build_prelude

    build_prelude()

    profile = Path(tempfile.mkdtemp(prefix="sheet-chrome-"))
    with _serving(ROOT) as port:
        url = f"http://127.0.0.1:{port}{PAGE_PATH}"
        try:
            session = _Session(_free_port(), profile)
        except Exception:
            shutil.rmtree(profile, ignore_errors=True)
            raise
        try:
            session.cmd("Page.enable")
            session.cmd(
                "Emulation.setDeviceMetricsOverride",
                width=VIEWPORT[0], height=VIEWPORT[1],
                deviceScaleFactor=1, mobile=False,
            )
            session.cmd("Page.navigate", url=url)
            for _ in range(80):                      # 20s ceiling
                time.sleep(0.25)
                value = session.cmd(
                    "Runtime.evaluate", expression=_READY_JS, returnByValue=True
                ).get("result", {}).get("value")
                if value == "drawn":                 # the sheet sets this itself
                    break
            else:
                # Deliberately stricter than `shoot.py`: that script accepts a
                # bare canvas so it still works against an older build of the
                # room. Here the page is in this commit and sets the flag, so
                # "canvas exists" would only ever mean the draw threw.
                raise SystemExit(
                    "REFUSED: the sheet never reported a drawn frame. Check the "
                    "page's own error box — boot() catches and prints it."
                )
            time.sleep(settle)
            png = base64.b64decode(
                session.cmd("Page.captureScreenshot", format="png")["data"]
            )
        finally:
            session.close()
            shutil.rmtree(profile, ignore_errors=True)

    if frame_is_blank(png):
        raise SystemExit(
            "REFUSED: the sheet capture is blank. Nothing was written — fix the "
            "page, do not lower the floor."
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--settle", type=float, default=1.5)
    args = parser.parse_args()
    out = args.out or DEFAULT_OUT / f"sheet-{time.strftime('%Y%m%d-%H%M%S')}.png"
    print("wrote", shoot_sheet(out, settle=args.settle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
