"""Démarrage/arrêt des processus d'Atelier 3D : serveur API, worker, ComfyUI dédié (et ComfyUI vidéo s'il a été lancé par nous).

Seuls les processus enregistrés dans <home>/run/state.json, dont le PID, l'heure de création et l'exécutable
correspondent, sont arrêtés. Les processus étrangers (votre ComfyUI lancé à la main, etc.) ne sont jamais touchés.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import webbrowser

import psutil

from . import config as C

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def state_path(cfg) -> pathlib.Path:
    return cfg.run_dir / "state.json"


def read_state(cfg) -> dict:
    try:
        return json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(cfg, st: dict) -> None:
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int, avoid: set[int] = frozenset()) -> int:
    p = preferred
    for _ in range(200):
        if p not in avoid and port_free(p):
            return p
        p += 1
    raise RuntimeError(f"Aucun port libre à partir de {preferred}")


def owned_alive(rec: dict | None) -> psutil.Process | None:
    """Processus vivant ET identique à celui que nous avons lancé (PID + heure de création + exécutable)."""
    if not rec:
        return None
    try:
        p = psutil.Process(rec["pid"])
        if abs(p.create_time() - rec["create_time"]) > 1.0:
            return None
        if rec.get("exe") and os.path.normcase(p.exe()) != os.path.normcase(rec["exe"]):
            return None
        return p
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
        return None


def spawn(cmd: list[str], log_file: pathlib.Path, env: dict | None = None, cwd: str | None = None) -> dict:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_file, "ab", buffering=0)
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    e = dict(os.environ)
    e.update(env or {})
    try:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd, env=e,
                                creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd, env=e,
                                creationflags=flags, close_fds=True)
    p = psutil.Process(proc.pid)
    return {"pid": proc.pid, "create_time": p.create_time(), "exe": p.exe(), "cmd": cmd, "log": str(log_file), "started_at": time.time()}


def http_ok(url: str, timeout: float = 2.0) -> bool:
    import httpx

    try:
        r = httpx.get(url, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def comfy_cmd(cfg, port: int, low_memory: bool = False) -> list[str]:
    c = cfg.section("comfyui")
    home = cfg.home
    cmd = [c["python"], "-s", c["main"], "--windows-standalone-build", "--listen", "127.0.0.1", "--port", str(port),
           "--disable-auto-launch", "--extra-model-paths-config", str(home / "config" / "extra_model_paths.yaml"),
           "--input-directory", str(home / "data" / "comfy" / "input"), "--output-directory", str(home / "data" / "comfy" / "output"),
           "--temp-directory", str(home / "data" / "comfy" / "temp"), "--user-directory", str(home / "data" / "comfy" / "user"),
           "--preview-method", "auto"]
    cmd += list(c.get("extra_args") or [])
    if low_memory:
        cmd += list(c.get("low_memory_args") or [])
    return cmd


def start_comfy(cfg, st: dict, low_memory: bool = False) -> dict:
    c = cfg.section("comfyui")
    rec = st.get("comfyui")
    if owned_alive(rec):
        return rec
    port = pick_port(int(c.get("port", 8190)), avoid={st.get("app", {}).get("port", 0)})
    env = {"PYTHONUTF8": "1", "CUDA_PATH": ""}
    rec = spawn(comfy_cmd(cfg, port, low_memory), cfg.logs_dir / "comfyui.log", env=env, cwd=str(pathlib.Path(c["dir"])))
    rec.update({"port": port, "url": f"http://127.0.0.1:{port}", "low_memory": low_memory})
    st["comfyui"] = rec
    write_state(cfg, st)
    return rec


def start(open_browser: bool = True, wait: float = 90.0) -> int:
    cfg = C.load()
    st = read_state(cfg)
    py = sys.executable
    app = owned_alive(st.get("app"))
    if app and http_ok(f"http://127.0.0.1:{st['app']['port']}/health"):
        url = f"http://127.0.0.1:{st['app']['port']}/"
        print(f"Atelier 3D est déjà lancé : {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    env = {"PYTHONUTF8": "1", "ATELIER3D_HOME": str(cfg.home), "PYTHONPATH": str(cfg.dist / "app" / "backend")}
    port = pick_port(cfg.app_port)
    st["app"] = {"port": port}
    if cfg.section("comfyui").get("enabled"):
        rec = start_comfy(cfg, st)
        print(f"ComfyUI dédié : démarrage sur {rec['url']} (journal : {rec['log']})")
    if not owned_alive(st.get("worker")):
        st["worker"] = spawn([py, "-m", "atelier3d.worker"], cfg.logs_dir / "worker.log", env=env)
    st["app"] = spawn([py, "-m", "atelier3d.server", "--port", str(port)], cfg.logs_dir / "server.log", env=env)
    st["app"]["port"] = port
    write_state(cfg, st)
    url = f"http://127.0.0.1:{port}/"
    t0 = time.time()
    while time.time() - t0 < wait:
        if http_ok(url + "health"):
            print(f"Atelier 3D prêt : {url}")
            if open_browser:
                webbrowser.open(url)
            return 0
        if not owned_alive(st["app"]):
            print(f"Le serveur s'est arrêté au démarrage : voir {cfg.logs_dir / 'server.log'}")
            return 2
        time.sleep(0.5)
    print(f"Le serveur ne répond pas après {wait:.0f} s : voir {cfg.logs_dir / 'server.log'}")
    return 3


def stop_rec(rec: dict | None, name: str) -> str:
    p = owned_alive(rec)
    if p is None:
        return f"{name} : non lancé (ou processus étranger ignoré)"
    kids = p.children(recursive=True)
    for k in kids + [p]:
        try:
            k.terminate()
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(kids + [p], timeout=8)
    for k in alive:
        try:
            k.kill()
        except psutil.NoSuchProcess:
            pass
    return f"{name} : arrêté (PID {rec['pid']})"


def stop() -> int:
    cfg = C.load()
    st = read_state(cfg)
    # moteurs d'abord : un ComfyUI lancé par le worker est son processus enfant et serait sinon arrêté avec lui
    for name in ("video_comfy", "comfyui", "worker", "app"):
        if name in st:
            print(stop_rec(st.get(name), name))
            st.pop(name, None)
    write_state(cfg, st)
    return 0


def status() -> int:
    cfg = C.load()
    st = read_state(cfg)
    for name in ("app", "worker", "comfyui", "video_comfy"):
        rec = st.get(name)
        alive = owned_alive(rec) is not None
        print(f"{name:12s} {'actif' if alive else 'arrêté':7s} {('port ' + str(rec.get('port'))) if rec and rec.get('port') else ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["start", "stop", "status", "restart"])
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.command == "start":
        return start(open_browser=not a.no_browser)
    if a.command == "stop":
        return stop()
    if a.command == "restart":
        stop()
        return start(open_browser=not a.no_browser)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
