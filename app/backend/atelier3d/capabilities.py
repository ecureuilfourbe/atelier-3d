"""Capacités réellement disponibles : moteurs joignables, modèles présents, workflows vérifiés, tests réussis.

Statuts : « disponible » (testé avec succès sur cette machine), « à tester » (installé et vérifié statiquement,
jamais exécuté avec succès ici), « indisponible » (avec raison). « Installé » ne signifie pas « compatible ».
"""
from __future__ import annotations

import json
import pathlib
import threading
import time

from . import config as C
from .adapters.comfyui import ComfyClient
from .adapters.workflows import TemplateRegistry
from .db import Database

_CACHE: dict = {"at": 0.0, "data": None}
_LOCK = threading.Lock()

CAP_TEMPLATES = {
    "image.create": "image_create_default",
    "image.edit": "image_edit_default",
    "image.remove_background": "remove_background_default",
    "mesh.generate": "image_to_mesh_default",
    "image.upscale.seedvr2": "upscale_seedvr2_default",
    "image.upscale.esrgan": "upscale_esrgan_default",
    "text.generate": "text_generate_default",
    "vision.local": "vision_generate_default",
}


def _tested(db: Database, capability: str) -> dict | None:
    r = db.one("SELECT j.id, j.finished_at, j.payload FROM jobs j WHERE j.status = 'succeeded' AND j.kind = 'graph_step' "
               "AND json_extract(j.payload, '$.capability') = ? ORDER BY j.finished_at DESC LIMIT 1", (capability,))
    return {"job_id": r["id"], "at": r["finished_at"]} if r else None


def _tested_step(db: Database, node_type: str, step: str, **params) -> dict | None:
    """Dernière réussite réelle d'une sous-étape précise (ex. liaison « poids automatiques Blender », mode vidéo)."""
    sql = "SELECT id, finished_at FROM jobs WHERE status = 'succeeded' AND kind = 'graph_step' AND node_type = ? AND step = ?"
    args: list = [node_type, step]
    for k, v in params.items():
        sql += f" AND json_extract(payload, '$.params.{k}') = ?"
        args.append(v)
    r = db.one(sql + " ORDER BY finished_at DESC LIMIT 1", tuple(args))
    return {"job_id": r["id"], "at": r["finished_at"]} if r else None


def compute(cfg: C.Config, db: Database, *, refresh: bool = False, comfy_url: str | None = None) -> dict:
    with _LOCK:
        if not refresh and _CACHE["data"] and time.time() - _CACHE["at"] < 15:
            return _CACHE["data"]
    from . import launcher

    st = launcher.read_state(cfg)
    url = comfy_url or (st.get("comfyui") or {}).get("url") or f"http://127.0.0.1:{cfg.section('comfyui').get('port', 8190)}"
    reg = TemplateRegistry(cfg.workflows_dir / "comfy")
    aliases = cfg.get("workflows.aliases", {}) or {}
    caps: dict = {}
    client = ComfyClient(url, timeout=5)
    comfy_alive = cfg.section("comfyui").get("enabled") and client.alive()
    oi, model_lists = None, {}
    if comfy_alive:
        try:
            oi = client.object_info()
            for f in ("diffusion_models", "text_encoders", "vae", "loras", "clip_vision", "background_removal", "geometry_estimation",
                      "upscale_models"):
                model_lists[f] = client.models(f)
        except Exception:
            comfy_alive = False
    engines = {"comfyui_dedicated": {"url": url, "status": "actif" if comfy_alive else ("arrêté" if cfg.section("comfyui").get("enabled") else "non installé"),
                                     "version": cfg.get("comfyui.version") or "v0.38.0"}}
    for cap, alias in CAP_TEMPLATES.items():
        wid = aliases.get(alias)
        entry = {"workflow_alias": alias, "workflow_id": wid}
        if not cfg.section("comfyui").get("enabled"):
            entry.update(status="indisponible", reason="ComfyUI dédié non installé (composants image/3D non choisis)")
        elif not wid:
            entry.update(status="indisponible", reason=f"alias {alias} non résolu (template ou modèles manquants)")
        else:
            tpl = reg.get(wid)
            entry.update(engine=tpl.bindings.get("engine"), label=tpl.label, limits=tpl.bindings.get("limits"))
            if comfy_alive:
                problems = tpl.verify(oi, model_lists)
                if problems:
                    entry.update(status="indisponible", reason="; ".join(problems[:4]))
                else:
                    t = _tested(db, cap)
                    entry.update(status="disponible" if t else "à tester", verified_against_object_info=True, last_success=t)
            else:
                t = _tested(db, cap)
                entry.update(status="à tester" if not t else "disponible", reason="ComfyUI dédié arrêté : démarré automatiquement à la première tâche",
                             last_success=t)
        caps[cap] = entry
    bl = cfg.section("blender")
    bl_ok = bool(bl.get("path") and pathlib.Path(bl["path"]).is_file())
    t = _tested(db, "blender")
    caps["blender"] = {"status": ("disponible" if t else "à tester") if bl_ok else "indisponible", "engine": {"name": "Blender", "version": bl.get("version"), "path": bl.get("path")},
                       "formats": ["blend", "glb", "fbx"], "last_success": t, "reason": None if bl_ok else "Blender introuvable"}
    mia = cfg.section("mia")
    mia_ok = bool(mia.get("enabled") and pathlib.Path(mia.get("python", "")).is_file() and pathlib.Path(mia.get("weights", "")).is_dir())
    t = _tested(db, "rig.mia")
    caps["rig.mia"] = {"status": ("disponible" if t else "à tester") if mia_ok else "indisponible",
                       "engine": {"name": "Make-It-Animatable", "commit": mia.get("commit"), "license": "code MIT, poids Apache-2.0"},
                       "limits": "Humanoïdes (squelette Mixamo 52 os). Pas de serpent/quadrupède. Doigts et cheveux à vérifier visuellement.",
                       "last_success": t, "reason": None if mia_ok else "environnement MIA non installé"}
    caps["rig.skintokens"] = {"status": "indisponible", "reason": "SkinTokens annonce ≥ 14 Go de VRAM (README upstream) ; non installé sur ce profil."}
    caps["rig.unirig"] = {"status": "indisponible", "reason": "UniRig non intégré en V1 (un seul moteur de rigging retenu : MIA)."}
    ta = _tested_step(db, "mesh.rig", "bind", engine="blender_auto")
    caps["rig.blender_auto"] = {"status": ("disponible" if ta else "à tester") if (mia_ok and bl_ok) else "indisponible",
                                "label": "Squelette MIA + poids automatiques Blender — « rig automatique à vérifier »", "last_success": ta}
    tr = _tested_step(db, "mesh.rig", "articulated_rig")
    caps["rig.articulated"] = {"status": ("disponible" if tr else "à tester") if bl_ok else "indisponible",
                               "label": "Armature à pivots pour objet articulé (Blender)", "last_success": tr}
    v = cfg.section("video")
    v_dedicated = v.get("provider") == "comfyui_dedicated"
    vurl = url if v_dedicated else ((st.get("video_comfy") or {}).get("url") or v.get("url"))
    if v.get("status") == "detected":
        vc = ComfyClient(vurl, timeout=3)
        alive = vc.alive()
        modes = {}
        mode_reasons = {}
        base = aliases.get("minimax_user")
        for mode, suf in (("text_to_video", "t2v"), ("image_to_video", "i2v"), ("first_last_to_video", "flf"), ("reference_to_video", "r2v")):
            wdir = cfg.workflows_dir / "comfy" / f"{base}_{suf}"
            ok = bool(base) and (wdir / "workflow.api.json").is_file()
            if ok and v_dedicated:  # poids installés avec Atelier 3D : le mode n'existe que si ses modèles sont présents
                try:
                    req = json.loads((wdir / "bindings.json").read_text(encoding="utf-8")).get("required_models", [])
                except (OSError, ValueError):
                    req = []
                miss = [m["name"] for m in req if not (cfg.models_dir / m["folder"] / m["name"]).is_file()]
                if miss:
                    ok = False
                    mode_reasons[mode] = "modèle non installé : " + ", ".join(miss) + " (relancez INSTALL.bat et cochez ce mode)"
            modes[mode] = ("disponible" if _tested_step(db, "video.generate", "generate", mode=mode) else "à tester") if ok else "indisponible"
        t = _tested(db, "video.minimax")
        caps["video.minimax"] = {"status": ("disponible" if t else "à tester") if base else "indisponible", "label": v.get("label"),
                                 "provider": v.get("provider"), "execution": v.get("execution"),
                                 "execution_label": ("Poids locaux (ComfyUI d'Atelier 3D) : fonctionne hors ligne" if v_dedicated
                                                     else "Poids locaux (ComfyUI local) : fonctionne hors ligne une fois lancé"),
                                 "comfy_dir": v.get("comfy_dir"), "url": vurl, "instance": "active" if alive else "arrêtée (lancement automatique autorisé)" if v.get("may_launch") else "arrêtée",
                                 "modes": modes, "mode_reasons": mode_reasons, "models": v.get("models"), "last_success": t,
                                 "reason": None if base else "workflow MiniMax importé non encore converti/validé"}
    else:
        caps["video.minimax"] = {"status": "indisponible", "reason": v.get("message") or "accès vidéo non configuré"}
    caps["local"] = {"status": "disponible", "engine": {"name": "Pillow"}}
    # OpenAI / ChatGPT Images : service externe, seulement si une clé a été saisie dans Réglages
    from .secrets_store import openai_view

    ov = openai_view(cfg.config_dir)
    t = _tested(db, "image.openai")
    caps["image.openai"] = {
        "status": ("disponible" if t else "à tester") if ov["configured"] else "indisponible",
        "label": "OpenAI / ChatGPT Images (service externe)",
        "engine": {"name": "OpenAI Images API", "model_generate": ov.get("model_generate") or None, "model_edit": ov.get("model_edit") or None},
        "execution": "external",
        "execution_label": "Service externe : prompts et images envoyés à OpenAI, facturés sur votre compte",
        "last_success": t,
        "reason": None if ov["configured"] else "Clé d'API OpenAI non configurée (Réglages → OpenAI)",
    }
    for cap, label in (("vision.openai", "OpenAI — analyse d'image (service externe)"), ("text.openai", "OpenAI — écriture de texte (service externe)")):
        t = _tested(db, cap)
        caps[cap] = {"status": ("disponible" if t else "à tester") if ov["configured"] else "indisponible", "label": label,
                     "engine": {"name": "OpenAI Responses API", "model": ov.get("model_text") or None}, "execution": "external",
                     "last_success": t, "reason": None if ov["configured"] else "Clé d'API OpenAI non configurée (Réglages → OpenAI)"}
    from .motion import capabilities_for

    mot = capabilities_for(cfg)
    for cap, m in mot.items():
        r = db.one("SELECT id, finished_at FROM jobs WHERE status = 'succeeded' AND kind = 'graph_step' AND node_type = 'mesh.animate' "
                   "AND step = 'motion' AND json_extract(result, '$.report_summary.source') = ? ORDER BY finished_at DESC LIMIT 1",
                   ("prompt" if cap == "motion.kimodo" else "video",))
        t = {"job_id": r["id"], "at": r["finished_at"]} if r else None
        caps[cap] = {"status": ("disponible" if t else "à tester") if m["ok"] else "indisponible", "label": m["label"],
                     "engine": m["engine"], "execution": "local", "last_success": t, "reason": m["reason"]}
    any_ok = [c for c in ("motion.kimodo", "motion.mediapipe") if caps[c]["status"] != "indisponible"]
    caps["motion.generate"] = {"status": ("disponible" if any(caps[c]["status"] == "disponible" for c in any_ok) else "à tester") if any_ok else "indisponible",
                               "label": "Mouvement pour « Animer le modèle 3D » (Kimodo : prompt, MediaPipe : vidéo)",
                               "engines": {c: caps[c]["status"] for c in ("motion.kimodo", "motion.mediapipe")},
                               "reason": None if any_ok else "ni Kimodo ni MediaPipe configurés (composant Animation)"}
    from .secrets_store import anthropic_view

    av = anthropic_view(cfg.config_dir)
    for cap, label in (("vision.claude", "Claude — analyse d'image (service externe)"), ("text.claude", "Claude — écriture de texte (service externe)")):
        t = _tested(db, cap)
        caps[cap] = {"status": ("disponible" if t else "à tester") if av["configured"] else "indisponible", "label": label,
                     "engine": {"name": "Anthropic Messages API", "model": av.get("model") or None}, "execution": "external",
                     "last_success": t, "reason": None if av["configured"] else "Clé d'API Claude non configurée (Réglages → Claude)"}
    data = {"capabilities": caps, "engines": engines, "aliases": aliases, "execution_modes": {
        "single": "disponible", "foreach_prompts": "disponible", "foreach_images": "indisponible (non intégré en V1)",
        "zip": "indisponible (non intégré en V1)", "cartesian": "indisponible (non intégré en V1)"}}
    with _LOCK:
        _CACHE.update(at=time.time(), data=data)
    return data


def flat(cfg, db) -> dict:
    return {k: v for k, v in compute(cfg, db)["capabilities"].items()}
