"""Long-running headless Chromium driven over a tiny local HTTP API.

The self-playtest browser when the Claude-in-Chrome extension is not
connected: run it in the background, then send it commands with
scripts/browser_cmd.py (eval JS, keys, clicks, screenshots). Headless, so
requestAnimationFrame runs and the PixiJS canvas animates (unlike a hidden
extension tab). Run with Playwright available, e.g.:

    uv run --no-project --with playwright python scripts/browser_driver.py 12555
    python scripts/browser_cmd.py goto http://127.0.0.1:12100/
    python scripts/browser_cmd.py shot shot.png

Set FORGE_CHROMIUM to a chrome.exe if Playwright's bundled browser version
does not match the installed one.

POST /cmd  {"op": ..., ...}  -> JSON result
  goto {url}
  eval {js}                      (async function body allowed: wrap yourself)
  shot {path, full?, clip?}
  key  {key, mode: press|down|up, delay?}
  hold {keys: [..], ms}          hold keys for ms (real time)
  click {x, y} | {selector}
  type {selector, text}
  console {clear?}
  wait {ms}
"""
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 12555
W, H = (1280, 900)

cmds: "queue.Queue[tuple[dict, queue.Queue]]" = queue.Queue()
console_log: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            assert isinstance(body, dict)
        except Exception:
            body = {"op": "noop"}
        reply: queue.Queue = queue.Queue()
        cmds.put((body, reply))
        try:
            res = reply.get(timeout=float(body.get("timeout", 300)))
        except queue.Empty:
            res = {"error": "timeout"}
        data = json.dumps(res, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=os.environ.get("FORGE_CHROMIUM") or None,
            args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                  "--autoplay-policy=no-user-gesture-required"],
        )
        ctx = browser.new_context(viewport={"width": W, "height": H})
        page = ctx.new_page()
        page.on("console", lambda m: console_log.append(f"[{m.type}] {m.text}"[:600]))
        page.on("dialog", lambda d: (console_log.append(f"[dialog] {d.message}"), d.dismiss()))
        page.on("pageerror", lambda e: console_log.append(f"[pageerror] {e}"[:600]))
        print(f"driver on {PORT}", flush=True)
        while True:
            try:
                body, reply = cmds.get(timeout=0.05)
            except queue.Empty:
                # keep the event loop pumping so the page runs
                try:
                    page.wait_for_timeout(10)
                except Exception:
                    pass
                continue
            op = body.get("op")
            try:
                if op == "goto":
                    page.goto(body["url"], wait_until="domcontentloaded")
                    res = {"ok": True, "url": page.url}
                elif op == "eval":
                    ms = int(body.get("ms", 30000))
                    wrapped = ("Promise.race([(async()=>{ return (" + body["js"] + "); })(),"
                               " new Promise(r=>setTimeout(()=>r('__timeout__'), " + str(ms) + "))])")
                    res = {"value": page.evaluate(wrapped)}
                elif op == "shot":
                    kw = {"path": body["path"], "full_page": bool(body.get("full"))}
                    if body.get("clip"):
                        x, y, w, h = body["clip"]
                        kw["clip"] = {"x": x, "y": y, "width": w, "height": h}
                    page.screenshot(**kw)
                    res = {"ok": True, "path": body["path"]}
                elif op == "key":
                    mode = body.get("mode", "press")
                    getattr(page.keyboard, mode)(body["key"])
                    res = {"ok": True}
                elif op == "hold":
                    for k in body["keys"]:
                        page.keyboard.down(k)
                    page.wait_for_timeout(int(body.get("ms", 500)))
                    for k in body["keys"]:
                        page.keyboard.up(k)
                    res = {"ok": True}
                elif op == "click":
                    if body.get("selector"):
                        page.click(body["selector"], timeout=10000)
                    else:
                        page.mouse.click(body["x"], body["y"])
                    res = {"ok": True}
                elif op == "type":
                    page.fill(body["selector"], body["text"])
                    res = {"ok": True}
                elif op == "console":
                    res = {"lines": console_log[-int(body.get("n", 60)):]}
                    if body.get("clear"):
                        console_log.clear()
                elif op == "wait":
                    page.wait_for_timeout(int(body.get("ms", 1000)))
                    res = {"ok": True}
                elif op == "quit":
                    reply.put({"ok": True})
                    break
                else:
                    res = {"error": f"unknown op {op}"}
            except Exception as e:
                res = {"error": repr(e)[:1500]}
            reply.put(res)
        browser.close()


if __name__ == "__main__":
    main()
