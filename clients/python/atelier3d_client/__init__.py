"""Client Python de l'API locale Atelier 3D (/api/v1).

Découverte automatique : URL depuis <home>/run/state.json, jeton depuis <home>/config/api_token
(<home> = ATELIER3D_HOME ou install-location.json). Un timeout côté client ne supprime jamais une tâche.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any, Iterator

import httpx

__all__ = ["Atelier3D", "Atelier3DError", "discover"]


class Atelier3DError(Exception):
    def __init__(self, status: int, payload: Any):
        self.status, self.payload = status, payload
        msg = payload.get("error", payload) if isinstance(payload, dict) else payload
        super().__init__(f"HTTP {status} : {json.dumps(msg, ensure_ascii=False)[:1500]}")


def discover() -> tuple[str, str]:
    home = os.environ.get("ATELIER3D_HOME")
    if not home:
        for cand in [pathlib.Path(__file__).resolve().parents[3] / "install-location.json",
                     pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Atelier3D" / "install-location.json"]:
            if cand.is_file():
                home = json.loads(cand.read_text(encoding="utf-8-sig"))["home"]
                break
    if not home:
        raise RuntimeError("Installation introuvable : définissez ATELIER3D_HOME ou ATELIER3D_URL/ATELIER3D_TOKEN")
    h = pathlib.Path(home)
    url = os.environ.get("ATELIER3D_URL")
    if not url:
        try:
            st = json.loads((h / "run" / "state.json").read_text(encoding="utf-8"))
            url = f"http://127.0.0.1:{st['app']['port']}"
        except Exception:
            url = "http://127.0.0.1:8765"
    token = os.environ.get("ATELIER3D_TOKEN") or (h / "config" / "api_token").read_text(encoding="utf-8").strip()
    return url, token


class Atelier3D:
    def __init__(self, url: str | None = None, token: str | None = None, author: str = "utilisateur", timeout: float = 60):
        if url is None or token is None:
            u, t = discover()
            url, token = url or u, token or t
        self.url = url.rstrip("/")
        self.http = httpx.Client(base_url=self.url, timeout=timeout,
                                 headers={"Authorization": f"Bearer {token}", "X-Atelier3D-Author": author})

    # ------------------------------------------------------------------ bas niveau
    def _req(self, method: str, path: str, **kw) -> Any:
        r = self.http.request(method, path, **kw)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        if r.status_code >= 400:
            raise Atelier3DError(r.status_code, data)
        return data

    def get(self, path, **kw):
        return self._req("GET", path, **kw)

    def post(self, path, json_body=None, **kw):
        return self._req("POST", path, json=json_body if json_body is not None else {}, **kw)

    # ------------------------------------------------------------------ général
    def health(self):
        return self.get("/health")

    def capabilities(self, refresh=False):
        return self.get("/api/v1/capabilities", params={"refresh": refresh})

    def node_types(self):
        return self.get("/api/v1/node-types")

    def graph_schema(self):
        return self.get("/api/v1/schemas/graph")

    # ------------------------------------------------------------------ imports / tâches
    def upload(self, path: str | pathlib.Path) -> dict:
        p = pathlib.Path(path)
        with open(p, "rb") as f:
            return self._req("POST", "/api/v1/uploads", files={"file": (p.name, f)})

    def create_job(self, kind: str, params: dict | None = None, upload_id: str | None = None, idempotency_key: str | None = None,
                   graph_id: str | None = None) -> dict:
        body = {"kind": kind, "params": params or {}}
        if upload_id:
            body["upload_id"] = upload_id
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        if graph_id:
            body["graph_id"] = graph_id
        return self.post("/api/v1/jobs", body)

    def job(self, job_id: str) -> dict:
        return self.get(f"/api/v1/jobs/{job_id}")

    def jobs(self, **filters) -> list[dict]:
        return self.get("/api/v1/jobs", params=filters)["jobs"]

    def cancel(self, job_id: str) -> dict:
        return self.post(f"/api/v1/jobs/{job_id}/cancel")

    def resume(self, job_id: str) -> dict:
        return self.post(f"/api/v1/jobs/{job_id}/resume")

    def rerun(self, job_id: str, **body) -> dict:
        return self.post(f"/api/v1/jobs/{job_id}/rerun", body)

    def media(self, kind: str = "all", q: str = "", graph_id: str | None = None, limit: int = 60, offset: int = 0) -> dict:
        """Médiathèque : images, vidéos et modèles 3D produits (kind = all | image | video | mesh), du plus récent au plus ancien."""
        params = {"kind": kind, "q": q, "limit": limit, "offset": offset}
        if graph_id:
            params["graph_id"] = graph_id
        return self.get("/api/v1/media", params=params)

    def artifacts(self, job_id: str) -> list[dict]:
        return self.get(f"/api/v1/jobs/{job_id}/artifacts")["artifacts"]

    def validation(self, job_id: str) -> dict:
        return self.get(f"/api/v1/jobs/{job_id}/validation")

    def open_in_blender(self, job_id: str, artifact_id: str | None = None) -> dict:
        return self.post(f"/api/v1/jobs/{job_id}/open-in-blender", {"artifact_id": artifact_id} if artifact_id else {})

    def download(self, artifact_id: str, dest: str | pathlib.Path) -> dict:
        dest = pathlib.Path(dest)
        # un dossier (existant ou non : chemin sans extension) reçoit le fichier sous son nom d'origine
        if dest.is_dir() or (not dest.exists() and not dest.suffix):
            dest.mkdir(parents=True, exist_ok=True)
            meta = self.get(f"/api/v1/artifacts/{artifact_id}")
            dest = dest / pathlib.Path(meta["path"]).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        import hashlib

        h = hashlib.sha256()
        with self.http.stream("GET", f"/api/v1/artifacts/{artifact_id}/download") as r:
            if r.status_code >= 400:
                raise Atelier3DError(r.status_code, r.read().decode("utf-8", "replace"))
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
        return {"path": str(dest.resolve()), "size": dest.stat().st_size, "sha256": h.hexdigest()}

    def events(self, after: int = 0, **filters) -> dict:
        return self.get("/api/v1/events", params={"after": after, **filters})

    def watch(self, job_id: str | None = None, *, graph_id: str | None = None, run_id: str | None = None, after: int = 0,
              timeout: float | None = None) -> Iterator[dict]:
        """Flux SSE (reconnexion automatique avec Last-Event-ID, rejoue les événements manqués)."""
        last = after
        t0 = time.time()
        params = {k: v for k, v in (("job_id", job_id), ("graph_id", graph_id), ("run_id", run_id)) if v}
        while timeout is None or time.time() - t0 < timeout:
            try:
                with self.http.stream("GET", "/api/v1/events", params={**params, "after": last},
                                      headers={"Accept": "text/event-stream", "Last-Event-ID": str(last)}, timeout=None) as r:
                    data = None
                    for line in r.iter_lines():
                        if timeout is not None and time.time() - t0 > timeout:
                            return
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                        elif line == "" and data is not None:
                            last = data["id"]
                            yield data
                            data = None
            except httpx.HTTPError:
                time.sleep(1)

    def wait(self, job_id: str, timeout: float = 3600, poll: float = 2.0, on_update=None) -> dict:
        """Attend la fin d'une tâche. Un timeout lève TimeoutError SANS annuler la tâche côté serveur."""
        t0 = time.time()
        last_sig = None
        while True:
            j = self.job(job_id)
            sig = (j.get("status"), j.get("summary"), j.get("stage"))
            if on_update and sig != last_sig:
                on_update(j)
                last_sig = sig
            if j["status"] in ("succeeded", "failed", "cancelled", "partial", "blocked"):
                return j
            if time.time() - t0 > timeout:
                raise TimeoutError(f"Tâche {job_id} toujours « {j['status']} » après {timeout:.0f} s (elle continue côté serveur)")
            time.sleep(poll)

    # ------------------------------------------------------------------ graphes
    def graphs(self, archived: bool = False) -> list[dict]:
        return self.get("/api/v1/graphs", params={"archived": str(archived).lower()})["graphs"]

    def delete_graph(self, graph_id: str) -> dict:
        """Suppression réversible (corbeille) ; refusée si des étapes de ce graphe sont en cours."""
        return self._req("DELETE", f"/api/v1/graphs/{graph_id}")

    def restore_graph(self, graph_id: str) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/restore")

    def create_graph(self, doc: dict) -> dict:
        return self.post("/api/v1/graphs", {"graph": doc})

    def create_from_preset(self, preset: str, **opts) -> dict:
        return self.post(f"/api/v1/presets/{preset}", opts)

    def graph(self, graph_id: str, status: bool = True) -> dict:
        return self.get(f"/api/v1/graphs/{graph_id}", params={"status": status})

    def patch_graph(self, graph_id: str, ops: list[dict], expected_revision: int | None = None) -> dict:
        return self._req("PATCH", f"/api/v1/graphs/{graph_id}", json={"ops": ops, "expected_revision": expected_revision})

    def plan(self, graph_id: str, **body) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/plan", body)

    def run_graph(self, graph_id: str, **body) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/runs", body)

    def rerun_node(self, graph_id: str, node_id: str, **body) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/rerun", body)

    def variants(self, graph_id: str, node_id: str, **params) -> dict:
        return self.get(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/variants", params=params)

    def select_variant(self, graph_id: str, node_id: str, variant_id: str, cascade: bool = False, pin_edges: list[str] | None = None) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/select-variant",
                         {"variant_id": variant_id, "cascade": cascade, **({"pin_edges": pin_edges} if pin_edges else {})})

    def items(self, graph_id: str, node_id: str) -> list[dict]:
        return self.get(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/items")["items"]

    def item_by_key(self, graph_id: str, node_id: str, item_key: str) -> dict:
        for it in self.items(graph_id, node_id):
            if it["item_key"] == item_key:
                return it
        raise KeyError(item_key)

    def rerun_item(self, graph_id: str, node_id: str, item_id: str, **body) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/rerun", body)

    def cancel_item(self, graph_id: str, node_id: str, item_id: str) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/cancel")

    def resume_item(self, graph_id: str, node_id: str, item_id: str) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/resume")

    def item_variants(self, graph_id: str, node_id: str, item_id: str) -> dict:
        return self.get(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/variants")

    def select_item_variant(self, graph_id: str, node_id: str, item_id: str, variant_id: str) -> dict:
        return self.post(f"/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/select-variant", {"variant_id": variant_id})

    def run_json(self, run_id: str) -> dict:
        return self.get(f"/api/v1/runs/{run_id}/run.json")

    def wait_run(self, graph_id: str, run_id: str, timeout: float = 7200, poll: float = 3.0, on_update=None) -> dict:
        t0 = time.time()
        while True:
            r = self.get(f"/api/v1/runs/{run_id}")
            if on_update:
                on_update(r)
            if r["status"] in ("succeeded", "failed", "partial", "cancelled"):
                return r
            if time.time() - t0 > timeout:
                raise TimeoutError(f"Run {run_id} toujours « {r['status']} » (il continue côté serveur)")
            time.sleep(poll)
