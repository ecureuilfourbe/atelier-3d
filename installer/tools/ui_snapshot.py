"""Outil de vérification visuelle (développement/tests) : pilote Edge/Chrome headless par le protocole DevTools,
attend qu'une condition JS soit vraie, exécute des actions JS facultatives et enregistre une capture PNG.

  python ui_snapshot.py URL sortie.png [--wait "js"] [--js "js"]... [--width 1700 --height 1050]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--wait", default="document.querySelectorAll('.snode').length > 0")
    ap.add_argument("--js", action="append", default=[])
    ap.add_argument("--width", type=int, default=1700)
    ap.add_argument("--height", type=int, default=1050)
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--settle", type=float, default=2.5)
    a = ap.parse_args()
    prof = tempfile.mkdtemp(prefix="a3d-ui-")
    port = 9333
    proc = subprocess.Popen([os.environ.get("A3D_BROWSER", EDGE), "--headless=new", f"--remote-debugging-port={port}",
                             f"--user-data-dir={prof}", f"--window-size={a.width},{a.height}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json").read())
                break
            except Exception:
                time.sleep(0.2)
        page = next(t for t in tabs if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120, suppress_origin=True)
        mid = [0]

        def cdp(method, **params):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == mid[0]:
                    return m.get("result", {})

        def ev(expr):
            r = cdp("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
            return (r.get("result") or {}).get("value")

        cdp("Emulation.setDeviceMetricsOverride", width=a.width, height=a.height, deviceScaleFactor=1, mobile=False)
        cdp("Page.enable")
        cdp("Page.navigate", url=a.url)
        t0 = time.time()
        while time.time() - t0 < a.timeout:
            if ev(f"(() => {{ try {{ return !!({a.wait}); }} catch (e) {{ return false; }} }})()"):
                break
            time.sleep(0.5)
        else:
            print("condition d'attente non atteinte")
        time.sleep(a.settle)
        for js in a.js:
            print("js:", ev(f"(async () => {{ {js} }})()"))
            time.sleep(a.settle)
        ev("document.querySelectorAll('.react-flow__node').forEach(n => n.style.willChange = 'auto')")
        time.sleep(0.5)
        shot = cdp("Page.captureScreenshot", format="png", captureBeyondViewport=True, fromSurface=True)
        pathlib.Path(a.out).write_bytes(base64.b64decode(shot["data"]))
        print("capture :", a.out)
        ws.close()
    finally:
        proc.kill()
        shutil.rmtree(prof, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
