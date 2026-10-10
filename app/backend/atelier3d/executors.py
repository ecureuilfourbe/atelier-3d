"""Exécuteurs d'étapes : relient chaque capacité studio à son moteur réel.

Chaque exécuteur reçoit un StepContext (entrées figées du run, paramètres effectifs, seed) et retourne
{outputs: {port: [artifact_id]}, engine: {...}, stats: {...}, report: {...}}.
"""
from __future__ import annotations

import io
import json
import math
import pathlib
import re
import shutil
import threading
import time
from typing import Any, Callable

from PIL import Image

from . import config as C
from .adapters.base import Cancelled, StepFailure
from .adapters.blender import run_script
from .adapters.comfyui import ComfyClient, ComfyError, history_files, history_text, new_prompt_id
from .adapters.workflows import TemplateRegistry, WorkflowError, normalize_combo_paths
from .db import Database, dumps, loads
from .events import emit
from .storage import Storage


# ---------------------------------------------------------------------------- contexte
class StepContext:
    def __init__(self, *, db: Database, storage: Storage, cfg: C.Config, job: dict, templates: TemplateRegistry,
                 services: "Services"):
        self.db, self.storage, self.cfg, self.job = db, storage, cfg, job
        self.payload = job["payload"]
        self.params = self.payload.get("params") or {}
        self.inputs = self.payload.get("resolved_inputs") or {}
        self.templates = templates
        self.services = services
        self.dir = storage.job_dir(job["id"])
        self.log_path = self.dir / "logs" / "technique.log"
        self._last_progress = 0.0

    # ------------------------------------------------------------ événements
    def _emit(self, type_, **data):
        j = self.job
        emit(self.db, type_, job_id=j["id"], run_id=j.get("run_id"), graph_id=j.get("graph_id"), node_id=j.get("node_id"),
             item_id=j.get("item_id") or None, step=j.get("step"), item_key=j.get("item_key"), **data)

    def stage(self, name: str, message: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET stage = ?, heartbeat_at = datetime('now') WHERE id = ?", (message, self.job["id"]))
        self._emit("stage", stage=name, message=message)
        self.log(f"[étape] {message}")

    def progress(self, value=None, maximum=None, message: str | None = None, estimated: bool = False, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_progress < 0.4:
            return
        self._last_progress = now
        prog = {"value": value, "max": maximum, "message": message, "estimated": estimated, "at": now}
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET progress = ?, heartbeat_at = datetime('now') WHERE id = ?", (dumps(prog), self.job["id"]))
        self._emit("progress", **prog)

    def log(self, line: str) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")

    def cancelled(self) -> bool:
        r = self.db.one("SELECT cancel_requested FROM jobs WHERE id = ?", (self.job["id"],))
        return bool(r and r["cancel_requested"])

    def check_cancel(self) -> None:
        if self.cancelled():
            raise Cancelled("Annulation demandée")

    def set_engine_ref(self, ref: dict) -> None:
        cur = self.job.get("engine_ref") or {}
        cur.update(ref)
        self.job["engine_ref"] = cur
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET engine_ref = ? WHERE id = ?", (dumps(cur), self.job["id"]))

    # ------------------------------------------------------------ entrées
    def ref(self, name: str) -> Any:
        return self.inputs.get(name)

    def ref_file(self, ref: dict, prefer: tuple = ("image", "mesh", "rigged_mesh", "video")) -> pathlib.Path:
        """Fichier principal d'une référence d'entrée (variante → artefact ; import → fichier original)."""
        if ref is None or ref.get("kind") == "missing":
            raise StepFailure(f"Entrée manquante : {ref.get('reason') if ref else 'non connectée'}", code="missing_input")
        if ref["kind"] == "upload":
            up = self.storage.get_upload(ref["upload_id"])
            if not up:
                raise StepFailure(f"Fichier importé {ref['upload_id']} introuvable", code="missing_input")
            return self.storage.abs(up["path"])
        if ref["kind"] == "variant":
            v = self.services.variant(ref["variant_id"])
            outs = v["outputs"]
            keys = [ref.get("output")] if ref.get("output") and ref.get("output") != "*" else []
            keys += [k for k in prefer if k not in keys] + [k for k in outs if k not in keys and k not in prefer]
            for k in keys:
                ids = outs.get(k)
                if ids:
                    a = self.storage.get_artifact(ids[0])
                    return self.storage.abs(a["path"])
            raise StepFailure(f"La variante {ref['variant_id']} n'a pas de sortie exploitable", code="missing_input")
        raise StepFailure(f"Référence d'entrée non prise en charge : {ref.get('kind')}", code="missing_input")

    def ref_variant(self, ref: dict) -> dict | None:
        return self.services.variant(ref["variant_id"]) if ref and ref.get("kind") == "variant" else None

    def text(self, name: str) -> str:
        r = self.ref(name)
        if not r or r.get("kind") != "text":
            raise StepFailure(f"Prompt « {name} » manquant", code="missing_input")
        return r["text"]

    # ------------------------------------------------------------ sorties
    def out_path(self, name: str) -> pathlib.Path:
        return self.dir / "outputs" / name

    def add(self, path: pathlib.Path, role: str, meta: dict | None = None) -> str:
        a = self.storage.add_artifact(path, job_id=self.job["id"], role=role, meta=meta or {})
        self._emit("artifact_created", artifact_id=a["id"], role=role, kind=a["kind"], url=a["url"], size=a["size"])
        return a["id"]


class Services:
    """Accès partagé aux variantes et aux moteurs (clients ComfyUI, verrou GPU)."""

    def __init__(self, db: Database, cfg: C.Config):
        self.db, self.cfg = db, cfg
        self._clients: dict[str, ComfyClient] = {}
        self.gpu_owner: str | None = None
        self.gpu_lock = threading.Lock()

    def variant(self, vid: str) -> dict:
        r = self.db.one("SELECT * FROM variants WHERE id = ?", (vid,))
        if not r:
            raise StepFailure(f"Variante {vid} introuvable", code="missing_input")
        d = dict(r)
        for k in ("params", "inputs", "outputs", "engine", "stats"):
            d[k] = loads(d.get(k), {})
        return d

    def comfy(self, which: str = "dedicated") -> ComfyClient:
        from . import launcher

        if which == "dedicated" or self.cfg.section("video").get("provider") == "comfyui_dedicated":
            st = launcher.read_state(self.cfg)
            url = (st.get("comfyui") or {}).get("url") or f"http://127.0.0.1:{self.cfg.section('comfyui').get('port', 8190)}"
        else:
            st = launcher.read_state(self.cfg)
            url = (st.get("video_comfy") or {}).get("url") or (self.cfg.section("video").get("url") or "http://127.0.0.1:8188")
        c = self._clients.get(which)
        if c is None or c.base != url:
            c = ComfyClient(url)
            self._clients[which] = c
        return c

    def ensure_dedicated(self, ctx: StepContext | None = None, timeout: float = 180, low_memory: bool = False) -> ComfyClient:
        """ComfyUI dédié prêt. low_memory (vidéo MiniMax installée) : s'il tourne en profil standard, lancé par Atelier 3D et
        sans travail en cours, il est relancé en profil mémoire réduite ; jamais d'interruption d'un calcul."""
        from . import launcher

        c = self.comfy("dedicated")
        if c.alive():
            if not low_memory:
                return c
            st = launcher.read_state(self.cfg)
            rec = st.get("comfyui")
            if not rec or rec.get("low_memory") or launcher.owned_alive(rec) is None:
                return c
            q = c.queue()
            if q["running"] or q["pending"]:
                if ctx:
                    ctx.log("ComfyUI dédié occupé : vidéo lancée sans relance en profil mémoire réduite")
                return c
            if ctx:
                ctx.stage("engine_restart", "Relance du ComfyUI dédié en profil mémoire réduite (vidéo MiniMax)")
            launcher.stop_rec(rec, "comfyui")
            st.pop("comfyui", None)
            launcher.write_state(self.cfg, st)
            time.sleep(2)
        if ctx:
            ctx.stage("engine_start", "Démarrage du ComfyUI dédié" + (" (profil mémoire réduite)" if low_memory else ""))
        st = launcher.read_state(self.cfg)
        rec = launcher.start_comfy(self.cfg, st, low_memory=low_memory)
        c = self.comfy("dedicated")
        t0 = time.time()
        while time.time() - t0 < timeout:
            if c.alive():
                return c
            if launcher.owned_alive(rec) is None:
                raise StepFailure("Le ComfyUI dédié s'est arrêté au démarrage", code="engine_start",
                                  corrective=f"Voir {self.cfg.logs_dir / 'comfyui.log'}")
            time.sleep(1)
        raise StepFailure("Le ComfyUI dédié ne répond pas", code="engine_start")

    def switch_gpu(self, engine: str, ctx: StepContext) -> None:
        """Libère réellement le moteur GPU précédent avant d'en utiliser un autre (verrou global)."""
        if ctx.job.get("resource") != "gpu":
            return  # étape hors verrou GPU : ne touche jamais aux moteurs GPU en cours
        prev = self.gpu_owner
        if prev == engine:
            return
        if prev == "comfy_dedicated":
            try:
                c = self.comfy("dedicated")
                if c.alive():
                    c.free(unload_models=True)
                    ctx.log("Modèles du ComfyUI dédié déchargés (POST /free unload_models)")
                    ctx._emit("engine_switch", released="comfy_dedicated", acquired=engine)
                    time.sleep(1.5)
            except Exception as e:
                ctx.log(f"Libération ComfyUI dédié impossible : {e}")
        elif prev == "comfy_video":
            try:
                c = self.comfy("video")
                if c.alive():
                    q = c.queue()
                    if not q["running"] and not q["pending"]:
                        c.free(unload_models=True)
                        ctx.log("File du ComfyUI vidéo vide : modèles déchargés (POST /free)")
                    else:
                        ctx.log("ComfyUI vidéo occupé par d'autres travaux : aucune libération forcée")
                    ctx._emit("engine_switch", released="comfy_video", acquired=engine)
            except Exception as e:
                ctx.log(f"Libération ComfyUI vidéo impossible : {e}")
        self.gpu_owner = engine


# ---------------------------------------------------------------------------- utilitaires image

def image_info(path: pathlib.Path) -> dict:
    with Image.open(path) as im:
        info = {"width": im.width, "height": im.height, "mode": im.mode}
        if im.mode in ("RGBA", "LA"):
            a = im.getchannel("A")
            lo, hi = a.getextrema()
            hist = a.histogram()
            transparent = sum(hist[:8]) / (im.width * im.height)
            info.update({"alpha": True, "alpha_min": lo, "alpha_max": hi, "transparent_ratio": round(transparent, 4)})
        else:
            info["alpha"] = False
        return info


def side_by_side(a: pathlib.Path, b: pathlib.Path, out: pathlib.Path, height: int = 512) -> pathlib.Path:
    ims = []
    for p in (a, b):
        im = Image.open(p).convert("RGBA")
        im = im.resize((max(1, int(im.width * height / im.height)), height))
        bg = Image.new("RGBA", im.size, (235, 235, 238, 255))
        bg.alpha_composite(im)
        ims.append(bg)
    w = sum(i.width for i in ims) + 12
    canvas = Image.new("RGB", (w, height), (255, 255, 255))
    x = 0
    for i in ims:
        canvas.paste(i.convert("RGB"), (x, 0))
        x += i.width + 12
    canvas.save(out)
    return out


def flatten_alpha(path: pathlib.Path, color: str, ctx: "StepContext") -> pathlib.Path:
    """Compose une image RGBA sur une couleur unie (le moteur cible ignore l'alpha)."""
    with Image.open(path) as im:
        if im.mode not in ("RGBA", "LA") and "transparency" not in im.info:
            return path
        rgba = im.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, color)
    bg.alpha_composite(rgba)
    out = ctx.dir / "inputs" / (path.stem + "_flat.png")
    bg.convert("RGB").save(out)
    ctx.log(f"Entrée RGBA composée sur {color} avant envoi (le workflow n'exploite pas le canal alpha) : {out.name}")
    return out


def fit_dims(src: pathlib.Path, area: int, multiple: int = 16, max_mp: float = 2.0) -> tuple[int, int]:
    with Image.open(src) as im:
        ar = im.width / im.height
    area = min(area, int(max_mp * 1_000_000))
    h = math.sqrt(area / ar)
    w = h * ar
    return max(multiple, int(round(w / multiple)) * multiple), max(multiple, int(round(h / multiple)) * multiple)


# ---------------------------------------------------------------------------- ComfyUI générique

def run_comfy(ctx: StepContext, alias_default: str, values: dict, uploads: dict[str, pathlib.Path], *, which: str = "dedicated",
              timeout_s: float = 3 * 3600, use_param_alias: bool = False) -> tuple[dict, Any, ComfyClient]:
    aliases = ctx.cfg.get("workflows.aliases", {}) or {}
    alias = (ctx.params.get("workflow_id") if use_param_alias else None) or alias_default
    try:
        tpl = ctx.templates.resolve(alias, aliases)
    except WorkflowError as e:
        raise StepFailure(f"Workflow « {alias} » non résolu : aucun template API validé ({e})", code="workflow_unresolved")
    client = ctx.services.ensure_dedicated(ctx) if which == "dedicated" else ctx.services.ensure_video(ctx)
    ctx.services.switch_gpu("comfy_dedicated" if which == "dedicated" else "comfy_video", ctx)
    # vérification contre le ComfyUI réellement lancé
    folders = {m["folder"].split("/")[0] for m in tpl.bindings.get("required_models", [])}
    problems = tpl.verify(client.object_info(), {f: client.models(f) for f in folders})
    if problems:
        raise StepFailure("Workflow non exécutable sur ce ComfyUI : " + " ; ".join(problems[:6]), code="workflow_invalid", details=problems)
    ref = ctx.job.get("engine_ref") or {}
    prompt_id = ref.get("prompt_id")
    submit = True
    if prompt_id:  # reprise : on vérifie l'état réel avant toute nouvelle soumission
        h = client.history(prompt_id)
        q = client.queue()
        if h is not None and (h.get("status") or {}).get("status_str") == "success":
            ctx.log(f"Reprise : prompt {prompt_id} déjà terminé, collecte sans resoumission")
            ctx._emit("resume_collect", prompt_id=prompt_id)
            return h, tpl, client
        if prompt_id in q["running"] or prompt_id in q["pending"]:
            ctx.log(f"Reprise : prompt {prompt_id} encore en cours, suivi sans resoumission")
            submit = False
        else:
            prompt_id = None
    flatten = tpl.bindings.get("flatten_alpha_inputs") or {}
    for key, path in uploads.items():
        if key in flatten:
            path = flatten_alpha(path, flatten[key], ctx)
        name = f"{ctx.job['id']}_{key}{path.suffix.lower()}"
        values[key] = client.upload_image(path, name)
    values.setdefault("filename_prefix", f"atelier3d/{ctx.job['id']}") if "filename_prefix" in tpl.bindings.get("inputs", {}) else None
    wf = tpl.instantiate(values)
    for line in normalize_combo_paths(wf, client.object_info()):
        ctx.log(line)
    (ctx.dir / "workflow.submitted.api.json").write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
    if prompt_id is None:
        prompt_id = new_prompt_id()
        ctx.set_engine_ref({"engine": "comfyui", "comfy_url": client.base, "prompt_id": prompt_id, "client_id": ctx.job["id"],
                            "workflow_id": tpl.id, "workflow_sha256": tpl.sha256})
    ctx.stage("engine", f"{tpl.label} — exécution ComfyUI")
    preview_path = ctx.dir / "previews" / "live.jpg"
    node_labels = {nid: n.get("class_type") for nid, n in wf.items()}

    def on_event(kind, data):
        if kind == "progress":
            ctx.progress(data.get("value"), data.get("max"), message=f"{node_labels.get(str(data.get('node')), data.get('node'))}")
        elif kind == "executing" and data.get("node"):
            ctx._emit("engine_node", node=data.get("node"), class_type=node_labels.get(str(data.get("node"))))
        elif kind == "execution_cached":
            ctx._emit("engine_cached_nodes", nodes=data.get("nodes"))
        elif kind in ("submitted", "execution_start", "cancel_sent", "ws_unavailable"):
            ctx._emit("engine_" + kind, **{k: v for k, v in data.items() if k != "prompt"})

    def on_preview(data: bytes, fmt: str):
        try:
            p = preview_path.with_suffix("." + ("png" if fmt == "png" else "jpg"))
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)
            ctx._emit("live_preview", url=f"/api/v1/jobs/{ctx.job['id']}/live-preview?t={int(time.time() * 1000)}")
        except OSError:
            pass

    try:
        h = client.run(wf, prompt_id=prompt_id, client_id=ctx.job["id"], on_event=on_event, should_cancel=ctx.cancelled,
                       timeout_s=timeout_s, submit=submit, on_preview=on_preview)
    except ComfyError as e:
        if e.kind == "interrupted" and ctx.cancelled():
            raise Cancelled("Interrompu dans ComfyUI (prompt ciblé)")
        raise StepFailure(str(e), code=f"comfy_{e.kind}", oom=e.oom, details=e.details,
                          corrective="Mémoire GPU insuffisante : libérez la VRAM ou réduisez la résolution." if e.oom else None)
    return h, tpl, client


def comfy_out(ctx, client, h, tpl, key: str, dest_name: str, role: str, meta: dict | None = None, required=True) -> list[str]:
    b = tpl.bindings["outputs"].get(key)
    if not b:
        return []
    files = history_files(h, b["node"], b["kind"])
    if not files:
        if required:
            raise StepFailure(f"ComfyUI n'a renvoyé aucune sortie « {key} » (node {b['node']})", code="no_output")
        return []
    ids = []
    for i, f in enumerate(files):
        ext = pathlib.Path(f["filename"]).suffix or ".bin"
        dest = ctx.out_path(f"{dest_name}{'' if i == 0 else '_' + str(i)}{ext}")
        client.download(f, dest)
        ids.append(ctx.add(dest, role, {**(meta or {}), "comfy_file": f}))
    return ids


def comfy_download(ctx, client, h, tpl, key: str, dest_name: str) -> list[pathlib.Path]:
    """Télécharge une sortie ComfyUI SANS l'enregistrer comme résultat (sortie brute à post-traiter)."""
    b = tpl.bindings["outputs"][key]
    files = history_files(h, b["node"], b["kind"])
    if not files:
        raise StepFailure(f"ComfyUI n'a renvoyé aucune sortie « {key} » (node {b['node']})", code="no_output")
    out = []
    for i, f in enumerate(files):
        dest = ctx.dir / "work" / f"{dest_name}{'' if i == 0 else '_' + str(i)}{pathlib.Path(f['filename']).suffix or '.png'}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download(f, dest)
        out.append(dest)
    return out


# ---------------------------------------------------------------------------- capacités

def exec_image_create(ctx: StepContext) -> dict:
    prompt = ctx.text("prompt")
    p = ctx.params
    values = {"prompt": prompt, "seed": int(ctx.payload["seed"]), "width": int(p.get("width", 1024)), "height": int(p.get("height", 1024)),
              "refine_prompt": bool(p.get("refine_prompt", False))}
    ctx.stage("prepare", "Préparation du workflow Krea 2 Turbo (texte seul)")
    h, tpl, client = run_comfy(ctx, "image_create_default", values, {}, use_param_alias=True)
    ids = comfy_out(ctx, client, h, tpl, "image", "image", "image")
    final = history_text(h, tpl.bindings["outputs"]["final_prompt"]["node"]) if "final_prompt" in tpl.bindings["outputs"] else None
    tp = ctx.out_path("prompts.json")
    tp.write_text(json.dumps({"prompt_utilisateur": prompt, "prompt_final_moteur": final}, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add(tp, "prompts")
    info = image_info(ctx.storage.abs(ctx.storage.get_artifact(ids[0])["path"]))
    return {"outputs": {"image": ids}, "engine": {**tpl.fingerprint(), "prompt_id": ctx.job["engine_ref"]["prompt_id"]},
            "report": {"prompt_original": prompt, "prompt_final": final, "image": info}}


def exec_image_edit(ctx: StepContext) -> dict:
    prompt = ctx.text("prompt")
    src = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    p = ctx.params
    w, h_ = fit_dims(src, int(p.get("width", 1024)) * int(p.get("height", 1024)))
    values = {"prompt": prompt, "seed": int(ctx.payload["seed"]), "steps": int(p.get("steps", 10)), "ref_boost": float(p.get("ref_boost", 4.0)),
              "grounding_px": int(p.get("grounding_px", 768)), "width": w, "height": h_}
    if ctx.ref("image_b"):
        raise StepFailure("La seconde référence (scène/sujet) n'est pas exposée par le workflow d'édition V1", code="unsupported_input")
    ctx.stage("prepare", f"Édition conditionnée par l'image (sortie {w}×{h_})")
    hist, tpl, client = run_comfy(ctx, "image_edit_default", values, {"image": src}, use_param_alias=True)
    ids = comfy_out(ctx, client, hist, tpl, "image", "image", "image")
    out = ctx.storage.abs(ctx.storage.get_artifact(ids[0])["path"])
    cmp_path = side_by_side(src, out, ctx.out_path("comparaison_source_resultat.jpg"))
    ctx.add(cmp_path, "comparison", {"left": "source", "right": "résultat"})
    tp = ctx.out_path("prompts.json")
    tp.write_text(json.dumps({"instruction": prompt, "prompt_final_moteur": prompt,
                              "note": "Krea2EditGroundedEncode reçoit l'instruction telle quelle avec l'image (Qwen3-VL)"}, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add(tp, "prompts")
    return {"outputs": {"image": ids}, "engine": {**tpl.fingerprint(), "prompt_id": ctx.job["engine_ref"]["prompt_id"]},
            "report": {"instruction": prompt, "source": image_info(src), "result": image_info(out), "dimensions": [w, h_],
                       "limits": tpl.bindings.get("limits")}}


def exec_openai_image(ctx: StepContext) -> dict:
    """Génération ou édition par OpenAI Images (service externe). La clé est lue chiffrée (DPAPI) au moment de l'appel et
    n'apparaît ni dans les journaux, ni dans les métadonnées, ni dans les erreurs."""
    from .adapters import openai_images as O
    from .secrets_store import OPENAI_KEY, SecretStore, load_services

    step = ctx.job["step"]
    op = "edit" if step in ("edit", "pose") else "create"
    p = ctx.params
    key = SecretStore(ctx.cfg.config_dir).get(OPENAI_KEY)
    if not key:
        raise StepFailure("Clé d'API OpenAI non configurée", code="openai_key_missing",
                          corrective="Réglages → OpenAI : saisissez votre clé, puis « Tester la clé »")
    s = load_services(ctx.cfg.config_dir)["openai"]
    model = p.get("openai_model") or (s.get("model_edit") if op == "edit" else s.get("model_generate"))
    if not model:
        model = O.pick_default(s.get("models") or [], op) or ("gpt-image-1")
    if step == "pose":  # « Préparer l'image » : consigne de pose fixe, taille au format de l'image source
        prompt = pose_instruction(p)
        w0, h0 = fit_dims(ctx.ref_file(ctx.ref("image"), prefer=("image",)), 1024 * 1024)
        size = O.size_for(model, w0, h0)
    else:
        prompt = ctx.text("prompt")
        size = O.size_for(model, int(p.get("width", 1024)), int(p.get("height", 1024)))
    quality = p.get("quality") or s.get("quality") or "auto"
    background = p.get("background") or "auto"
    moderation = p.get("moderation") or s.get("moderation") or "auto"
    out = ctx.out_path("image.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    sources: list[pathlib.Path] = []
    if op == "edit":
        sources.append(ctx.ref_file(ctx.ref("image"), prefer=("image",)))
        if ctx.ref("image_b"):
            sources.append(ctx.ref_file(ctx.ref("image_b"), prefer=("image",)))
    if ctx.cancelled():
        raise Cancelled()
    ctx.stage("engine", f"OpenAI {model} — {'édition' if op == 'edit' else 'génération'} {size}, qualité {quality} (service externe, "
                        "jusqu'à 2 minutes)")
    ctx.log(f"OpenAI Images : modèle={model} taille={size} qualité={quality} fond={background} modération={moderation} "
            f"images_source={len(sources)}")
    if op == "edit":
        meta = O.edit(key, model=model, prompt=prompt, images=sources, size=size, quality=quality, background=background,
                      moderation=moderation, out=out)
    else:
        meta = O.generate(key, model=model, prompt=prompt, size=size, quality=quality, background=background,
                          moderation=moderation, out=out)
    ids = [ctx.add(out, "image", {"engine": "openai", "model": model})]
    if op == "edit":
        cmp_path = side_by_side(sources[0], out, ctx.out_path("comparaison_source_resultat.jpg"))
        ctx.add(cmp_path, "comparison", {"left": "source", "right": "résultat"})
    tp = ctx.out_path("prompts.json")
    tp.write_text(json.dumps({"prompt_utilisateur": prompt, "prompt_revise_par_openai": meta.get("revised_prompt"),
                              "moteur": "OpenAI Images (service externe)", "modele": model}, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add(tp, "prompts")
    info = image_info(out)
    report = {"engine": "OpenAI Images API", "model": model, "operation": op, "size": size, "quality": quality,
              "background": background, "moderation": moderation, "usage": meta.get("usage"), "image": info,
              "sources": [image_info(x) for x in sources], "external_service": True}
    if background == "transparent" and not info.get("alpha"):
        report["warning"] = "Fond transparent demandé mais l'image reçue n'a pas de canal alpha"
    return {"outputs": {"image": ids}, "engine": {"engine": "openai", "model": model, "size": size, "quality": quality},
            "report": report}


def exec_remove_background(ctx: StepContext) -> dict:
    ref = ctx.ref("previous") or ctx.ref("image")
    src = ctx.ref_file(ref, prefer=("image",))
    ctx.stage("prepare", "Détourage BiRefNet (masque alpha recalculable)")
    hist, tpl, client = run_comfy(ctx, "remove_background_default", {}, {"image": src})
    ids = comfy_out(ctx, client, hist, tpl, "image", "image_rgba", "image")
    mask_ids = comfy_out(ctx, client, hist, tpl, "mask", "mask", "mask", required=False)
    out = ctx.storage.abs(ctx.storage.get_artifact(ids[0])["path"])
    info = image_info(out)
    if not info.get("alpha") or info.get("alpha_min", 255) >= 250:
        raise StepFailure("Le résultat ne contient pas de vrai canal alpha (aucune transparence détectée)", code="no_alpha", details=info)
    cmp_path = side_by_side(src, out, ctx.out_path("comparaison_detourage.jpg"))
    ctx.add(cmp_path, "comparison")
    warnings = []
    if info["transparent_ratio"] < 0.05:
        warnings.append("Moins de 5 % de pixels transparents : le fond n'a peut-être pas été retiré.")
    if info["transparent_ratio"] > 0.97:
        warnings.append("Plus de 97 % transparent : le sujet a peut-être été effacé.")
    return {"outputs": {"image": ids, "mask": mask_ids}, "engine": {**tpl.fingerprint(), "prompt_id": ctx.job["engine_ref"]["prompt_id"]},
            "report": {"alpha": info, "warnings": warnings, "png_rgba_verified": True}}


def exec_crop(ctx: StepContext) -> dict:
    ref = ctx.ref("previous") or ctx.ref("image")
    src = ctx.ref_file(ref, prefer=("image",))
    prev = ctx.ref_variant(ref)
    p = ctx.params
    ctx.stage("crop", "Cadrage et contrôle du sujet")
    with Image.open(src) as im0:
        has_alpha = im0.mode in ("RGBA", "LA", "PA") or "transparency" in im0.info
        if not has_alpha:  # détourage désactivé : fond conservé, rien à recadrer sans masque du sujet
            im0.load()
            out = ctx.out_path("image_preparee.png")
            out.parent.mkdir(parents=True, exist_ok=True)
            im0.convert("RGB").save(out)
            mask_out = ctx.out_path("masque.png")
            Image.new("L", im0.size, 255).save(mask_out)
            return {"outputs": {"image": [ctx.add(out, "image")], "mask": [ctx.add(mask_out, "mask")]},
                    "engine": {"engine": "Pillow (local)", "version": Image.__version__},
                    "report": {"bbox": None, "background_kept": True, "source_variant": prev["id"] if prev else None,
                               "warnings": ["Fond conservé (détourage désactivé) : pas de recadrage sur le sujet, image transmise telle quelle."]}}
    im = Image.open(src).convert("RGBA")
    a = im.getchannel("A")
    bbox = a.point(lambda v: 255 if v > 16 else 0).getbbox()
    warnings = []
    if bbox is None:
        raise StepFailure("Aucun sujet détecté (alpha vide)", code="empty_subject")
    l, t, r, b = bbox
    margin = 3
    if l <= margin or t <= margin or r >= im.width - margin or b >= im.height - margin:
        warnings.append("Le sujet touche le bord de l'image : une partie du corps est peut-être coupée (les zones cachées ne seront pas retrouvées).")
    if p.get("crop_to_subject", True):
        pad = float(p.get("padding", 0.08))
        bw, bh = r - l, b - t
        side = int(max(bw, bh) * (1 + 2 * pad))
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(im.crop(bbox), ((side - bw) // 2, (side - bh) // 2))
        im = canvas
    out = ctx.out_path("image_preparee.png")
    im.save(out)
    ids = [ctx.add(out, "image")]
    mask_out = ctx.out_path("masque.png")
    im.getchannel("A").save(mask_out)
    mids = [ctx.add(mask_out, "mask")]
    return {"outputs": {"image": ids, "mask": mids}, "engine": {"engine": "Pillow (local)", "version": Image.__version__},
            "report": {"bbox": bbox, "warnings": warnings, "source_variant": prev["id"] if prev else None}}


def exec_mesh_generate(ctx: StepContext) -> dict:
    src = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    p = ctx.params
    ctx.stage("prepare", "Préparation de la génération 3D (Pixal3D natif)")
    tplb = ctx.templates.resolve("image_to_mesh_default", ctx.cfg.get("workflows.aliases", {}) or {})
    prof = (tplb.bindings.get("quality_profiles") or {}).get(p.get("quality", "standard"), {})
    values = {"seed": int(ctx.payload["seed"]), "texture_size": int(p.get("texture_size") or prof.get("texture_size", 2048)),
              "decimate_target": int(p.get("decimate_target") or prof.get("decimate_target", 300000)),
              "use_trellis2": p.get("engine") == "trellis2", "remove_background": bool(p.get("remove_background", True))}
    hist, tpl, client = run_comfy(ctx, "image_to_mesh_default", values, {"image": src}, timeout_s=4 * 3600)
    mesh_ids = comfy_out(ctx, client, hist, tpl, "mesh", "mesh", "mesh")
    extra = {}
    for key in ("base_color", "metallic", "roughness", "normal", "ambient_occlusion", "uv_atlas", "input_crop", "input_mask"):
        extra[key] = comfy_out(ctx, client, hist, tpl, key, key, "texture" if key not in ("input_crop", "input_mask", "uv_atlas") else key, required=False)
    info_text = history_text(hist, tpl.bindings["outputs"]["mesh_info"]["node"]) if "mesh_info" in tpl.bindings["outputs"] else None
    glb = ctx.storage.abs(ctx.storage.get_artifact(mesh_ids[0])["path"])
    ctx.stage("previews", "Aperçus Blender du modèle généré")
    res = blender(ctx, "mesh_inspect.py", {"mesh_path": str(glb), "prefix": "mesh"})
    prev_ids = [ctx.add(pathlib.Path(x), "preview") for x in res.get("previews", [])]
    return {"outputs": {"mesh": mesh_ids, "textures": [i for k in ("base_color", "metallic", "roughness", "normal", "ambient_occlusion") for i in extra[k]],
                        "previews": prev_ids, "source": extra["input_crop"]},
            "engine": {**tpl.fingerprint(), "prompt_id": ctx.job["engine_ref"]["prompt_id"], "parameters": values},
            "report": {"mesh": res.get("mesh"), "material_maps": res.get("material_maps"), "engine_mesh_info": info_text,
                       "warnings": res.get("warnings", []),
                       "note": "Aperçus = rendus du modèle généré ; l'arrière et les zones cachées sont des hypothèses du moteur (une seule image source)."}}


def blender(ctx: StepContext, script: str, args: dict, subdir: str | None = None) -> dict:
    exe = ctx.cfg.get("blender.path")
    if not exe or not pathlib.Path(exe).is_file():
        raise StepFailure("Blender introuvable : relancez INSTALL.bat ou indiquez blender.path", code="blender_missing")
    work = ctx.dir / "outputs" if subdir is None else ctx.dir / "outputs" / subdir

    def on_event(ev):
        if ev.get("type") == "stage":
            ctx.stage(ev.get("stage", "blender"), ev.get("message", ""))
        elif ev.get("type") == "warning":
            ctx._emit("warning", message=ev.get("message"))
            ctx.log("[avertissement] " + str(ev.get("message")))

    return run_script(exe, ctx.cfg.blender_scripts_dir / script, args, work, on_event=on_event, should_cancel=ctx.cancelled,
                      log_path=ctx.log_path)


def _prev_mesh(ctx, key="previous"):
    ref = ctx.ref(key) or ctx.ref("mesh")
    return ctx.ref_file(ref, prefer=("mesh", "rigged_mesh")), ctx.ref_variant(ref)


def exec_mesh_prepare(ctx: StepContext) -> dict:
    step = ctx.job["step"]
    p = ctx.params
    if step == "repair":
        src, _ = _prev_mesh(ctx, "mesh")
    else:
        src, _ = _prev_mesh(ctx, "previous")
    args = {"stage": step, "mesh_path": str(src), "triangle_budget": int(p.get("triangle_budget", 40000)),
            "simplify_mode": p.get("simplify_mode", "decimate"), "texture_size": int(p.get("texture_size", 2048)),
            "repair": bool(p.get("repair", True)), "keep_parts_separate": bool(p.get("keep_parts_separate", True))}
    if step == "bake":
        # haute résolution = sortie de l'étape « repair » qui a alimenté la simplification (provenance)
        simp = ctx.ref_variant(ctx.ref("previous"))
        rep_ref = (simp or {}).get("inputs", {}).get("previous")
        high = ctx.ref_file(rep_ref, prefer=("mesh",)) if rep_ref else src
        args["high_path"] = str(high)
    ctx.services.switch_gpu("blender", ctx)
    res = blender(ctx, "mesh_prepare.py", args)
    out_ids = [ctx.add(pathlib.Path(res["glb"]), "mesh")]
    prev_ids = [ctx.add(pathlib.Path(x), "preview") for x in res.get("previews", [])]
    tex_ids = []
    for m in (res.get("bake") or {}).get("maps", []):
        if m.get("file"):
            tex_ids.append(ctx.add(pathlib.Path(m["file"]), "texture", {"map": m["map"], "resolution": m.get("resolution")}))
    rep = ctx.out_path(f"rapport_{step}.json")
    rep.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    rid = ctx.add(rep, "report")
    return {"outputs": {"mesh": out_ids, "report": [rid], "previews": prev_ids, "textures": tex_ids},
            "engine": {"engine": "Blender", "version": res.get("blender_version"), "script": "mesh_prepare.py", "stage": step},
            "report": res}


def exec_rig_mia(ctx: StepContext) -> dict:
    mesh, _ = _prev_mesh(ctx, "mesh")
    mia = ctx.cfg.section("mia")
    if not mia.get("enabled") or not pathlib.Path(mia.get("python", "")).is_file():
        raise StepFailure("Moteur Make-It-Animatable non installé", code="engine_unavailable")
    import subprocess

    ctx.services.switch_gpu("mia", ctx)
    out = ctx.out_path("mia_prediction.npz")
    cmd = [mia["python"], mia["runner"], "--mia-dir", mia["code"], "--weights-dir", mia["weights"], "--input", str(mesh), "--output", str(out)]
    if ctx.params.get("no_fingers"):
        cmd.append("--no-fingers")
    if ctx.params.get("input_normal", True):
        cmd.append("--input-normal")
    ctx.stage("mia", "Make-It-Animatable : articulations et poids")
    ctx.set_engine_ref({"engine": "mia", "cmd": cmd[1:3]})
    result, err = None, None
    with open(ctx.log_path, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=0x08000000, env={**__import__("os").environ, "PYTHONUTF8": "1"})
        for line in proc.stdout:
            log.write(line)
            if line.startswith("@@A3D "):
                ev = json.loads(line[6:])
                if ev["type"] == "stage":
                    ctx.stage("mia_" + ev["stage"], ev["message"])
                elif ev["type"] == "result":
                    result = ev
                elif ev["type"] == "error":
                    err = ev
            if ctx.cancelled():
                proc.kill()
                raise Cancelled("Annulé pendant MIA")
        proc.wait()
    if proc.returncode != 0 or result is None:
        e = err or {}
        raise StepFailure(f"Make-It-Animatable : {e.get('message', 'échec')}", code="mia_error", oom=bool(e.get("oom")), details=e,
                          corrective="Mémoire GPU insuffisante : fermez les autres applications GPU puis relancez cette étape." if e.get("oom") else None)
    aid = ctx.add(out, "rig_prediction")
    return {"outputs": {"rig_prediction": [aid]},
            "engine": {"engine": "Make-It-Animatable", "commit": mia.get("commit"), "weights": "output/best/new (HF jasongzy/Make-It-Animatable)",
                       "runner": "run_mia.py", "options": {"no_fingers": bool(ctx.params.get("no_fingers")), "input_normal": bool(ctx.params.get("input_normal", True))}},
            "stats": {"seconds": result.get("seconds"), "peak_vram_mb": result.get("peak_vram_mb")}, "report": result}


def exec_rig_bind(ctx: StepContext) -> dict:
    prev_ref = ctx.ref("previous")
    prev = ctx.ref_variant(prev_ref)
    if not prev:
        raise StepFailure("Prédiction de squelette manquante", code="missing_input")
    npz = ctx.storage.abs(ctx.storage.get_artifact(prev["outputs"]["rig_prediction"][0])["path"])
    mesh_ref = (prev.get("inputs") or {}).get("mesh")
    mesh = ctx.ref_file(mesh_ref, prefer=("mesh",))
    mode = "auto_weights" if ctx.params.get("engine") == "blender_auto" else "mia_weights"
    ctx.services.switch_gpu("blender", ctx)
    res = blender(ctx, "rig_bind.py", {"mesh_path": str(mesh), "npz_path": str(npz), "mode": mode,
                                       "joint_overrides": ctx.params.get("joint_overrides") or {}})
    return _rig_outputs(ctx, res)


def _rig_outputs(ctx, res):
    blend_id = ctx.add(pathlib.Path(res["blend"]), "rigged_blend")
    glb_id = ctx.add(pathlib.Path(res["glb"]), "rigged_mesh")
    prev_ids = [ctx.add(pathlib.Path(x), "preview") for x in res.get("previews", [])]
    rep = ctx.out_path("rapport_rig.json")
    rep.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    rid = ctx.add(rep, "report")
    w = res.get("weights") or {}
    status = "rig à corriger" if w.get("unweighted_vertices") else res.get("status_label")
    return {"outputs": {"rigged_mesh": [glb_id, blend_id], "report": [rid], "previews": prev_ids},
            "engine": {"engine": "Blender", "version": res.get("blender_version"), "script": "rig_bind.py", "mode": res.get("mode")},
            "report": {**res, "status": status}}


def exec_rig_articulated(ctx: StepContext) -> dict:
    mesh, _ = _prev_mesh(ctx, "mesh")
    ctx.services.switch_gpu("blender", ctx)
    res = blender(ctx, "rig_articulated.py", {"mesh_path": str(mesh), "pivots": ctx.params.get("pivots") or []})
    return _rig_outputs(ctx, res)


def exec_validate(ctx: StepContext) -> dict:
    ref = ctx.ref("rigged_mesh")
    v = ctx.ref_variant(ref)
    if not v:
        raise StepFailure("Maillage riggé manquant", code="missing_input")
    blend_path = None
    for aid in v["outputs"].get("rigged_mesh", []):
        a = ctx.storage.get_artifact(aid)
        if a["path"].endswith(".blend"):
            blend_path = ctx.storage.abs(a["path"])
    if blend_path is None:
        raise StepFailure("Aucun fichier .blend riggé dans l'entrée", code="missing_input")
    ctx.services.switch_gpu("blender", ctx)
    res = blender(ctx, "rig_validate.py", {"blend_path": str(blend_path), "amplitude": float(ctx.params.get("amplitude", 1.0))})
    prev_ids = [ctx.add(pathlib.Path(x), "pose_preview") for x in res.get("previews", [])]
    frames = [pathlib.Path(x) for x in res.get("animation_frames", [])]
    anim_ids = []
    if frames:
        ims = [Image.open(f).convert("RGBA") for f in frames]
        bg = [Image.alpha_composite(Image.new("RGBA", i.size, (242, 242, 245, 255)), i).convert("P", palette=Image.ADAPTIVE) for i in ims]
        gif = ctx.out_path("apercu_anime.gif")
        bg[0].save(gif, save_all=True, append_images=bg[1:] + bg[-2:0:-1], duration=180, loop=0)
        anim_ids.append(ctx.add(gif, "animation_preview"))
    validation = ctx.dir / "validation.json"
    validation.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    rid = ctx.add(validation, "validation")
    blend_id = ctx.add(pathlib.Path(res["blend"]), "rigged_blend")
    glb_id = ctx.add(pathlib.Path(res["glb"]), "rigged_mesh")
    return {"outputs": {"report": [rid], "rigged_mesh": [glb_id, blend_id], "previews": prev_ids + anim_ids},
            "engine": {"engine": "Blender", "version": res.get("blender_version"), "script": "rig_validate.py"},
            "report": res}


def exec_export(ctx: StepContext) -> dict:
    ref = ctx.ref("asset")
    v = ctx.ref_variant(ref)
    src = None
    rigged = False
    if v:
        for key in ("rigged_mesh", "mesh"):
            for aid in v["outputs"].get(key, []):
                a = ctx.storage.get_artifact(aid)
                if a["path"].endswith(".blend"):
                    src, rigged = ctx.storage.abs(a["path"]), True
            if src:
                break
    if src is None:
        src = ctx.ref_file(ref, prefer=("rigged_mesh", "mesh"))
    p = ctx.params
    ctx.services.switch_gpu("blender", ctx)
    res = blender(ctx, "asset_export.py", {"input_path": str(src), "name": p.get("name", "Asset"), "formats": p.get("formats") or ["blend", "glb", "fbx"],
                                           "target_height_m": float(p.get("target_height_m") or 0)}, subdir="export")
    ctx.stage("verify", "Vérification : réouverture du .blend et réimport .glb/.fbx dans un nouveau processus Blender")
    ver = blender(ctx, "verify_export.py", {"files": {k: res["files"].get(k) for k in ("blend", "glb", "fbx")}, "expect_rig": rigged,
                                            "expected_dimensions_m": res.get("dimensions_m")}, subdir="verify")
    ids = {}
    for k in ("blend", "glb", "fbx", "glb_poses_test"):
        if res["files"].get(k):
            ids[k] = ctx.add(pathlib.Path(res["files"][k]), f"export_{k}")
    tex_ids = [ctx.add(pathlib.Path(t["file"]), "texture", t) for t in res["files"].get("textures", [])]
    prev_ids = [ctx.add(pathlib.Path(x), "preview") for x in res.get("previews", [])]
    final_status = "export Blender vérifié" if ver.get("ok") else "échec"
    if ver.get("ok") and rigged:
        upstream = _upstream_verdict(ctx, v)
        if upstream == "rig à corriger":
            final_status = "rig à corriger"
    validation = {"status": final_status, "export": res, "verification": ver, "rigged": rigged,
                  "note": "« Prêt pour production » n'est jamais déclaré automatiquement."}
    (ctx.dir / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    vid = ctx.add(ctx.dir / "validation.json", "validation")
    if not ver.get("ok"):
        raise StepFailure("La vérification de l'export a échoué (réouverture/réimport)", code="export_verification_failed", details=ver)
    return {"outputs": {"files": [ids[k] for k in ("blend", "glb", "fbx", "glb_poses_test") if k in ids] + tex_ids, "report": [vid], "previews": prev_ids},
            "engine": {"engine": "Blender", "version": res.get("blender_version"), "scripts": ["asset_export.py", "verify_export.py"]},
            "report": validation}


def _upstream_verdict(ctx, v):
    seen = 0
    cur = v
    while cur and seen < 10:
        seen += 1
        rep_ids = cur["outputs"].get("report", [])
        for rid in rep_ids:
            a = ctx.storage.get_artifact(rid)
            try:
                data = json.loads(ctx.storage.abs(a["path"]).read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("verdict"):
                return data["verdict"]
        prevs = [x for x in (cur.get("inputs") or {}).values() if isinstance(x, dict) and x.get("kind") == "variant"]
        cur = ctx.services.variant(prevs[0]["variant_id"]) if prevs else None
    return None


# ---------------------------------------------------------------------------- « Préparer l'image » : pose A / T
POSE_INSTRUCTIONS = {
    "a_pose": ("Redraw this exact same character as a full-body character reference in a neutral A-pose: standing straight and "
               "facing the camera, arms straight and lowered about 45 degrees away from the torso, hands open and relaxed with the "
               "fingers slightly apart, palms facing the thighs, legs straight with the feet shoulder-width apart, neutral expression. "
               "Keep the same face, hairstyle, body proportions, outfit, colors, materials and accessories. The whole body is visible "
               "from the top of the head to the feet, nothing is cropped. Plain light grey background."),
    "t_pose": ("Redraw this exact same character as a full-body character reference in a T-pose: standing straight and facing the "
               "camera, both arms extended straight out to the sides at shoulder height, perfectly horizontal, hands open and flat "
               "with the palms facing down and the fingers together, legs straight with the feet slightly apart, neutral expression. "
               "Keep the same face, hairstyle, body proportions, outfit, colors, materials and accessories. The whole body is visible "
               "from the head to the feet and from one hand to the other, nothing is cropped. Plain light grey background."),
}


def pose_instruction(p: dict) -> str:
    base = POSE_INSTRUCTIONS.get(p.get("pose") or "")
    if not base:
        raise StepFailure(f"Pose inconnue : {p.get('pose')}", code="bad_params")
    extra = str(p.get("pose_details") or "").strip()
    return base + (f" Additional details: {extra}" if extra else "")


def exec_pose_krea(ctx: StepContext) -> dict:
    src = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    p = ctx.params
    instruction = pose_instruction(p)
    w, h_ = fit_dims(src, int(p.get("width", 1024)) * int(p.get("height", 1024)))  # profil réduit après OOM : 768×768
    values = {"prompt": instruction, "seed": int(ctx.payload["seed"]), "steps": 10, "ref_boost": float(p.get("ref_boost", 2.0)),
              "grounding_px": 768, "width": w, "height": h_}
    ctx.stage("prepare", f"{'A-pose' if p.get('pose') == 'a_pose' else 'T-pose'} : édition Krea 2 conditionnée par l'image ({w}×{h_})")
    hist, tpl, client = run_comfy(ctx, "image_edit_default", values, {"image": src})
    ids = comfy_out(ctx, client, hist, tpl, "image", "pose", "image")
    out = ctx.storage.abs(ctx.storage.get_artifact(ids[0])["path"])
    ctx.add(side_by_side(src, out, ctx.out_path("comparaison_pose.jpg")), "comparison", {"left": "source", "right": "pose"})
    tp = ctx.out_path("prompts.json")
    tp.write_text(json.dumps({"pose": p.get("pose"), "consigne_moteur": instruction}, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add(tp, "prompts")
    return {"outputs": {"image": ids}, "engine": {**tpl.fingerprint(), "prompt_id": ctx.job["engine_ref"]["prompt_id"]},
            "report": {"pose": p.get("pose"), "instruction": instruction, "source": image_info(src), "result": image_info(out),
                       "note": "Pose redessinée par IA : vérifier visuellement mains, doigts, visage et accessoires."}}


# ---------------------------------------------------------------------------- « Upscale image »
UPSCALE_TARGETS = {"2k": 2048, "4k": 4096}


def exec_upscale(ctx: StepContext) -> dict:
    src = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    p = ctx.params
    engine = p.get("engine", "seedvr2")
    target = UPSCALE_TARGETS.get(p.get("resolution", "2k"), 2048)
    with Image.open(src) as im:
        im.load()
        source = im.copy()
    has_alpha = source.mode in ("RGBA", "LA", "PA") or "transparency" in source.info
    long_side = max(source.size)
    tw, th = (round(source.width * target / long_side), round(source.height * target / long_side))
    warnings: list[str] = []
    out = ctx.out_path(f"upscale_{p.get('resolution', '2k')}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    engine_info: dict = {}
    if long_side >= target:
        warnings.append(f"Image source déjà de {long_side} px (≥ {target}) : simple réduction lanczos, sans IA.")
        result = source.resize((tw, th), Image.LANCZOS)
        engine_info = {"engine": "Pillow (local)", "version": Image.__version__}
        multiplier = round(target / long_side, 4)
    else:
        if engine == "seedvr2":
            multiplier = round(target / long_side, 4)
            if multiplier > 8:
                raise StepFailure(f"Image source trop petite ({source.width}×{source.height}) : ×{multiplier:.1f} dépasse ×8 (limite SeedVR2)",
                                  code="bad_input", corrective="Partez d'une image d'au moins 512 px, ou choisissez 2K.")
            values = {"multiplier": multiplier, "seed": int(ctx.payload.get("seed") or 0), "color_correction": p.get("color_correction", "lab")}
            ctx.stage("prepare", f"SeedVR2 3B : {source.width}×{source.height} → ≈{tw}×{th} (×{multiplier:g})")
            hist, tpl, client = run_comfy(ctx, "upscale_seedvr2_default", values, {"image": src}, timeout_s=2 * 3600)
        else:
            multiplier = 4.0
            ctx.stage("prepare", f"Real-ESRGAN x4 : {source.width}×{source.height} → {source.width * 4}×{source.height * 4}, puis {tw}×{th}")
            hist, tpl, client = run_comfy(ctx, "upscale_esrgan_default", {}, {"image": src}, timeout_s=3600)
        raw = comfy_download(ctx, client, hist, tpl, "image", "upscale_brut")[0]
        with Image.open(raw) as r:
            r.load()
            result = r.copy()
        engine_info = {**tpl.fingerprint(), "prompt_id": ctx.job["engine_ref"]["prompt_id"]}
        ctx.stage("finish", f"Taille exacte {tw}×{th}" + (" et canal alpha" if has_alpha else ""))
        if result.size != (tw, th):
            result = result.resize((tw, th), Image.LANCZOS)  # écart d'arrondi (SeedVR2) ou réduction depuis ×4 (ESRGAN)
    if has_alpha:
        a = result.getchannel("A") if result.mode == "RGBA" else None
        if a is None or a.getextrema()[0] >= 250:  # moteur sans alpha : masque d'origine agrandi (lanczos)
            a = source.convert("RGBA").getchannel("A").resize((tw, th), Image.LANCZOS)
        result = result.convert("RGB")
        result.putalpha(a)
    else:
        result = result.convert("RGB")
    result.save(out)
    ids = [ctx.add(out, "image", {"engine": engine, "target": target})]
    ctx.add(side_by_side(src, out, ctx.out_path("comparaison_upscale.jpg")), "comparison", {"left": "source", "right": "agrandie"})
    # zoom 1:1 au centre : source agrandie sans IA (bicubique) à gauche, résultat à droite
    cw = min(512, tw, th)
    cx, cy = (tw - cw) // 2, (th - cw) // 2
    crop_res = result.crop((cx, cy, cx + cw, cy + cw)).convert("RGBA")
    sx = source.width / tw
    crop_src = source.convert("RGBA").crop((round(cx * sx), round(cy * sx), round((cx + cw) * sx), round((cy + cw) * sx))).resize((cw, cw), Image.BICUBIC)
    zoom = Image.new("RGB", (cw * 2 + 12, cw), (255, 255, 255))
    for i, part in enumerate((crop_src, crop_res)):
        bg = Image.new("RGBA", part.size, (235, 235, 238, 255))
        bg.alpha_composite(part)
        zoom.paste(bg.convert("RGB"), (i * (cw + 12), 0))
    zp = ctx.out_path("zoom_detail.jpg")
    zoom.save(zp, quality=92)
    ctx.add(zp, "comparison", {"left": "source agrandie sans IA (bicubique)", "right": "résultat", "zoom": "1:1 au centre"})
    info = image_info(out)
    return {"outputs": {"image": ids}, "engine": engine_info,
            "report": {"engine": engine, "target_long_side": target, "multiplier": multiplier, "source": image_info(src), "result": info,
                       "alpha_kept": bool(has_alpha), "warnings": warnings,
                       "note": "SeedVR2 reconstruit des détails plausibles : à vérifier visuellement (texte, visages, motifs)." if engine == "seedvr2" else
                               "Real-ESRGAN n'invente pas de détails : rendu plus lisse."}}


# ---------------------------------------------------------------------------- « Prompts aléatoires »
SUBJECT_RULES = {
    "free": "",
    "scene": "Each prompt describes a place or a scene (architecture, landscape or interior) seen as a whole, without a character in the foreground.",
    "character": "Each prompt describes one single character, full body, clearly visible, with a distinctive outfit and silhouette.",
    "object": "Each prompt describes one single object or prop, entirely visible and centered, with its materials and details.",
    "creature": "Each prompt describes one single creature, full body, clearly visible, with its anatomy, skin and colors.",
}


def randomize_instruction(context: str, n: int, p: dict) -> str:
    """Consigne pour Qwen3-VL 4B. Liste numérotée plutôt que JSON : constaté le 06/10/2026, une demande de réponse JSON
    fait répondre au modèle un faux objet {"error": …} (6 essais sur 6), alors que la liste numérotée réussit (4 sur 4)."""
    lang = "French" if p.get("language") == "fr" else "English"
    rule = SUBJECT_RULES.get(p.get("subject") or "free", "")
    extra = str(p.get("extra") or "").strip()
    return (f"Write {n} different prompts for an image generator, inspired by this theme: \"{context}\".\n"
            + (f"{rule}\n" if rule else "")
            + f"Each prompt is one paragraph of 40 to 80 words in {lang}: main subject, setting, composition and camera framing, "
            f"lighting, colors, materials and mood. Make the {n} prompts clearly different from each other (different subjects, places, "
            f"times of day, viewpoints) while staying faithful to the theme: if it names a universe, a franchise, an era or a style, "
            f"use its typical architecture, technology, vehicles, creatures and costumes. No text, letters or logos in the images.\n"
            + (f"Additional instructions: {extra}\n" if extra else "")
            + "Write them as a numbered list, one prompt per line, starting with \"1.\", with no other text.")


def parse_prompt_list(text: str) -> list[str]:
    """Liste numérotée (« 1. … »), avec lignes de continuation ; à défaut tableau JSON, puis une ligne par prompt."""
    s = re.sub(r"```(?:json|text)?", "", text or "").strip()
    items: list[str] = []
    numbered = re.compile(r"^\s*(?:\*\*)?\d{1,2}\s*[.)]\s*(?:\*\*)?\s*")
    if any(numbered.match(line) for line in s.splitlines()):
        for line in s.splitlines():
            if numbered.match(line):
                items.append(numbered.sub("", line, count=1))
            elif items and line.strip():
                items[-1] += " " + line.strip()  # suite du prompt précédent
    else:
        i, j = s.find("["), s.rfind("]")
        if i != -1 and j > i:
            try:
                data = json.loads(s[i:j + 1])
                items = [str(x) for x in data] if isinstance(data, list) else []
            except json.JSONDecodeError:
                items = []
        if not items:
            items = [re.sub(r"^\s*[-*•]\s*", "", line) for line in s.splitlines() if len(line.strip()) > 25]
    out, seen = [], set()
    for x in items:
        x = re.sub(r"\s+", " ", x.replace("**", "")).strip().strip('"').strip()
        if len(x.split()) >= 6 and x.lower() not in seen and not x.startswith("{"):
            seen.add(x.lower())
            out.append(x)
    return out


def exec_text_generate(ctx: StepContext) -> dict:
    p = ctx.params
    r = ctx.ref("context")
    context = (r.get("text") if isinstance(r, dict) and r.get("kind") == "text" else None) or str(p.get("context") or "")
    context = context.strip()
    if not context:
        raise StepFailure("Contexte vide : écrivez un thème dans la node (ou branchez un prompt sur « Contexte »)", code="missing_input")
    n = max(1, min(16, int(p.get("count", 5))))
    seed = int(ctx.payload.get("seed") or 0)
    instruction = randomize_instruction(context, n, p)
    prompts: list[str] = []
    raws = []
    for attempt in range(2):  # une seconde tentative (autre seed) si la réponse est incomplète ou illisible
        values = {"prompt": instruction, "max_length": min(4096, 160 * n + 200), "temperature": float(p.get("creativity", 0.9)),
                  "seed": seed + attempt}
        ctx.stage("engine", f"Qwen3-VL 4B : écriture de {n} prompt(s)" + (" — nouvelle tentative" if attempt else ""))
        hist, tpl, client = run_comfy(ctx, "text_generate_default", values, {}, timeout_s=900)
        raw = history_text(hist, tpl.bindings["outputs"]["text"]["node"]) or ""
        raws.append(raw)
        for x in parse_prompt_list(raw):
            if x.lower() not in {y.lower() for y in prompts}:
                prompts.append(x)
        if len(prompts) >= n:
            break
        ctx.log(f"Réponse incomplète ({len(prompts)}/{n} prompts lisibles)")
        ctx.set_engine_ref({"prompt_id": None})
        ctx.job["engine_ref"] = {**(ctx.job.get("engine_ref") or {}), "prompt_id": None}
    if len(prompts) < n:
        raise StepFailure(f"Le modèle n'a écrit que {len(prompts)} prompt(s) lisible(s) sur {n}", code="llm_output",
                          details={"raw": [x[:2000] for x in raws]}, corrective="Relancez (autre seed) ou réduisez le nombre de prompts.")
    prompts = prompts[:n]
    out = ctx.out_path("prompts.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"context": context, "prompts": prompts, "instruction": instruction, "raw_output": raws},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    pid = ctx.add(out, "prompts", {"prompts": prompts, "context": context, "language": p.get("language", "en")})
    return {"outputs": {"prompts": [pid]}, "engine": {**tpl.fingerprint()},
            "report": {"context": context, "prompts": prompts, "count": n, "attempts": len(raws)}}


# ---------------------------------------------------------------------------- OpenAI texte / vision (API Responses)
def _openai_key_and_text_model(ctx: StepContext) -> tuple[str, str]:
    from .adapters import openai_images as O
    from .secrets_store import OPENAI_KEY, SecretStore, load_services

    key = SecretStore(ctx.cfg.config_dir).get(OPENAI_KEY)
    if not key:
        raise StepFailure("Clé d'API OpenAI non configurée", code="openai_key_missing",
                          corrective="Réglages → OpenAI : saisissez votre clé, puis « Tester la clé » — ou choisissez le moteur local")
    s = load_services(ctx.cfg.config_dir)["openai"]
    model = ctx.params.get("openai_model") or s.get("model_text") or O.pick_text_default(s.get("text_models") or []) or "gpt-6.1-sol"
    return key, model


def _claude_key_and_model(ctx: StepContext) -> tuple[str, str]:
    from .adapters import anthropic_api as A
    from .secrets_store import ANTHROPIC_KEY, SecretStore, load_services

    key = SecretStore(ctx.cfg.config_dir).get(ANTHROPIC_KEY)
    if not key:
        raise StepFailure("Clé d'API Claude (Anthropic) non configurée", code="anthropic_key_missing",
                          corrective="Réglages → Claude : saisissez votre clé, puis « Tester la clé » — ou choisissez un autre moteur")
    s = load_services(ctx.cfg.config_dir)["anthropic"]
    model = ctx.params.get("claude_model") or s.get("model") or A.pick_default(s.get("models") or [])
    if not model:
        raise StepFailure("Aucun modèle Claude connu pour cette clé", code="anthropic_model",
                          corrective="Réglages → Claude : « Tester la clé » (la liste des modèles accessibles est lue avec la clé)")
    return key, model


def _vision_input(ctx: StepContext, src: pathlib.Path) -> pathlib.Path:
    """Image envoyée à l'analyse : alpha composé sur gris clair, côté le plus long ≤ 1536 px (coût en jetons maîtrisé)."""
    with Image.open(src) as im:
        im.load()
        rgba = im.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (217, 217, 217, 255))
    bg.alpha_composite(rgba)
    out = bg.convert("RGB")
    if max(out.size) > 1536:
        out.thumbnail((1536, 1536), Image.LANCZOS)
    dest = ctx.dir / "inputs" / "analyse.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest, quality=92)
    return dest


def _props_result(ctx: StepContext, data: dict, engine: dict, extra_report: dict) -> dict:
    from . import props as P

    if not data["parts"]:
        raise StepFailure("Aucune partie extractible détectée dans l'image", code="props_empty",
                          corrective="Essayez « Détaillé », l'autre moteur d'analyse, ou ajoutez des parties à la main dans la node")
    out = ctx.out_path("analysis.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**data, **extra_report}, ensure_ascii=False, indent=2), encoding="utf-8")
    aid = ctx.add(out, "analysis", {"character": data["character"], "parts": data["parts"]})
    prompts = [P.extraction_prompt(x, data["character"], ctx.params.get("extra", "")) for x in data["parts"]]
    return {"outputs": {"analysis": [aid]}, "engine": engine,
            "report": {"character": data["character"], "parts": data["parts"], "count": len(data["parts"]),
                       "extraction_prompts": prompts, **extra_report}}


def exec_props_openai(ctx: StepContext) -> dict:
    from . import props as P
    from .adapters import openai_images as O

    src = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    key, model = _openai_key_and_text_model(ctx)
    img = _vision_input(ctx, src)
    instruction = P.analysis_instruction(ctx.params, structured=True)
    if ctx.cancelled():
        raise Cancelled()
    ctx.stage("engine", f"OpenAI {model} : analyse de l'image (service externe)")
    ctx.log(f"OpenAI Responses : modèle={model} image={img.name} détail=high")
    data, usage = O.respond_json(key, model=model, prompt=instruction, schema=P.OPENAI_SCHEMA, name="props_breakdown", images=[img])
    norm = P.normalize(data, ctx.params)
    return _props_result(ctx, norm, {"engine": "openai", "model": model},
                         {"instruction": instruction, "usage": usage, "raw_parts": len(data.get("parts") or []), "external_service": True})


def exec_props_claude(ctx: StepContext) -> dict:
    from . import props as P
    from .adapters import anthropic_api as A

    src = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    key, model = _claude_key_and_model(ctx)
    img = _vision_input(ctx, src)
    instruction = P.analysis_instruction(ctx.params, structured=True)
    if ctx.cancelled():
        raise Cancelled()
    ctx.stage("engine", f"Claude {model} : analyse de l'image (service externe)")
    ctx.log(f"Anthropic Messages : modèle={model} image={img.name}")
    data, usage = A.respond_json(key, model=model, prompt=instruction, schema=P.OPENAI_SCHEMA, name="props_breakdown", images=[img])
    norm = P.normalize(data, ctx.params)
    return _props_result(ctx, norm, {"engine": "claude", "model": model},
                         {"instruction": instruction, "usage": usage, "raw_parts": len(data.get("parts") or []), "external_service": True})


def exec_props_local(ctx: StepContext) -> dict:
    from . import props as P

    src = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    img = _vision_input(ctx, src)
    instruction = P.analysis_instruction(ctx.params, structured=False)
    seed = int(ctx.payload.get("seed") or 0)
    raws, norm, tpl = [], {"character": "", "parts": []}, None
    for attempt in range(2):  # seconde tentative (autre seed) si aucune partie lisible
        ctx.stage("engine", "Qwen3-VL 4B : analyse de l'image" + (" — nouvelle tentative" if attempt else ""))
        hist, tpl, client = run_comfy(ctx, "vision_generate_default",
                                      {"prompt": instruction, "max_length": 1400, "temperature": 0.4, "seed": seed + attempt},
                                      {"image": img}, timeout_s=900)
        raw = history_text(hist, tpl.bindings["outputs"]["text"]["node"]) or ""
        raws.append(raw)
        norm = P.normalize(P.parse_local(raw), ctx.params)
        if norm["parts"]:
            break
        ctx.set_engine_ref({"prompt_id": None})
    return _props_result(ctx, norm, {**tpl.fingerprint()}, {"instruction": instruction, "raw_output": raws})


def exec_text_openai(ctx: StepContext) -> dict:
    """« Prompts aléatoires » écrits par OpenAI ou Claude (sortie structurée : liste de chaînes)."""
    from .adapters import anthropic_api as A
    from .adapters import openai_images as O

    claude = ctx.job.get("payload", {}).get("capability") == "text.claude"
    p = ctx.params
    r = ctx.ref("context")
    context = ((r.get("text") if isinstance(r, dict) and r.get("kind") == "text" else None) or str(p.get("context") or "")).strip()
    if not context:
        raise StepFailure("Contexte vide : écrivez un thème dans la node (ou branchez un prompt sur « Contexte »)", code="missing_input")
    n = max(1, min(16, int(p.get("count", 5))))
    key, model = _claude_key_and_model(ctx) if claude else _openai_key_and_text_model(ctx)
    name = "Claude" if claude else "OpenAI"
    instruction = randomize_instruction(context, n, p).rsplit("\n", 1)[0] + f"\nReturn exactly {n} prompts."
    schema = {"type": "object", "additionalProperties": False, "required": ["prompts"],
              "properties": {"prompts": {"type": "array", "items": {"type": "string"}}}}
    ctx.stage("engine", f"{name} {model} : écriture de {n} prompt(s) (service externe)")
    respond = A.respond_json if claude else O.respond_json
    data, usage = respond(key, model=model, prompt=instruction, schema=schema, name="image_prompts")
    prompts = [re.sub(r"\s+", " ", str(x)).strip() for x in data.get("prompts") or [] if str(x).strip()][:n]
    if len(prompts) < n:
        raise StepFailure(f"{name} n'a écrit que {len(prompts)} prompt(s) sur {n}", code="llm_output", details={"prompts": prompts})
    out = ctx.out_path("prompts.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"context": context, "prompts": prompts, "instruction": instruction, "model": model},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    pid = ctx.add(out, "prompts", {"prompts": prompts, "context": context, "language": p.get("language", "en")})
    return {"outputs": {"prompts": [pid]}, "engine": {"engine": "claude" if claude else "openai", "model": model},
            "report": {"context": context, "prompts": prompts, "count": n, "usage": usage, "external_service": True}}


EXECUTORS: dict[tuple[str, str], Callable[[StepContext], dict]] = {
    ("image.create", "*"): exec_image_create,
    ("image.edit", "*"): exec_image_edit,
    ("image.edit", "pose"): exec_pose_krea,
    ("image.upscale.seedvr2", "*"): exec_upscale,
    ("image.upscale.esrgan", "*"): exec_upscale,
    ("text.generate", "*"): exec_text_generate,
    ("text.openai", "*"): exec_text_openai,
    ("vision.openai", "*"): exec_props_openai,
    ("text.claude", "*"): exec_text_openai,
    ("vision.claude", "*"): exec_props_claude,
    ("vision.local", "*"): exec_props_local,
    ("image.openai", "*"): exec_openai_image,
    ("image.remove_background", "*"): exec_remove_background,
    ("local", "crop"): exec_crop,
    ("mesh.generate", "*"): exec_mesh_generate,
    ("blender", "repair"): exec_mesh_prepare,
    ("blender", "simplify"): exec_mesh_prepare,
    ("blender", "bake"): exec_mesh_prepare,
    ("rig.mia", "*"): exec_rig_mia,
    ("blender", "bind"): exec_rig_bind,
    ("blender", "articulated_rig"): exec_rig_articulated,
    ("blender", "deform_tests"): exec_validate,
    ("blender", "export"): exec_export,
}


def executor_for(capability: str, step: str):
    return EXECUTORS.get((capability, step)) or EXECUTORS.get((capability, "*"))
