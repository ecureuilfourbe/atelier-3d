"""Serveur API /api/v1 d'Atelier 3D (FastAPI) + frontend statique, même origine, 127.0.0.1 par défaut.

Sécurité locale : jeton (Authorization: Bearer) pour les clients (CLI, MCP) ; cookie de session SameSite=Strict
posé par la page pour le navigateur ; contrôle Host (anti DNS-rebinding) et Origin sur les requêtes modifiantes.
Accès à distance (facultatif, VPN WireGuard de la Freebox) : voir access.py — réseaux autorisés + code d'accès.
Aucune route n'exécute de commande ou de code arbitraire.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import hashlib
import hmac
import ipaddress
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, access, capabilities, config as C, presets
from .db import get_db, loads, now
from .events import emit, last_id, since
from .graph import nodetypes as NT
from .graph.model import GraphError, GraphStore, edges_from, json_schema, node_map, topo_order, validate
from .graph.planner import SCOPES, Planner
from .graph.state import StateStore
from .jobs import JobError, JobService
from .storage import IMAGE_EXT, MESH_EXT, VIDEO_EXT, AUDIO_EXT, Storage

CFG = C.load()
DB = get_db(CFG.db_path)
STORAGE = Storage(DB, CFG.data_dir)
GRAPHS = GraphStore(DB)
STATE = StateStore(DB)
JOBS = JobService(DB)
PLANNER = Planner(DB, GRAPHS, STATE, capabilities_fn=lambda: capabilities.flat(CFG, DB),
                  aliases_fn=lambda: CFG.get("workflows.aliases", {}) or {})
TOKEN = (CFG.config_dir / "api_token").read_text(encoding="utf-8").strip() if (CFG.config_dir / "api_token").is_file() else secrets.token_hex(32)
# session du navigateur dérivée du jeton local persistant : un onglet ouvert reste valide après STOP/START
# (la valeur du cookie n'est jamais le jeton lui-même)
SESSION = hmac.new(TOKEN.encode(), b"atelier3d-browser-session-v1", hashlib.sha256).hexdigest()[:48]
MAX_UPLOAD = 1024 * 1024 * 1024

app = FastAPI(title="Atelier 3D API", version=__version__,
              description="API locale du studio Atelier 3D. Les routes /api/v1 sont le contrat du studio (pas des routes ComfyUI).")


def author_of(request: Request) -> str:
    a = request.headers.get("X-Atelier3D-Author", "").strip().lower()
    if a in ("claude", "claude code", "claude-code", "agent"):
        return "Claude Code"
    if request.headers.get("Authorization", "").startswith("Bearer ") and not a:
        return "client API"
    return "utilisateur"


LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
LOGIN_LIMITER = access.LoginLimiter()


def _remote_conf() -> dict:
    return access.remote_conf(CFG.config_dir / "atelier3d.json")


def is_remote(request: Request) -> bool:
    return not access.is_loopback(request.client.host if request.client else "")


@app.middleware("http")
async def security(request: Request, call_next):
    ip = request.client.host if request.client else ""
    conf = _remote_conf()
    action, detail = access.decide(
        client_ip=ip, host=request.headers.get("host") or "", path=request.url.path, method=request.method,
        origin=request.headers.get("origin"), authorization=request.headers.get("authorization", ""), cookies=request.cookies,
        token=TOKEN, local_session=SESSION, conf=conf, code=access.access_code(CFG.config_dir) if not access.is_loopback(ip) else "",
        local_hosts=LOCAL_HOSTS | (set(CFG.section("security").get("lan_hosts", [])) if CFG.section("security").get("lan_mode") else set()))
    if action == "ok":
        return await call_next(request)
    if action == "login":
        return RedirectResponse("/remote/login", status_code=303)
    status = 401 if action == "unauthorized" else 403
    if not request.url.path.startswith("/api/") and action == "ip":
        return PlainTextResponse(f"Atelier 3D : {detail}.", status_code=403)
    return JSONResponse({"error": {"code": action, "message": detail}}, status_code=status)


@app.exception_handler(GraphError)
async def graph_error(_, e: GraphError):
    return JSONResponse({"error": e.to_json()}, status_code=e.status)


@app.exception_handler(JobError)
async def job_error(_, e: JobError):
    return JSONResponse({"error": e.to_json()}, status_code=e.status)


# ============================================================================ santé / capacités

@app.get("/health")
def health():
    from . import launcher

    st = launcher.read_state(CFG)
    worker_alive = launcher.owned_alive(st.get("worker")) is not None
    return {"status": "ok", "version": __version__, "time": now(), "worker": "actif" if worker_alive else "arrêté",
            "events_last_id": last_id(DB)}


@app.get("/api/v1/capabilities")
def get_capabilities(refresh: bool = False):
    return capabilities.compute(CFG, DB, refresh=refresh)


@app.get("/api/v1/node-types")
def node_types():
    caps = capabilities.flat(CFG, DB)
    out = []
    for nt in NT.catalog():
        steps = []
        try:
            for st in NT.get(nt["type"]).step_list({"type": nt["type"], "params": NT.defaults_for(NT.get(nt["type"]))}):
                steps.append({"id": st.id, "label": st.label, "capability": st.capability, "resource": st.resource, "generative": st.generative,
                              "status": (caps.get(st.capability) or {}).get("status", "disponible" if st.capability == "local" else "inconnu")})
        except Exception:
            pass
        out.append({**nt, "default_steps": steps})
    return {"node_types": out, "port_types": NT.PORT_TYPES, "compatibility": {k: sorted(v) for k, v in NT.COMPATIBLE.items()}}


@app.get("/api/v1/schemas/graph")
def graph_schema():
    return json_schema()


@app.get("/api/v1/presets")
def list_presets():
    return {"presets": presets.PRESETS}


@app.post("/api/v1/presets/{preset}")
def create_from_preset(preset: str, request: Request, body: dict = Body(default={})):
    try:
        doc = presets.build(preset, body or {})
    except KeyError as e:
        raise HTTPException(404, str(e))
    g = GRAPHS.create(doc, author=author_of(request))
    return {"graph": g}


# ============================================================================ imports

def _check_allowed(path: pathlib.Path, request: Request | None = None) -> pathlib.Path:
    if request is not None and is_remote(request):
        raise HTTPException(403, "Import par chemin disque réservé au PC lui-même : envoyez le fichier depuis cet appareil")
    p = path.resolve()
    for root in CFG.allowed_roots():
        try:
            p.relative_to(pathlib.Path(root).resolve())
            return p
        except ValueError:
            continue
    raise HTTPException(403, f"Chemin hors des dossiers autorisés : {', '.join(map(str, CFG.allowed_roots()))}")


def _validate_upload(path: pathlib.Path, filename: str) -> dict:
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in IMAGE_EXT | MESH_EXT | VIDEO_EXT | AUDIO_EXT:
        raise HTTPException(415, f"Format non pris en charge : {ext}")
    meta: dict[str, Any] = {}
    if ext in IMAGE_EXT:
        from PIL import Image

        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                meta = {"width": im.width, "height": im.height, "mode": im.mode}
        except Exception as e:
            raise HTTPException(422, f"Image illisible : {e}")
    elif ext == ".glb":
        with open(path, "rb") as f:
            if f.read(4) != b"glTF":
                raise HTTPException(422, "Fichier GLB invalide (en-tête glTF absent)")
    return meta


@app.post("/api/v1/uploads")
async def upload(request: Request, file: UploadFile | None = File(default=None), path: str | None = Form(default=None)):
    """Import multipart (navigateur, client) ou par chemin local d'un dossier autorisé (MCP)."""
    if file is not None:
        filename = pathlib.Path(file.filename or "fichier").name
        tmp = pathlib.Path(tempfile.mkdtemp(dir=CFG.data_dir)) / ("in" + pathlib.Path(filename).suffix.lower())
        size = 0
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "Fichier trop volumineux (1 Gio max)")
                out.write(chunk)
        meta = _validate_upload(tmp, filename)
        rec = STORAGE.add_upload(tmp, filename, meta=meta, move=True)
        shutil.rmtree(tmp.parent, ignore_errors=True)
    elif path:
        src = _check_allowed(pathlib.Path(path), request)
        if not src.is_file():
            raise HTTPException(404, "Fichier introuvable")
        meta = _validate_upload(src, src.name)
        rec = STORAGE.add_upload(src, src.name, meta=meta)
    else:
        raise HTTPException(400, "Fournir un fichier (multipart « file ») ou un chemin (« path »)")
    emit(DB, "upload_created", upload_id=rec["id"], filename=rec["filename"], author=author_of(request))
    return rec


@app.post("/api/v1/uploads/from-artifact")
def upload_from_artifact(request: Request, body: dict = Body(...)):
    """Importe un résultat de la médiathèque (image, vidéo ou modèle 3D produit) comme fichier source d'une node « Importer » :
    copie de l'original, provenance conservée (graphe, node, item, tâche, variante). Le même résultat déjà importé est
    réutilisé tel quel (pas de doublon)."""
    aid = str(body.get("artifact_id") or "")
    a = STORAGE.get_artifact(aid)
    if not a:
        raise HTTPException(404, "Résultat introuvable")
    if a["kind"] not in ("image", "mesh", "video"):
        raise HTTPException(415, "Seules les images, les vidéos et les modèles 3D peuvent alimenter une node Importer")
    src = STORAGE.abs(a["path"])
    if not src.is_file():
        raise HTTPException(404, "Fichier du résultat absent du disque")
    prev = DB.one("SELECT id FROM uploads WHERE json_extract(meta, '$.from_artifact') = ? ORDER BY created_at DESC LIMIT 1", (aid,))
    if prev:
        u = STORAGE.get_upload(prev["id"])
        if u and STORAGE.abs(u["path"]).is_file():
            return {**u, "reused": True}
    job = DB.one("SELECT graph_id, node_id, item_key, step FROM jobs WHERE id = ?", (a.get("job_id"),)) if a.get("job_id") else None
    v = DB.one("SELECT id FROM variants WHERE job_id = ? ORDER BY created_at LIMIT 1", (a.get("job_id"),)) if a.get("job_id") else None
    meta = _validate_upload(src, src.name)
    meta.update({"from_artifact": aid, "source": {
        "graph_id": job["graph_id"] if job else None, "node_id": job["node_id"] if job else None, "item_key": job["item_key"] if job else None,
        "step": job["step"] if job else None, "job_id": a.get("job_id"), "variant_id": v["id"] if v else None, "sha256": a["sha256"]}})
    stem = "_".join(x for x in ((job["node_id"] if job else None), (job["item_key"] if job else None), src.stem) if x)
    rec = STORAGE.add_upload(src, f"{stem}{src.suffix.lower()}", meta=meta)
    emit(DB, "upload_created", upload_id=rec["id"], filename=rec["filename"], author=author_of(request), from_artifact=aid)
    return rec


@app.get("/api/v1/uploads/{upload_id}")
def get_upload(upload_id: str):
    u = STORAGE.get_upload(upload_id)
    if not u:
        raise HTTPException(404, "Import introuvable")
    vid = ((u.get("meta") or {}).get("source") or {}).get("variant_id")
    if vid:  # importé depuis la médiathèque : rendu ou miniature du résultat d'origine (comparaison de modèles 3D, vidéos)
        v = DB.one("SELECT outputs FROM variants WHERE id = ?", (vid,))
        outs = loads(v["outputs"], {}) if v else {}
        prev = (outs.get("previews") or outs.get("frames") or [None])[0]
        if prev:
            u = {**u, "preview_artifact_id": prev}
    return u


@app.get("/api/v1/uploads/{upload_id}/download")
def download_upload(upload_id: str):
    u = STORAGE.get_upload(upload_id)
    if not u:
        raise HTTPException(404, "Import introuvable")
    return FileResponse(STORAGE.abs(u["path"]), filename=u["filename"])


# ============================================================================ tâches

JOB_KINDS = ("image_to_mesh", "prepare_mesh", "rig_existing_mesh", "full_pipeline", "image_generate", "video_generate", "graph_run")


@app.post("/api/v1/jobs")
def create_job(request: Request, body: dict = Body(...)):
    kind = body.get("kind")
    if kind not in JOB_KINDS:
        raise HTTPException(422, f"kind doit être l'un de : {', '.join(JOB_KINDS)}")
    author = author_of(request)

    def _do():
        params = body.get("params") or {}
        if kind == "graph_run":
            gid = body.get("graph_id") or params.get("graph_id")
            run = PLANNER.create_run(gid, scope=params.get("scope", "graph"), node_id=params.get("node_id"), force=bool(params.get("force_recompute")),
                                     activate=params.get("activate"), author=author, expected_revision=params.get("expected_graph_revision"),
                                     jobs_service=JOBS)
        else:
            if body.get("upload_id"):
                params["upload_id"] = body["upload_id"]
            doc, opts = presets.build_for_job(kind, params)
            g = GRAPHS.create(doc, author=author)
            gid = g["id"]
            run = PLANNER.create_run(gid, scope=opts["scope"], author=author, force=bool(params.get("force_recompute")), jobs_service=JOBS)
        parent = JOBS.create_parent(kind, run_id=run.get("run_id"), graph_id=gid, payload={"request": body, "plan": {
            "executions": len(run.get("jobs", [])), "foreach": run.get("foreach")}}, author=author)
        return {"job_id": parent["id"], "graph_id": gid, "run_id": run.get("run_id"), "status": "queued" if run.get("run_id") else "succeeded",
                "steps": run.get("jobs", []), "message": run.get("message")}

    return JOBS.idempotent(body.get("idempotency_key"), "POST /api/v1/jobs", {k: v for k, v in body.items() if k != "idempotency_key"}, _do)


def job_view(j: dict) -> dict:
    j = JOBS.aggregate(j)
    view = {k: j.get(k) for k in ("id", "kind", "status", "graph_id", "run_id", "node_id", "node_type", "step", "item_id", "item_key", "stage",
                                    "progress", "error", "result", "author", "created_at", "started_at", "finished_at", "attempt",
                                    "cancel_requested", "engine_ref", "children", "counts", "summary", "current", "depends_on")}
    view["origin"] = "Claude Code" if j.get("author") == "Claude Code" else j.get("author")
    if j.get("started_at"):
        try:
            import datetime as dt

            start = dt.datetime.fromisoformat(j["started_at"])
            end = dt.datetime.fromisoformat(j["finished_at"]) if j.get("finished_at") else dt.datetime.now(dt.timezone.utc)
            view["elapsed_s"] = round((end - start).total_seconds(), 1)
        except Exception:
            pass
    if j.get("run_id") and j["kind"] != "graph_step":
        view["final_status"] = _final_status(j)
    return view


def _final_status(j: dict) -> dict:
    """Statut final lisible : « export Blender vérifié », « rig à corriger » ou « échec », avec preuves."""
    if j["status"] in ("queued", "running"):
        return {"label": "en cours", "evidence": []}
    kids = j.get("children") or []
    exp = [k for k in kids if k["step"] == "export"]
    evidence = []
    label = "échec" if j["status"] in ("failed", "partial", "cancelled", "interrupted") else "terminé"
    for k in exp:
        rs = (k.get("result") or {}).get("report_summary") or {}
        if k["status"] == "succeeded":
            label = rs.get("status") or "export Blender vérifié"
            evidence.append({"job_id": k["job_id"], "variant_id": (k.get("result") or {}).get("variant_id")})
    for k in kids:
        if k["step"] == "deform_tests" and k["status"] == "succeeded":
            v = ((k.get("result") or {}).get("report_summary") or {}).get("verdict")
            if v == "rig à corriger":
                label = "rig à corriger" if label != "échec" else label
            evidence.append({"job_id": k["job_id"], "verdict": v})
    return {"label": label, "evidence": evidence}


def _waiting_reasons(jobs: list[dict]) -> None:
    """Pour chaque étape en attente : ce qu'elle attend (prérequis du run, ou étape qui occupe la même file GPU / réseau / CPU),
    affiché dans la node (« en attente du GPU : … depuis … »)."""
    queued = [j for j in jobs if j.get("status") == "queued" and j.get("kind") == "graph_step"]
    if not queued:
        return
    names = {r["id"]: r["name"] for r in DB.all("SELECT id, name FROM graphs")}
    busy = {}
    for r in DB.all("SELECT id, resource, graph_id, node_id, node_type, started_at FROM jobs WHERE status = 'running' AND kind = 'graph_step'"):
        busy.setdefault(r["resource"], dict(r))
    for j in queued:
        deps = j.get("depends_on") or []
        deps = [d for d in (loads(deps, []) if isinstance(deps, str) else deps) if d]
        pending = [r["node_id"] for r in DB.all(f"SELECT node_id, status FROM jobs WHERE id IN ({','.join('?' * len(deps))})", tuple(deps))
                   if r["status"] != "succeeded"] if deps else []
        res = j.get("resource") or DB.one("SELECT resource FROM jobs WHERE id = ?", (j["id"],))["resource"]
        if pending:
            j["waiting_for"] = {"kind": "prerequisite", "nodes": pending}
        elif res in busy and busy[res]["id"] != j["id"]:
            b = busy[res]
            j["waiting_for"] = {"kind": "resource", "resource": res, "job_id": b["id"], "node_id": b["node_id"], "node_type": b["node_type"],
                                "graph_id": b["graph_id"], "graph_name": names.get(b["graph_id"]), "since": b["started_at"]}


@app.get("/api/v1/jobs")
def list_jobs(status: str | None = None, graph_id: str | None = None, kind: str | None = None, limit: int = 100, all: bool = False):
    views = [job_view(j) for j in JOBS.list(status=status, graph_id=graph_id, kind=kind, limit=limit, parents_only=not all)]
    _waiting_reasons(views)
    return {"jobs": views}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    return job_view(JOBS.get(job_id))


@app.get("/api/v1/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = 0):
    j = JOBS.get(job_id)
    ids = [job_id] + ([c["id"] for c in JOBS.run_children(j["run_id"])] if j.get("run_id") and j["kind"] != "graph_step" else [])
    if "text/event-stream" in request.headers.get("accept", ""):
        return _sse(request, after, run_id=j.get("run_id") if j["kind"] != "graph_step" else None, job_id=job_id if j["kind"] == "graph_step" else None)
    evs = []
    for i in ids:
        evs += since(DB, after, job_id=i, limit=2000)
    if j.get("run_id") and j["kind"] != "graph_step":
        evs += since(DB, after, run_id=j["run_id"], limit=2000)
    uniq = {e["id"]: e for e in evs}
    return {"events": [uniq[k] for k in sorted(uniq)], "last_id": max(uniq) if uniq else after}


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    return JOBS.request_cancel(job_id, author=author_of(request))


@app.post("/api/v1/jobs/{job_id}/resume")
def resume_job(job_id: str, request: Request):
    return JOBS.resume(job_id, author=author_of(request))


@app.post("/api/v1/jobs/{job_id}/rerun")
def rerun_job(job_id: str, request: Request, body: dict = Body(default={})):
    j = JOBS.get(job_id)
    if j["kind"] == "graph_step":
        return node_rerun(j["graph_id"], j["node_id"], request, {**body, "item_id": j["item_id"] or None,
                                                                   "scope": body.get("scope", "item_only" if j["item_id"] else "node_only")})
    if not j.get("graph_id"):
        raise HTTPException(422, "Tâche sans graphe associé")
    return run_graph(j["graph_id"], request, {**body, "scope": body.get("scope", "graph"), "force_recompute": body.get("force_recompute", True)})


@app.get("/api/v1/jobs/{job_id}/artifacts")
def job_artifacts(job_id: str):
    j = JOBS.get(job_id)
    ids = [job_id]
    if j.get("run_id") and j["kind"] != "graph_step":
        ids += [c["id"] for c in JOBS.run_children(j["run_id"])]
    arts = []
    for i in ids:
        arts += STORAGE.artifacts_for_job(i)
    return {"artifacts": arts}


@app.get("/api/v1/jobs/{job_id}/validation")
def job_validation(job_id: str):
    arts = job_artifacts(job_id)["artifacts"]
    reps = [a for a in arts if a["role"] == "validation"]
    out = []
    for a in reps:
        try:
            out.append({"artifact_id": a["id"], "job_id": a["job_id"], "data": json.loads(STORAGE.abs(a["path"]).read_text(encoding="utf-8"))})
        except Exception:
            pass
    return {"reports": out}


@app.get("/api/v1/jobs/{job_id}/live-preview")
def live_preview(job_id: str):
    d = CFG.jobs_dir / job_id / "previews"
    for name in ("live.png", "live.jpg"):
        if (d / name).is_file():
            return FileResponse(d / name, headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "Aucun aperçu en direct")


@app.get("/api/v1/jobs/{job_id}/log")
def job_log(job_id: str, tail: int = 400):
    p = CFG.jobs_dir / job_id / "logs" / "technique.log"
    if not p.is_file():
        return PlainTextResponse("")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
    return PlainTextResponse("\n".join(lines))


@app.post("/api/v1/jobs/{job_id}/open-in-blender")
def open_in_blender(job_id: str, body: dict = Body(default={})):
    arts = job_artifacts(job_id)["artifacts"]
    want = body.get("artifact_id")
    cand = [a for a in arts if a["path"].endswith(".blend") and (not want or a["id"] == want)]
    if not cand:
        raise HTTPException(404, "Aucun .blend validé pour cette tâche")
    a = cand[-1]
    path = STORAGE.abs(a["path"])
    exe = CFG.get("blender.path")
    if not exe or not pathlib.Path(exe).is_file():
        raise HTTPException(409, "Blender introuvable sur cette machine")
    subprocess.Popen([exe, str(path)], creationflags=0x00000008 | 0x00000200, close_fds=True)
    return {"opened": str(path), "artifact_id": a["id"], "blender": exe}


# ============================================================================ artefacts

# ============================================================================ médiathèque
# Résultats « finaux » de chaque étape (pas les masques, comparaisons, textures ni frames intermédiaires).
MEDIA_KINDS = {
    "image": [("image.generate", "image"), ("image.edit", "image"), ("image.prepare", "image"), ("image.upscale", "image")],
    "video": [("video.generate", "video")],
    "mesh": [("mesh.generate", "mesh"), ("mesh.prepare", "mesh"), ("mesh.rig", "rigged_mesh"), ("asset.export", "export_glb")],
}
MEDIA_THUMB_ROLE = {"video": ("thumbnail",), "mesh": ("preview",)}


def _norm(s: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


@app.get("/api/v1/media")
def media(kind: str = "all", q: str = "", graph_id: str | None = None, limit: int = Query(60, ge=1, le=500), offset: int = Query(0, ge=0)):
    """Médiathèque : toutes les images, vidéos et modèles 3D produits, du plus récent au plus ancien, avec vignette,
    graphe, node, item, prompt, auteur et « utilisée en aval » (variante active de son slot)."""
    if kind != "all" and kind not in MEDIA_KINDS:
        raise HTTPException(422, f"kind inconnu : {kind} (all, image, video, mesh)")
    conds, args = [], []
    for k in MEDIA_KINDS:  # tous les types : les compteurs des onglets restent justes quel que soit le filtre
        for nt, role in MEDIA_KINDS[k]:
            conds.append("(j.node_type = ? AND a.role = ?)")
            args += [nt, role]
    sql = ("SELECT a.id, a.role, a.path, a.size, a.mime, a.created_at, a.job_id, j.node_type, j.node_id, j.step, j.item_id, j.item_key, "
           "j.graph_id, j.author, j.payload FROM artifacts a JOIN jobs j ON j.id = a.job_id WHERE (" + " OR ".join(conds) + ")")
    if graph_id:
        sql += " AND j.graph_id = ?"
        args.append(graph_id)
    rows = DB.all(sql + " ORDER BY a.created_at DESC", tuple(args))
    kind_of = {(nt, role): k for k, lst in MEDIA_KINDS.items() for nt, role in lst}
    docs: dict[str, Any] = {}
    names = {r["id"]: r["name"] + (" (supprimé)" if r["archived"] else "") for r in DB.all("SELECT id, name, archived FROM graphs")}
    tokens = [t for t in _norm(q).split() if t]
    items = []
    for r in rows:
        k = kind_of[(r["node_type"], r["role"])]
        gid = r["graph_id"]
        if gid and gid not in docs:
            try:
                docs[gid] = node_map(GRAPHS.get(gid))
            except Exception:  # noqa: BLE001 — graphe supprimé ou illisible : l'artefact reste listé
                docs[gid] = {}
        node = (docs.get(gid) or {}).get(r["node_id"]) or {}
        payload = loads(r["payload"], {})
        pr = (payload.get("inputs") or {}).get("prompt")
        prompt = pr.get("text") if isinstance(pr, dict) else None
        label = node.get("label") or NT.get(r["node_type"]).label if r["node_type"] else ""
        it = {"artifact_id": r["id"], "kind": k, "role": r["role"], "url": f"/api/v1/artifacts/{r['id']}/download",
              "name": pathlib.Path(r["path"]).name, "size": r["size"], "mime": r["mime"], "created_at": r["created_at"],
              "job_id": r["job_id"], "graph_id": gid, "graph_name": names.get(gid), "node_id": r["node_id"], "node_label": label,
              "node_type": r["node_type"], "step": r["step"], "item_id": r["item_id"] or None, "item_key": r["item_key"],
              "author": r["author"], "prompt": prompt, "seed": payload.get("seed")}
        hay = _norm(" ".join(str(x) for x in (it["graph_name"], label, it["item_key"], prompt, it["name"], r["node_type"], r["step"]) if x))
        if tokens and not all(t in hay for t in tokens):
            continue
        items.append(it)
    counts = {k: 0 for k in MEDIA_KINDS}
    for it in items:
        counts[it["kind"]] += 1
    if kind != "all":
        items = [it for it in items if it["kind"] == kind]
    total = len(items)
    page = items[offset:offset + limit]
    for it in page:  # vignette, variante et statut « utilisée » calculés seulement pour la page renvoyée
        if it["kind"] == "image":
            it["thumb_url"] = it["url"]
        else:
            th = DB.one("SELECT id, path FROM artifacts WHERE job_id = ? AND role IN (%s) ORDER BY (path LIKE '%%three_quarter%%') DESC, created_at LIMIT 1"
                        % ",".join("?" * len(MEDIA_THUMB_ROLE[it["kind"]])), (it["job_id"], *MEDIA_THUMB_ROLE[it["kind"]]))
            it["thumb_url"] = f"/api/v1/artifacts/{th['id']}/download" if th else None
        v = DB.one("SELECT id FROM variants WHERE job_id = ? ORDER BY created_at LIMIT 1", (it["job_id"],))
        it["variant_id"] = v["id"] if v else None
        s = DB.one("SELECT active_variant_id FROM slots WHERE graph_id = ? AND node_id = ? AND step = ? AND item_id = ?",
                   (it["graph_id"], it["node_id"], it["step"], it["item_id"] or "")) if it["graph_id"] else None
        it["active"] = bool(v and s and s["active_variant_id"] == v["id"])
    return {"items": page, "total": total, "counts": counts, "offset": offset, "limit": limit}


@app.get("/api/v1/artifacts/{artifact_id}")
def artifact_meta(artifact_id: str):
    a = STORAGE.get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, "Artefact introuvable")
    return a


@app.get("/api/v1/artifacts/{artifact_id}/download")
def artifact_download(artifact_id: str, inline: bool = False):
    a = STORAGE.get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, "Artefact introuvable")
    p = STORAGE.abs(a["path"])
    if inline:
        return FileResponse(p, media_type=a["mime"])
    return FileResponse(p, media_type=a["mime"], filename=p.name)


@app.post("/api/v1/artifacts/{artifact_id}/open-folder")
def artifact_open_folder(artifact_id: str):
    a = STORAGE.get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, "Artefact introuvable")
    p = STORAGE.abs(a["path"])
    subprocess.Popen(["explorer", "/select,", str(p)])
    return {"opened": str(p.parent)}


# ============================================================================ graphes

def graph_view(doc: dict, include_status: bool = True) -> dict:
    out = {"graph": doc}
    if include_status:
        try:
            out["status"] = STATE.graph_status(doc)
        except GraphError as e:
            out["status_error"] = e.to_json()
        out["validation"] = validate(doc)
        out["slots"] = _slots_view(doc)
    return out


def _slots_view(doc: dict) -> dict:
    """Variantes, sortie active et dernière tâche par node/étape/item (pour l'inspecteur)."""
    gid = doc["id"]
    out: dict[str, Any] = {}
    act = {(r["node_id"], r["step"], r["item_id"]): r["active_variant_id"] for r in DB.all("SELECT * FROM slots WHERE graph_id = ?", (gid,))}
    for r in DB.all("SELECT id, node_id, step, item_id, author, created_at, outputs, from_cache_of, seed, job_id, run_id FROM variants WHERE graph_id = ? ORDER BY created_at", (gid,)):
        key = f"{r['node_id']}|{r['step']}|{r['item_id']}"
        s = out.setdefault(key, {"node_id": r["node_id"], "step": r["step"], "item_id": r["item_id"] or None,
                                 "active_variant_id": act.get((r["node_id"], r["step"], r["item_id"])), "variants": [], "jobs": []})
        outs = loads(r["outputs"], {})
        v = {"id": r["id"], "author": r["author"], "created_at": r["created_at"], "outputs": outs,
             "from_cache_of": r["from_cache_of"], "seed": r["seed"], "job_id": r["job_id"], "run_id": r["run_id"]}
        if outs.get("prompts") and r["step"] == "generate":  # « Prompts aléatoires » : textes affichés dans la node
            v["texts"] = STATE.generated_prompts(r["id"]) or []
        s["variants"].append(v)
    queued = []
    for r in DB.all("SELECT id, node_id, step, item_id, status, stage, progress, error, author, created_at, run_id, kind, resource, depends_on "
                    "FROM jobs WHERE graph_id = ? AND kind = 'graph_step' ORDER BY created_at", (gid,)):
        key = f"{r['node_id']}|{r['step']}|{r['item_id']}"
        s = out.setdefault(key, {"node_id": r["node_id"], "step": r["step"], "item_id": r["item_id"] or None,
                                 "active_variant_id": act.get((r["node_id"], r["step"], r["item_id"])), "variants": [], "jobs": []})
        jv = {"id": r["id"], "status": r["status"], "stage": r["stage"], "progress": loads(r["progress"]), "error": loads(r["error"]),
              "author": r["author"], "created_at": r["created_at"], "run_id": r["run_id"]}
        if r["status"] == "queued":
            queued.append({**jv, "kind": r["kind"], "resource": r["resource"], "depends_on": r["depends_on"], "_view": jv})
        s["jobs"].append(jv)
    _waiting_reasons(queued)
    for q in queued:
        if q.get("waiting_for"):
            q["_view"]["waiting_for"] = q["waiting_for"]
    for s in out.values():
        s["jobs"] = s["jobs"][-10:]
    return out


@app.get("/api/v1/graphs")
def list_graphs(archived: bool = False):
    """Graphes de la liste ; archived=true : la corbeille (graphes supprimés, restaurables)."""
    return {"graphs": GRAPHS.list(archived)}


@app.delete("/api/v1/graphs/{graph_id}")
def delete_graph(graph_id: str, request: Request):
    """Supprime un graphe de la liste (corbeille, restaurable). Ses résultats restent dans la médiathèque et sur le disque.
    Refusé tant qu'une étape de ce graphe est en attente ou en cours."""
    busy = DB.one("SELECT COUNT(*) AS n FROM jobs WHERE graph_id = ? AND status IN ('queued', 'running')", (graph_id,))["n"]
    if busy:
        raise HTTPException(409, f"{busy} étape(s) de ce graphe en attente ou en cours : annulez-les ou attendez la fin avant de le supprimer")
    return GRAPHS.set_archived(graph_id, True, author=author_of(request))


@app.post("/api/v1/graphs/{graph_id}/restore")
def restore_graph(graph_id: str, request: Request):
    """Remet un graphe supprimé dans la liste, avec ses variantes et résultats."""
    return GRAPHS.set_archived(graph_id, False, author=author_of(request))


@app.post("/api/v1/graphs")
def create_graph(request: Request, body: dict = Body(...)):
    doc = body.get("graph", body)
    g = GRAPHS.create(doc, author=author_of(request))
    return graph_view(g)


@app.get("/api/v1/graphs/{graph_id}")
def get_graph(graph_id: str, status: bool = True, revision: int | None = None):
    if revision is not None:
        return {"graph": GRAPHS.get_revision(graph_id, revision)}
    return graph_view(GRAPHS.get(graph_id), include_status=status)


@app.get("/api/v1/graphs/{graph_id}/revisions")
def graph_revisions(graph_id: str):
    rows = DB.all("SELECT revision, author, reason, created_at FROM graph_revisions WHERE graph_id = ? ORDER BY revision DESC LIMIT 200", (graph_id,))
    return {"revisions": [dict(r) for r in rows]}


@app.patch("/api/v1/graphs/{graph_id}")
def patch_graph(graph_id: str, request: Request, body: dict = Body(...)):
    ops = body.get("ops")
    if ops is None and "graph" in body:
        ops = [{"op": "replace", "doc": body["graph"]}]
    if not isinstance(ops, list):
        raise HTTPException(422, "Fournir « ops » (liste d'opérations) ou « graph »")
    doc, log = GRAPHS.patch(graph_id, ops, author=author_of(request), expected_revision=body.get("expected_revision"))
    return {**graph_view(doc), "changes": log}


@app.post("/api/v1/graphs/{graph_id}/plan")
def plan_graph(graph_id: str, request: Request, body: dict = Body(default={})):
    p = PLANNER.plan(graph_id, scope=body.get("scope", "graph"), node_id=body.get("node_id"), item_id=body.get("item_id"), step=body.get("step"),
                     force=bool(body.get("force_recompute")), params_patch=body.get("params_patch"), apply=body.get("apply", True),
                     author=author_of(request), expected_revision=body.get("expected_revision"))
    p.pop("doc", None)
    return p


@app.post("/api/v1/graphs/{graph_id}/runs")
def run_graph(graph_id: str, request: Request, body: dict = Body(default={})):
    author = author_of(request)

    def _do():
        r = PLANNER.create_run(graph_id, scope=body.get("scope", "graph"), node_id=body.get("node_id"), item_id=body.get("item_id"),
                               step=body.get("step"), force=bool(body.get("force_recompute", body.get("force", False))),
                               params_patch=body.get("params_patch"), activate=body.get("activate"), author=author,
                               expected_revision=body.get("expected_graph_revision", body.get("expected_revision")), jobs_service=JOBS)
        parent = JOBS.create_parent("graph_run", run_id=r.get("run_id"), graph_id=graph_id, payload={"request": body}, author=author) if r.get("run_id") else None
        r["job_id"] = parent["id"] if parent else None
        return r

    return JOBS.idempotent(body.get("idempotency_key"), f"POST /graphs/{graph_id}/runs", {k: v for k, v in body.items() if k != "idempotency_key"}, _do)


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str):
    r = DB.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not r:
        raise HTTPException(404, "Run introuvable")
    d = dict(r)
    d["snapshot"] = loads(d["snapshot"])
    d["scope"] = loads(d["scope"])
    d["options"] = loads(d["options"])
    d["jobs"] = [job_view(j) for j in JOBS.run_children(run_id)]
    return d


@app.get("/api/v1/runs/{run_id}/run.json")
def run_json(run_id: str):
    """Snapshot effectivement exécuté : graphe figé, mapping studio/Comfy, moteurs, seeds, prompt_id, événements, artefacts."""
    r = get_run(run_id)
    steps = []
    for j in JOBS.run_children(run_id):
        full = JOBS.get(j["id"])
        steps.append({"job_id": j["id"], "node_id": j["node_id"], "step": j["step"], "item_id": j["item_id"] or None, "item_key": j["item_key"],
                      "status": j["status"], "seed": (full["payload"] or {}).get("seed"), "params": (full["payload"] or {}).get("params"),
                      "engine": (full["payload"] or {}).get("engine"), "engine_ref": full.get("engine_ref"), "result": full.get("result"),
                      "artifacts": [{"id": a["id"], "role": a["role"], "path": a["path"], "sha256": a["sha256"]} for a in STORAGE.artifacts_for_job(j["id"])]})
    return {"run_id": run_id, "graph_id": r["graph_id"], "revision": r["revision"], "graph_snapshot": r["snapshot"], "status": r["status"],
            "author": r["author"], "created_at": r["created_at"], "finished_at": r["finished_at"], "steps": steps,
            "events": since(DB, 0, run_id=run_id, limit=5000)}


def _variant_view(v: dict) -> dict:
    arts = {k: STORAGE.artifacts_by_ids(ids) for k, ids in (v.get("outputs") or {}).items()}
    return {**v, "artifacts": arts}


def _stale_set(doc) -> set[str]:
    st = STATE.graph_status(doc)
    return {nid for nid, s in st.items() if s["status"] in ("stale", "upstream", "partial")}


@app.post("/api/v1/graphs/{graph_id}/nodes/{node_id}/rerun")
def node_rerun(graph_id: str, node_id: str, request: Request, body: dict = Body(default={})):
    scope = body.get("scope", "node_only")
    if scope not in ("node_only", "node_and_descendants", "step_only", "all_items", "item_only", "item_and_descendants"):
        raise HTTPException(422, f"Portée de relance invalide : {scope}")
    author = author_of(request)

    def _do():
        r = PLANNER.create_run(graph_id, scope=scope, node_id=node_id, item_id=body.get("item_id"), step=body.get("step"),
                               force=bool(body.get("force_recompute", False)), params_patch=body.get("params_patch"),
                               activate="always" if body.get("activate_output") else None, author=author,
                               expected_revision=body.get("expected_graph_revision"), jobs_service=JOBS)
        r["job_id"] = (r["jobs"][0]["job_id"] if r.get("jobs") else None)
        return r

    return JOBS.idempotent(body.get("idempotency_key"), f"rerun {graph_id}/{node_id}", {k: v for k, v in body.items() if k != "idempotency_key"}, _do)


@app.get("/api/v1/graphs/{graph_id}/nodes/{node_id}/variants")
def node_variants(graph_id: str, node_id: str, step: str | None = None, item_id: str | None = None):
    doc = GRAPHS.get(graph_id)
    n = node_map(doc).get(node_id)
    if not n:
        raise HTTPException(404, "Node introuvable")
    vs = STATE.list_variants(graph_id, node_id, step=step, item_id=item_id)
    act = {(r["step"], r["item_id"]): r["active_variant_id"] for r in DB.all("SELECT * FROM slots WHERE graph_id = ? AND node_id = ?", (graph_id, node_id))}
    return {"node_id": node_id, "variants": [{**_variant_view(v), "active": act.get((v["step"], v["item_id"])) == v["id"]} for v in vs],
            "active": [{"step": s, "item_id": i or None, "variant_id": vid} for (s, i), vid in act.items()]}


@app.post("/api/v1/graphs/{graph_id}/nodes/{node_id}/select-variant")
def select_variant(graph_id: str, node_id: str, request: Request, body: dict = Body(...)):
    vid = body.get("variant_id")
    v = STATE.get_variant(vid)
    if not v or v["graph_id"] != graph_id or v["node_id"] != node_id:
        raise HTTPException(404, "Variante introuvable pour cette node")
    doc = GRAPHS.get(graph_id)
    before = _stale_set(doc)
    author = author_of(request)
    STATE.set_active(graph_id, node_id, v["step"], v["item_id"] or "", vid, author=author)
    restored = []
    if body.get("cascade"):
        restored = _cascade_restore(doc, author)
    if body.get("pin_edges"):
        ops = []
        for eid in body["pin_edges"]:
            ops.append({"op": "update_edge", "edge_id": eid, "link": {"mode": "pinned", "variant_id": vid}})
        doc, _ = GRAPHS.patch(graph_id, ops, author=author)
    if v["step"] == "analyze":  # « Extraction de props » : la liste de parties de cette analyse devient celle de la node
        from . import props as P

        P.apply_analysis(DB, graph_id, node_id, vid, author)
    after = _stale_set(GRAPHS.get(graph_id))
    return {"node_id": node_id, "variant_id": vid, "step": v["step"], "item_id": v["item_id"] or None,
            "newly_stale": sorted(after - before), "no_longer_stale": sorted(before - after), "restored_chain": restored}


def _cascade_restore(doc: dict, author: str) -> list[dict]:
    """Restaure une chaîne compatible : pour chaque slot devenu obsolète, sélectionne une variante existante
    dont les entrées correspondent exactement aux nouvelles entrées actives (provenance)."""
    restored = []
    nodes = node_map(doc)
    for nid in topo_order(doc):
        n = nodes[nid]
        try:
            steps = STATE.node_steps(n)
        except Exception:
            continue
        for item in STATE.item_ids(n):
            for i, st in enumerate(steps):
                s = STATE.slot_status(doc, n, i, item)
                if s["status"] == "ok":
                    continue
                for v in reversed(STATE.list_variants(doc["id"], nid, step=st.id, item_id=item)):
                    if v["input_fingerprint"] == s["expected_fingerprint"]:
                        STATE.set_active(doc["id"], nid, st.id, item, v["id"], author=author, reason="restauration de chaîne (provenance)")
                        restored.append({"node_id": nid, "step": st.id, "item_id": item or None, "variant_id": v["id"]})
                        break
    return restored


@app.get("/api/v1/graphs/{graph_id}/nodes/{node_id}/items")
def node_items(graph_id: str, node_id: str):
    doc = GRAPHS.get(graph_id)
    n = node_map(doc).get(node_id)
    if not n:
        raise HTTPException(404, "Node introuvable")
    steps = STATE.node_steps(n)
    last = steps[-1].id if steps else None
    items = []
    previews = {(x.get("auto") or {}).get("item_id"): x for x in doc["nodes"] if (x.get("auto") or {}).get("owner_node_id") == node_id}
    for it in sorted(n.get("items", []), key=lambda i: (i.get("status") != "active", i.get("order", 0))):
        vid = STATE.active_id(graph_id, node_id, last, it["item_id"]) if last else None
        va = STATE.get_variant(vid) if vid else None
        jobs = DB.all("SELECT id, status, stage, progress, error, step FROM jobs WHERE graph_id = ? AND node_id = ? AND item_id = ? ORDER BY created_at DESC LIMIT 5",
                      (graph_id, node_id, it["item_id"]))
        status = None
        if it.get("status") == "active":
            idx = len(steps) - 1
            status = STATE.slot_status(doc, n, idx, it["item_id"])["status"] if steps else None
        pv = previews.get(it["item_id"])
        all_v = STATE.list_variants(graph_id, node_id, step=last, item_id=it["item_id"]) if last else []
        latest = all_v[-1]["id"] if all_v else None
        items.append({**it, "ports": {"image": f"image:{it['item_key']}"}, "active_variant_id": vid, "status_slot": status,
                      "outputs": _variant_view(va)["artifacts"] if va else {}, "variants_count": len(all_v),
                      "latest_variant_id": latest, "candidate_not_selected": bool(latest and latest != vid),
                      "seed": (va or {}).get("seed"),
                      "preview_node_id": pv["id"] if pv else None, "jobs": [dict(j) | {"progress": loads(j["progress"]), "error": loads(j["error"])} for j in jobs]})
    return {"node_id": node_id, "mode": (n.get("execution") or {}).get("mode", "single"), "items": items}


@app.post("/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/rerun")
def item_rerun(graph_id: str, node_id: str, item_id: str, request: Request, body: dict = Body(default={})):
    scope = body.get("scope", "item_only")
    if scope not in ("item_only", "item_and_descendants"):
        raise HTTPException(422, "scope doit être item_only ou item_and_descendants")
    return node_rerun(graph_id, node_id, request, {**body, "item_id": item_id, "scope": scope})


@app.post("/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/cancel")
def item_cancel(graph_id: str, node_id: str, item_id: str, request: Request):
    rows = DB.all("SELECT id FROM jobs WHERE graph_id = ? AND node_id = ? AND item_id = ? AND status IN ('queued', 'running')", (graph_id, node_id, item_id))
    return {"cancelled": [JOBS.request_cancel(r["id"], author=author_of(request)) for r in rows]}


@app.post("/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/resume")
def item_resume(graph_id: str, node_id: str, item_id: str, request: Request):
    rows = DB.all("SELECT id FROM jobs WHERE graph_id = ? AND node_id = ? AND item_id = ? AND status IN ('failed', 'interrupted', 'blocked', 'cancelled') "
                  "ORDER BY created_at DESC", (graph_id, node_id, item_id))
    if not rows:
        raise HTTPException(409, "Aucune tâche à reprendre pour cet item")
    return JOBS.resume(rows[0]["id"], author=author_of(request))


@app.get("/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/variants")
def item_variants(graph_id: str, node_id: str, item_id: str):
    return node_variants(graph_id, node_id, item_id=item_id)


@app.post("/api/v1/graphs/{graph_id}/nodes/{node_id}/items/{item_id}/select-variant")
def item_select(graph_id: str, node_id: str, item_id: str, request: Request, body: dict = Body(...)):
    v = STATE.get_variant(body.get("variant_id", ""))
    if not v or v["item_id"] != item_id:
        raise HTTPException(404, "Variante introuvable pour cet item")
    return select_variant(graph_id, node_id, request, body)


@app.get("/api/v1/graphs/{graph_id}/status")
def graph_status(graph_id: str):
    doc = GRAPHS.get(graph_id)
    return {"revision": doc["revision"], "status": STATE.graph_status(doc), "slots": _slots_view(doc)}


# ============================================================================ workflows ComfyUI

@app.post("/api/v1/workflows/import")
def import_workflow(request: Request, body: dict = Body(...)):
    """Valide un vrai JSON ComfyUI (API ou éditeur) SANS l'exécuter."""
    from .adapters.comfyui import ComfyClient

    wf = body.get("workflow")
    if wf is None and body.get("path"):
        wf = json.loads(_check_allowed(pathlib.Path(body["path"]), request).read_text(encoding="utf-8"))
    if not isinstance(wf, dict):
        raise HTTPException(422, "Fournir « workflow » (objet JSON) ou « path »")
    target = body.get("target", "dedicated")
    from . import launcher

    st = launcher.read_state(CFG)
    if target == "dedicated" or CFG.get("video.provider") == "comfyui_dedicated":
        url = (st.get("comfyui") or {}).get("url")
    else:
        url = (st.get("video_comfy") or {}).get("url") or CFG.get("video.url")
    fmt = "api" if all(isinstance(v, dict) and "class_type" in v for v in wf.values()) else ("editor" if "nodes" in wf and "links" in wf else "inconnu")
    classes = sorted({v["class_type"] for v in wf.values()}) if fmt == "api" else sorted({n.get("type") for n in wf.get("nodes", [])})
    report = {"format": fmt, "classes": classes, "target": target, "url": url, "executable": False}
    if fmt == "editor":
        report["note"] = ("Format éditeur (nodes/links/widgets_values/subgraphs) : ce n'est PAS le JSON soumis à /prompt. "
                          "Conversion requise par l'export API officiel du frontend ComfyUI (graphToPrompt).")
        report["subgraphs"] = len((wf.get("definitions") or {}).get("subgraphs", []))
        report["bypassed_nodes"] = sum(1 for n in wf.get("nodes", []) if n.get("mode") == 4)
    if url:
        c = ComfyClient(url, timeout=10)
        if c.alive():
            oi = c.object_info()
            subgraph_ids = {sg["id"] for sg in (wf.get("definitions") or {}).get("subgraphs", [])} if fmt == "editor" else set()
            report["missing_classes"] = [x for x in classes if x not in oi and x not in subgraph_ids and x not in ("Note", "MarkdownNote", "Reroute", "PrimitiveNode")]
            report["executable"] = fmt == "api" and not report["missing_classes"]
        else:
            report["note_engine"] = "Instance cible non lancée : vérification des classes impossible"
    return report


@app.get("/api/v1/workflows")
def list_workflows():
    from .adapters.workflows import TemplateRegistry

    reg = TemplateRegistry(CFG.workflows_dir / "comfy")
    return {"aliases": CFG.get("workflows.aliases", {}), "templates": [{"id": i, "label": reg.get(i).label, "sha256": reg.get(i).sha256,
                                                                          "bindings": reg.get(i).bindings} for i in reg.ids()]}


# ============================================================================ événements

def _sse(request: Request, after: int, *, graph_id=None, job_id=None, run_id=None):
    async def gen():
        last = after
        hb = time.time()
        while True:
            if await request.is_disconnected():
                break
            evs = since(DB, last, graph_id=graph_id, job_id=job_id, run_id=run_id, limit=500)
            for e in evs:
                last = e["id"]
                yield f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
            if time.time() - hb > 10:
                hb = time.time()
                yield f": ping {last}\n\n"
            await asyncio.sleep(0.25 if not evs else 0.02)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/v1/events")
def events(request: Request, after: int = 0, graph_id: str | None = None, job_id: str | None = None, run_id: str | None = None):
    leid = request.headers.get("last-event-id")
    if leid and leid.isdigit():
        after = max(after, int(leid))
    if "text/event-stream" in request.headers.get("accept", ""):
        return _sse(request, after, graph_id=graph_id, job_id=job_id, run_id=run_id)
    evs = since(DB, after, graph_id=graph_id, job_id=job_id, run_id=run_id, limit=1000)
    return {"events": evs, "last_id": evs[-1]["id"] if evs else after}


# ============================================================================ réglages (services externes)
# La clé n'est jamais renvoyée : seulement « configurée » et ses 4 derniers caractères. Elle est chiffrée (DPAPI) sur disque.

def _openai_test(key: str) -> dict:
    from .adapters import openai_images as O
    from .adapters.base import StepFailure
    from .secrets_store import load_services, save_services

    svc = load_services(CFG.config_dir)
    o = svc["openai"]
    try:
        ids = O.list_models(key)
        models, text_models = O.image_models(ids), O.text_models(ids)
    except StepFailure as e:
        o["last_test"] = {"ok": False, "at": now(), "message": str(e)}
        save_services(CFG.config_dir, svc)
        return o["last_test"]
    except Exception as e:  # noqa: BLE001 — réseau, proxy…
        o["last_test"] = {"ok": False, "at": now(), "message": f"OpenAI injoignable : {type(e).__name__}"}
        save_services(CFG.config_dir, svc)
        return o["last_test"]
    o["models"], o["text_models"] = models, text_models
    for k, purpose in (("model_generate", "generate"), ("model_edit", "edit")):
        if not o.get(k) or o[k] not in models:
            o[k] = O.pick_default(models, purpose)
    if not o.get("model_text") or o["model_text"] not in text_models:
        o["model_text"] = O.pick_text_default(text_models)
    msg = (f"Clé valide : {len(models)} modèle(s) d'image et {len(text_models)} modèle(s) texte / vision disponible(s)" if models
           else "Clé valide, mais aucun modèle GPT Image n'est accessible à ce compte (vérification d'organisation ?)")
    o["last_test"] = {"ok": bool(models), "at": now(), "message": msg}
    save_services(CFG.config_dir, svc)
    return o["last_test"]


@app.get("/api/v1/settings")
def get_settings(request: Request):
    from .secrets_store import openai_view

    conf = _remote_conf()
    return {"openai": openai_view(CFG.config_dir),
            "client": {"remote": is_remote(request), "ip": request.client.host if request.client else None},
            "remote": {"enabled": conf["enabled"], "networks": conf["networks"]}}


def _ps(cmd: str, timeout: float = 15) -> str:
    """Commande PowerShell en LECTURE seule (diagnostic) ; aucune modification du système."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, timeout=timeout,
                           creationflags=0x08000000)
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _remote_diagnostic(conf: dict) -> dict:
    """Adresses du PC sur le réseau local, règle de pare-feu, route de retour vers le réseau VPN (ProtonVPN…)."""
    from . import launcher

    port = (launcher.read_state(CFG).get("app") or {}).get("port") or CFG.app_port
    raw = _ps("Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | "
              "ForEach-Object { [pscustomobject]@{alias=$_.InterfaceAlias; ip=$_.IPv4Address.IPAddress; gw=$_.IPv4DefaultGateway.NextHop} } | "
              "ConvertTo-Json -Compress")
    try:
        ifs = json.loads(raw) if raw else []
        ifs = [ifs] if isinstance(ifs, dict) else ifs
    except ValueError:
        ifs = []
    lan = [x for x in ifs if str(x.get("gw") or "").startswith("192.168.") and str(x.get("ip") or "").startswith("192.168.")]
    rule = _ps("Get-NetFirewallRule -DisplayName 'Atelier 3D (VPN WireGuard)' -ErrorAction SilentlyContinue | "
               "Select-Object -ExpandProperty Enabled")
    net0 = conf["networks"][0] if conf["networks"] else access.DEFAULT_NETWORKS[0]
    probe = str(next(iter(ipaddress.ip_network(net0).hosts()), "192.168.27.65"))
    route = _ps(f"Find-NetRoute -RemoteIPAddress {probe} -ErrorAction SilentlyContinue | Select-Object -Last 1 | "
                "ForEach-Object { $_.InterfaceAlias + '|' + $_.NextHop }")
    alias, _, hop = route.partition("|")
    lan_alias = {x.get("alias") for x in lan}
    return {"port": port, "urls": [f"http://{x['ip']}:{port}/" for x in lan], "interfaces": ifs,
            "listening_all": CFG.app_host != "127.0.0.1" or conf["enabled"],
            "firewall_rule": rule.strip().lower() == "true", "route": {"probe": probe, "interface": alias or None, "next_hop": hop or None,
                                                                       "ok": bool(alias) and alias in lan_alias}}


@app.get("/api/v1/settings/remote")
def get_remote_settings():
    conf = _remote_conf()
    return {**conf, "code": access.access_code(CFG.config_dir), "diagnostic": _remote_diagnostic(conf),
            "script": str(CFG.dist / "ACCES_VPN.bat")}


@app.put("/api/v1/settings/remote")
def put_remote_settings(request: Request, body: dict = Body(...)):
    path = CFG.config_dir / "atelier3d.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    sec = raw.setdefault("security", {})
    r = sec.setdefault("remote", {"enabled": False, "networks": list(access.DEFAULT_NETWORKS)})
    if "networks" in body:
        try:
            r["networks"] = access.validate_networks(body["networks"])
        except ValueError as e:
            raise HTTPException(422, str(e))
    if "enabled" in body:
        r["enabled"] = bool(body["enabled"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    emit(DB, "settings_changed", setting="remote_access", enabled=r["enabled"], networks=r["networks"], author=author_of(request))
    conf = _remote_conf()
    listening = CFG.app_host != "127.0.0.1"
    return {**conf, "restart_needed": conf["enabled"] != listening}


@app.post("/api/v1/settings/remote/code")
def new_remote_code(request: Request):
    code = access.regenerate_code(CFG.config_dir)  # change aussi la session : tous les appareils sont déconnectés
    emit(DB, "settings_changed", setting="remote_access_code", author=author_of(request))
    return {"code": code}


@app.put("/api/v1/settings/openai")
def put_openai_settings(request: Request, body: dict = Body(...)):
    """Enregistre la clé (si fournie, puis la teste) et/ou les réglages par défaut (modèles, qualité, modération)."""
    from .adapters import openai_images as O
    from .secrets_store import OPENAI_KEY, SecretStore, load_services, openai_view, save_services

    svc = load_services(CFG.config_dir)
    o = svc["openai"]
    for k, allowed in (("quality", O.QUALITIES), ("moderation", O.MODERATION)):
        if k in body:
            if body[k] not in allowed:
                raise HTTPException(422, f"{k} : valeur autorisée parmi {allowed}")
            o[k] = body[k]
    for k in ("model_generate", "model_edit"):
        if k in body:
            v = str(body[k] or "")
            if v and not v.startswith("gpt-image"):
                raise HTTPException(422, f"{k} : modèle GPT Image attendu (gpt-image-…)")
            o[k] = v
    if "model_text" in body:
        v = str(body["model_text"] or "")
        if v and not v.startswith("gpt-"):
            raise HTTPException(422, "model_text : modèle texte OpenAI attendu (gpt-…)")
        o["model_text"] = v
    save_services(CFG.config_dir, svc)
    test = None
    if body.get("api_key") is not None:
        key = str(body["api_key"]).strip()
        if not key or any(c.isspace() for c in key) or len(key) < 20:
            raise HTTPException(422, "Clé d'API invalide (vide, trop courte ou contenant des espaces)")
        SecretStore(CFG.config_dir).set(OPENAI_KEY, key)
        emit(DB, "settings_changed", setting="openai_api_key", author=author_of(request))  # sans la valeur
        test = _openai_test(key)
    capabilities.compute(CFG, DB, refresh=True)
    return {"openai": openai_view(CFG.config_dir), "test": test}


@app.post("/api/v1/settings/openai/test")
def test_openai_settings():
    from .secrets_store import OPENAI_KEY, SecretStore, openai_view

    key = SecretStore(CFG.config_dir).get(OPENAI_KEY)
    if not key:
        raise HTTPException(409, "Aucune clé OpenAI enregistrée")
    test = _openai_test(key)
    capabilities.compute(CFG, DB, refresh=True)
    return {"openai": openai_view(CFG.config_dir), "test": test}


@app.delete("/api/v1/settings/openai/key")
def delete_openai_key(request: Request):
    from .secrets_store import OPENAI_KEY, SecretStore, load_services, openai_view, save_services

    SecretStore(CFG.config_dir).delete(OPENAI_KEY)
    svc = load_services(CFG.config_dir)  # le dernier test et la liste des modèles concernaient l'ancienne clé
    svc["openai"]["last_test"], svc["openai"]["models"] = None, []
    save_services(CFG.config_dir, svc)
    emit(DB, "settings_changed", setting="openai_api_key_deleted", author=author_of(request))
    capabilities.compute(CFG, DB, refresh=True)
    return {"openai": openai_view(CFG.config_dir)}


@app.get("/api/v1/system")
def system_info():
    import psutil

    vm = psutil.virtual_memory()
    out = {"ram_total_mb": vm.total // 2**20, "ram_used_mb": (vm.total - vm.available) // 2**20}
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5, creationflags=0x08000000)
        f = [x.strip() for x in r.stdout.strip().split(",")]
        out.update({"gpu": f[0], "vram_total_mb": int(f[1]), "vram_used_mb": int(f[2]), "gpu_util": int(f[3])})
    except Exception:
        out["gpu"] = None
    return out


# ============================================================================ frontend

DIST = CFG.frontend_dist


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    p = DIST / "index.html"
    html = p.read_text(encoding="utf-8") if p.is_file() else "<h1>Atelier 3D</h1><p>Frontend non construit.</p>"
    r = HTMLResponse(html, headers={"Cache-Control": "no-store"})
    if not is_remote(request):  # à distance, la session vient du code d'accès (/remote/login)
        r.set_cookie("a3d_session", SESSION, httponly=True, samesite="strict", secure=False)
    return r


_LOGIN_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atelier 3D — connexion à distance</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#11141c;color:#e6edf3;font:15px system-ui,Segoe UI,sans-serif}
form{background:#1a1f2b;border:1px solid #2c3446;border-radius:12px;padding:24px;width:min(340px,90vw);display:grid;gap:12px}
h1{font-size:18px;margin:0}p{margin:0;color:#9aa4b2;font-size:13px}input{font:600 18px ui-monospace,Consolas,monospace;letter-spacing:2px;
text-transform:uppercase;padding:10px;border-radius:8px;border:1px solid #39435a;background:#0f131b;color:#e6edf3;text-align:center}
button{padding:10px;border:0;border-radius:8px;background:#6c7bff;color:#fff;font-weight:600;cursor:pointer}.err{color:#ff8b7d}
</style></head><body><form method="post" action="/remote/login"><h1>Atelier 3D — accès à distance</h1>
<p>Saisissez le code d'accès affiché sur le PC (⚙ Réglages → Accès à distance).</p>__ERR__
<input name="code" autocomplete="off" autocapitalize="characters" placeholder="XXXX-XXXX-XXXX" autofocus required>
<button type="submit">Se connecter</button></form></body></html>"""


@app.get("/remote/login", response_class=HTMLResponse)
def remote_login_page():
    return HTMLResponse(_LOGIN_HTML.replace("__ERR__", ""), headers={"Cache-Control": "no-store"})


@app.post("/remote/login")
async def remote_login(request: Request):
    ip = request.client.host if request.client else ""
    wait = LOGIN_LIMITER.blocked(ip)
    form = await request.form()
    if wait:
        html = _LOGIN_HTML.replace("__ERR__", f'<p class="err">Trop d\'essais : réessayez dans {int(wait // 60) + 1} min.</p>')
        return HTMLResponse(html, status_code=429)
    code = access.access_code(CFG.config_dir)
    if not access.code_matches(str(form.get("code") or ""), code):
        LOGIN_LIMITER.failure(ip)
        emit(DB, "remote_login_failed", ip=ip)
        return HTMLResponse(_LOGIN_HTML.replace("__ERR__", '<p class="err">Code incorrect.</p>'), status_code=401)
    LOGIN_LIMITER.success(ip)
    emit(DB, "remote_login", ip=ip)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(access.COOKIE, access.session_value(TOKEN, code), httponly=True, samesite="strict", secure=False,
                 max_age=access.SESSION_DAYS * 86400)
    return r


@app.post("/remote/logout")
def remote_logout():
    r = RedirectResponse("/remote/login", status_code=303)
    r.delete_cookie(access.COOKIE)
    return r


if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


def main() -> int:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=CFG.app_port)
    ap.add_argument("--host", default=None)
    a = ap.parse_args()
    if a.host is None:  # accès à distance actif : écoute IPv4 sur toutes les interfaces (le contrôle d'accès filtre)
        a.host = "0.0.0.0" if _remote_conf()["enabled"] else CFG.app_host
    CFG.raw.setdefault("app", {})["host"] = a.host
    uvicorn.run(app, host=a.host, port=a.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
