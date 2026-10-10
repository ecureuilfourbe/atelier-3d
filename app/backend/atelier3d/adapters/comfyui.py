"""Client ComfyUI : routes et événements réellement documentés (/prompt, /ws, /history, /view, /upload/image,
/interrupt ciblé, /queue, /free, /object_info, /models).

Règles :
- le prompt_id (UUID) est généré et enregistré AVANT la soumission → reprise sans double soumission ;
- on ne suppose pas qu'un message « executed » arrive pour chaque node : la fin est confirmée par /history ;
- l'interruption est toujours ciblée sur NOTRE prompt_id ; on ne vide jamais la file d'un autre utilisateur.
"""
from __future__ import annotations

import json
import pathlib
import struct
import threading
import time
import uuid
from typing import Any, Callable

import httpx

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


class ComfyError(Exception):
    def __init__(self, message: str, *, kind: str = "comfy_error", details: Any = None, oom: bool = False):
        super().__init__(message)
        self.kind, self.details, self.oom = kind, details, oom


OOM_MARKERS = ("out of memory", "outofmemoryerror", "allocation on device", "cuda error: out of memory",
               "hostbuf_file_reader_read failed", "not enough memory", "cudamalloc")


def looks_oom(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in OOM_MARKERS)


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=timeout)
        self._object_info: dict | None = None

    # ------------------------------------------------------------------ santé / introspection
    def alive(self) -> bool:
        try:
            return self.http.get("/system_stats", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def system_stats(self) -> dict:
        return self.http.get("/system_stats").json()

    def object_info(self, refresh: bool = False) -> dict:
        if self._object_info is None or refresh:
            self._object_info = self.http.get("/object_info", timeout=120).json()
        return self._object_info

    def models(self, folder: str) -> list[str]:
        r = self.http.get(f"/models/{folder}")
        return r.json() if r.status_code == 200 else []

    def queue(self) -> dict:
        q = self.http.get("/queue").json()
        return {"running": [i[1] for i in q.get("queue_running", [])], "pending": [i[1] for i in q.get("queue_pending", [])]}

    def history(self, prompt_id: str) -> dict | None:
        h = self.http.get(f"/history/{prompt_id}").json()
        return h.get(prompt_id)

    # ------------------------------------------------------------------ fichiers
    def upload_image(self, path: pathlib.Path, name: str, subfolder: str = "atelier3d") -> str:
        with open(path, "rb") as f:
            r = self.http.post("/upload/image", files={"image": (name, f, "image/png")},
                               data={"type": "input", "subfolder": subfolder, "overwrite": "true"}, timeout=120)
        if r.status_code != 200:
            raise ComfyError(f"Échec de l'envoi de l'image à ComfyUI : HTTP {r.status_code} {r.text[:300]}", kind="upload")
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]

    def download(self, info: dict, dest: pathlib.Path) -> pathlib.Path:
        params = {"filename": info["filename"], "subfolder": info.get("subfolder", ""), "type": info.get("type", "output")}
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.stream("GET", "/view", params=params, timeout=300) as r:
            if r.status_code != 200:
                raise ComfyError(f"Téléchargement impossible de {info['filename']} (HTTP {r.status_code})", kind="download")
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        return dest

    # ------------------------------------------------------------------ exécution
    def submit(self, workflow: dict, prompt_id: str, client_id: str) -> dict:
        r = self.http.post("/prompt", json={"prompt": workflow, "client_id": client_id, "prompt_id": prompt_id}, timeout=120)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        if r.status_code != 200:
            raise ComfyError("ComfyUI a refusé le workflow : " + _format_prompt_error(body), kind="validation", details=body)
        if body.get("node_errors"):
            raise ComfyError("Erreurs de nodes : " + json.dumps(body["node_errors"])[:800], kind="validation", details=body)
        return body

    def interrupt_if_ours(self, prompt_id: str) -> bool:
        """Interruption ciblée (route /interrupt avec prompt_id) — sans effet si ce n'est pas notre prompt en cours."""
        q = self.queue()
        if prompt_id in q["pending"]:
            self.http.post("/queue", json={"delete": [prompt_id]})
            return True
        if prompt_id in q["running"]:
            self.http.post("/interrupt", json={"prompt_id": prompt_id})
            return True
        return False

    def free(self, unload_models: bool = True, free_memory: bool = False) -> None:
        self.http.post("/free", json={"unload_models": unload_models, "free_memory": free_memory})

    def run(self, workflow: dict, *, prompt_id: str, client_id: str, on_event: Callable[[str, dict], None] | None = None,
            should_cancel: Callable[[], bool] | None = None, timeout_s: float = 6 * 3600, submit: bool = True,
            on_preview: Callable[[bytes, str], None] | None = None) -> dict:
        """Soumet (si demandé) puis suit l'exécution ; retourne l'entrée /history du prompt."""
        on_event = on_event or (lambda k, d: None)
        should_cancel = should_cancel or (lambda: False)
        state = {"done": False, "error": None, "interrupted": False, "last_msg": time.time()}
        ws_thread = None
        if websocket is not None:
            ws_thread = threading.Thread(target=self._ws_loop, args=(client_id, prompt_id, state, on_event, on_preview), daemon=True)
            ws_thread.start()
            time.sleep(0.3)
        if submit:
            self.submit(workflow, prompt_id, client_id)
            on_event("submitted", {"prompt_id": prompt_id})
        t0 = time.time()
        cancel_sent = False
        while True:
            if should_cancel() and not cancel_sent:
                cancel_sent = True
                hit = self.interrupt_if_ours(prompt_id)
                on_event("cancel_sent", {"targeted": hit})
            h = None
            if state["done"] or time.time() - state["last_msg"] > 5:
                h = self.history(prompt_id)
            if h is not None:
                st = h.get("status", {})
                if st.get("completed") or st.get("status_str") in ("success", "error"):
                    state["done"] = True
                    break
            if state["done"] and h is None:
                # message de fin reçu mais historique pas encore écrit : on attend un peu
                time.sleep(0.3)
                h = self.history(prompt_id)
                if h is not None:
                    break
            if time.time() - t0 > timeout_s:
                raise ComfyError(f"Délai dépassé ({timeout_s:.0f} s) ; le prompt {prompt_id} reste suivi par ComfyUI", kind="timeout")
            if not state["done"] and time.time() - state["last_msg"] > 15:
                q = self.queue()
                if prompt_id not in q["running"] and prompt_id not in q["pending"] and self.history(prompt_id) is None:
                    raise ComfyError(f"Le prompt {prompt_id} n'est plus ni en file ni dans l'historique (ComfyUI redémarré ?)", kind="lost")
                state["last_msg"] = time.time()
            time.sleep(0.25)
        state["stop"] = True
        st = h.get("status", {})
        if st.get("status_str") == "error":
            msgs = st.get("messages", [])
            err = next((m[1] for m in msgs if m[0] == "execution_error"), {})
            interrupted = any(m[0] == "execution_interrupted" for m in msgs)
            if interrupted:
                raise ComfyError("Exécution interrompue", kind="interrupted", details=msgs)
            text = f"{err.get('node_type', '?')} (node {err.get('node_id', '?')}) : {err.get('exception_type', '')} {err.get('exception_message', '')}".strip()
            raise ComfyError(text, kind="execution", details=err, oom=looks_oom(text))
        if any(m[0] == "execution_interrupted" for m in st.get("messages", [])):
            raise ComfyError("Exécution interrompue", kind="interrupted")
        return h

    def _ws_loop(self, client_id, prompt_id, state, on_event, on_preview):
        url = self.base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
        try:
            ws = websocket.create_connection(url, timeout=5)
        except Exception as e:  # repli : suivi par /history
            on_event("ws_unavailable", {"error": str(e)})
            return
        ws.settimeout(1.0)
        current = None
        last_preview = 0.0
        try:
            while not state.get("stop"):
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    break
                state["last_msg"] = time.time()
                if isinstance(msg, bytes):
                    if on_preview and current == prompt_id and len(msg) > 8:
                        etype = struct.unpack(">I", msg[:4])[0]
                        now = time.time()
                        if now - last_preview < 0.8:
                            continue
                        if etype == 1:
                            fmt = struct.unpack(">I", msg[4:8])[0]
                            on_preview(msg[8:], "png" if fmt == 2 else "jpeg")
                            last_preview = now
                        elif etype == 4:  # PREVIEW_IMAGE_WITH_METADATA
                            mlen = struct.unpack(">I", msg[4:8])[0]
                            meta = json.loads(msg[8:8 + mlen].decode("utf-8", "replace"))
                            if meta.get("prompt_id") in (None, prompt_id):
                                on_preview(msg[8 + mlen:], "png" if "png" in str(meta.get("image_type", "")) else "jpeg")
                                last_preview = now
                    continue
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                t, d = m.get("type"), m.get("data") or {}
                pid = d.get("prompt_id")
                if t == "execution_start":
                    current = pid
                if pid is not None and pid != prompt_id:
                    continue
                if t in ("execution_start", "execution_cached", "executing", "progress", "executed", "execution_success",
                         "execution_error", "execution_interrupted", "progress_state"):
                    on_event(t, d)
                if t in ("execution_success", "execution_error", "execution_interrupted") and pid == prompt_id:
                    state["done"] = True
                if t == "executing" and d.get("node") is None and pid == prompt_id:
                    state["done"] = True
        finally:
            try:
                ws.close()
            except Exception:
                pass


def _format_prompt_error(body: dict) -> str:
    err = body.get("error") or {}
    parts = [err.get("message", "") + (": " + err.get("details", "") if err.get("details") else "")]
    for nid, ne in (body.get("node_errors") or {}).items():
        for e in ne.get("errors", []):
            parts.append(f"node {nid} ({ne.get('class_type')}): {e.get('message')} {e.get('details', '')}")
    return " | ".join(p for p in parts if p)[:1500]


def new_prompt_id() -> str:
    return str(uuid.uuid4())


def history_files(h: dict, node_id: str, kind: str) -> list[dict]:
    out = (h.get("outputs") or {}).get(node_id) or {}
    if kind == "images":
        return list(out.get("images") or [])
    if kind == "3d":
        files = []
        for k in ("3d", "result", "model_3d", "files"):
            v = out.get(k)
            if isinstance(v, list):
                files += [x for x in v if isinstance(x, dict) and "filename" in x]
        return files
    if kind == "video":
        files = []
        for k in ("gifs", "videos", "images", "animated"):
            for x in out.get(k) or []:
                if isinstance(x, dict) and str(x.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                    files.append(x)
        return files
    return []


def history_text(h: dict, node_id: str) -> str | None:
    out = (h.get("outputs") or {}).get(node_id) or {}
    t = out.get("text")
    if isinstance(t, list):
        return "\n".join(str(x) for x in t)
    return t if isinstance(t, str) else None
