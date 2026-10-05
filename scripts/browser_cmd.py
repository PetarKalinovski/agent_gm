"""Client for scripts/browser_driver.py. Usage:
  python pwc.py eval [ms] <<'JS'  ...expression...  JS
  python pwc.py shot name.png [x y w h]
  python pwc.py key KeyW [press|down|up]
  python pwc.py hold 800 w d
  python pwc.py console [n] [clear]
  python pwc.py goto URL
  python pwc.py click x y | click "#selector"
"""
import json
import sys
import urllib.request


def call(body, timeout=330):
    req = urllib.request.Request("http://127.0.0.1:12555/cmd", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    op = a[0]
    if op == "eval":
        ms = int(a[1]) if len(a) > 1 else 30000
        js = sys.stdin.read()
        res = call({"op": "eval", "js": js, "ms": ms})
        v = res.get("value", res)
        print(v if isinstance(v, str) else json.dumps(v, indent=1, ensure_ascii=False))
        return
    if op == "shot":
        body = {"op": "shot", "path": a[1]}
        if len(a) == 6:
            body["clip"] = [int(x) for x in a[2:6]]
        print(call(body))
        return
    if op == "key":
        print(call({"op": "key", "key": a[1], "mode": a[2] if len(a) > 2 else "press"}))
        return
    if op == "hold":
        print(call({"op": "hold", "ms": int(a[1]), "keys": a[2:]}))
        return
    if op == "console":
        res = call({"op": "console", "n": int(a[1]) if len(a) > 1 else 40, "clear": len(a) > 2})
        print("\n".join(res.get("lines", [])))
        return
    if op == "goto":
        print(call({"op": "goto", "url": a[1]}))
        return
    if op == "click":
        if len(a) == 2:
            print(call({"op": "click", "selector": a[1]}))
        else:
            print(call({"op": "click", "x": int(a[1]), "y": int(a[2])}))
        return
    if op == "wait":
        print(call({"op": "wait", "ms": int(a[1])}))
        return
    print(call(json.loads(a[1])))


if __name__ == "__main__":
    main()
