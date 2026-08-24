"""Does /world actually survive a renderer that will not load?

The boot retry is the half of the KI-045 fix that vendoring does not cover:
vendoring removes the CDN as a *cause*, the retry handles every other boot
failure, including the recurring one — a browser source reloading while the
API container restarts, i.e. every deploy.

None of that is reachable from the pytest suite, which can only assert that
the page *contains* the code. So this drives the real page in real Chrome with
the renderer script made unfetchable, and reads what the page does. The local
server's request log is the assertion: every reload the page performs arrives
here as a request for /world?boot_retry=N.

Not a CI test — needs Chrome and a running API, like scripts/smoke_test.sh.

    python scripts/check_world_boot.py            # all cases
    python scripts/check_world_boot.py block      # one case

Cases:
  block      renderer 503s          -> 5 retries, then stops (the KI-045 shape)
  latethrow  renderer loads, boot
             throws after it        -> 5 retries, then stops. This is the
                                       window the retry cap used to be reset
                                       in; before the fix it looped forever.
  junk       ?boot_retry=abc        -> stops immediately, never reloads

The happy path is deliberately NOT a case here. Headless Chrome hangs on this
page's SSE and dumps zero bytes once boot succeeds and subscribe() runs — the
same limitation that made screenshotting /world unworkable. A healthy render is
verified where it matters instead: an OBS screenshot of the live scene. Every
case below returns *before* subscribe(), which is why they are drivable at all.
"""

import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = os.environ.get("WORLD_BOOT_API", "http://127.0.0.1:8000")
PORT = int(os.environ.get("WORLD_BOOT_PORT", "8731"))
MAX_RETRIES = 5  # len(RETRY_BACKOFF_MS) in world.html

# PIXI that gets past the guard and past app.init(), then throws — i.e. fails
# exactly where the counter used to be cleared.
LATE_THROW_JS = (
    b"window.PIXI={"
    b"Application:function(){this.init=async()=>{"
    b"this.canvas=document.createElement('canvas');};"
    b"this.screen={width:1920,height:1080};this.stage={addChild(){}};},"
    b"Container:function(){throw new Error('late boot failure');}"
    b"};"
)


def run_case(name, case, start=""):
    world_html = urllib.request.urlopen(API + "/world").read().decode()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            if body:
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            seen.append(self.path)
            if self.path.startswith("/world?") or self.path == "/world":
                return self._send(200, world_html.encode(), "text/html; charset=utf-8")
            if "pixi" in self.path:
                if case == "block":
                    return self._send(503)
                if case == "latethrow":
                    return self._send(200, LATE_THROW_JS, "text/javascript")
            if self.path.startswith("/static/"):
                data = urllib.request.urlopen(API + self.path).read()
                return self._send(200, data, "text/javascript")
            if self.path.startswith("/stream/"):
                # never proxy SSE — headless Chrome hangs on this page's stream
                return self._send(204)
            try:
                data = urllib.request.urlopen(API + self.path, timeout=5).read()
            except Exception:
                data = b'{"data":{}}'
            return self._send(200, data)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proc = subprocess.run(
            [
                "google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
                f"--user-data-dir={tempfile.mkdtemp()}",
                # fast-forwards the 2s/5s/15s/30s/60s backoff
                "--virtual-time-budget=200000",
                "--dump-dom", f"http://127.0.0.1:{PORT}/world{start}",
            ],
            capture_output=True, text=True, timeout=180,
        )
        dom = proc.stdout
    except subprocess.TimeoutExpired:
        # Chrome never settled: the page is still reloading. That IS the
        # failure — an unattended surface looping against its own API.
        srv.shutdown()
        loads = [r for r in seen if r.startswith("/world")]
        return False, (
            f"never stopped reloading ({len(loads)} loads, cap is {MAX_RETRIES})"
        )
    finally:
        srv.shutdown()

    loads = [r for r in seen if r.startswith("/world")]
    match = re.search(r'id="fallback"[^>]*>([^<]*)<', dom)
    text = match.group(1) if match else ""
    retries = len(loads) - 1

    if name == "junk":
        ok = retries == 0 and "retrying" not in text and text != ""
        return ok, f"loads={loads} fallback={text!r}"
    ok = retries == MAX_RETRIES and "retrying" not in text and text != ""
    return ok, f"retries={retries} (cap {MAX_RETRIES}) fallback={text!r}"


CASES = {
    "block": ("block", ""),
    "latethrow": ("latethrow", ""),
    "junk": ("block", "?boot_retry=abc"),
}

if __name__ == "__main__":
    wanted = sys.argv[1:] or list(CASES)
    failures = 0
    for name in wanted:
        case, start = CASES[name]
        ok, detail = run_case(name, case, start)
        print(f"{'PASS' if ok else 'FAIL'}  {name:10s} {detail}")
        failures += not ok
    sys.exit(1 if failures else 0)
