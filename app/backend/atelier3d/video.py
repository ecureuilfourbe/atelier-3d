"""Branche vidéo « MiniMax H3 Ultra » : workflows MiniMax H3 Ultra livrés (app/workflows/comfy/minimax_h3_ultra_user_*).

Deux fournisseurs (video.provider) :
- « comfyui_dedicated » (composants d'installation « video » / « video_ref ») : poids et nodes installés avec Atelier 3D,
  exécution par le ComfyUI dédié, relancé au besoin en profil mémoire réduite ;
- « comfyui_attached » (« video_attach ») : ComfyUI portable existant de l'utilisateur avec nodes natives MiniMax H3 et poids
  locaux FL2VA / Ref2VA (int8) + LoRA turbo → exécution locale, hors ligne une fois lancé.
Pour un ComfyUI rattaché :
- On s'attache à l'instance si elle tourne déjà (jamais d'interruption de travaux étrangers).
- Sinon, et seulement si autorisé (video.may_launch), on la lance avec son propre Python et ses arguments, comme un
  processus appartenant à Atelier 3D (arrêté par STOP.bat), sans modifier ses fichiers.
- Les workflows API proviennent de la conversion officielle du workflow de l'utilisateur (copie importée).
"""
from __future__ import annotations

import json
import math
import pathlib
import subprocess
import time

from .adapters.base import StepFailure
from .executors import EXECUTORS, Services, StepContext, comfy_out, run_comfy
from . import launcher

MODES = ("text_to_video", "image_to_video", "first_last_to_video", "reference_to_video")
SUFFIX = {"text_to_video": "t2v", "image_to_video": "i2v", "first_last_to_video": "flf", "reference_to_video": "r2v"}


def frames_for_seconds(seconds: float) -> int:
    """Grille du modèle : longueur = 17k + 5 images à 24 i/s (formule du workflow utilisateur)."""
    n = max(5, round(seconds * 24))
    return n + (5 - (n % 17)) % 17


def ensure_video(services: Services, ctx: StepContext, low_memory: bool) -> object:
    cfg = services.cfg
    v = cfg.section("video")
    if v.get("status") != "detected":
        raise StepFailure("Accès MiniMax H3 non configuré (aucun ComfyUI vidéo détecté)", code="engine_unavailable")
    if v.get("provider") == "comfyui_dedicated":
        # même ComfyUI que les autres moteurs ; le premier démarrage charge aussi les nodes rgthree / VideoHelperSuite
        return services.ensure_dedicated(ctx, timeout=600, low_memory=low_memory)
    client = services.comfy("video")
    st = launcher.read_state(cfg)
    rec = st.get("video_comfy")
    ours = launcher.owned_alive(rec) is not None
    if client.alive():
        if ours and low_memory and not rec.get("low_memory"):
            q = client.queue()
            if not q["running"] and not q["pending"]:
                ctx.stage("engine_restart", "Relance de VOTRE ComfyUI vidéo (lancé par Atelier 3D) en profil mémoire réduite")
                launcher.stop_rec(rec, "video_comfy")
                time.sleep(3)
            else:
                return client
        else:
            if not ours:
                ctx.log("ComfyUI vidéo déjà lancé hors d'Atelier 3D : attachement, sans contrôle des travaux étrangers")
            return client
    if not v.get("may_launch"):
        raise StepFailure("Votre ComfyUI vidéo n'est pas lancé et le lancement automatique est désactivé", code="engine_unavailable",
                          corrective="Lancez run_nvidia_gpu.bat de votre installation, ou autorisez video.may_launch.")
    comfy_dir = pathlib.Path(v["comfy_dir"])
    port = int(v.get("url", "http://127.0.0.1:8188").rsplit(":", 1)[1].split("/")[0])
    args = [a for a in (v.get("launch_args") or []) if a not in ("--port", "--listen")]
    cmd = [v["python"], "-s", str(comfy_dir / "main.py")] + args + ["--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch"]
    if low_memory:
        cmd += list(v.get("low_memory_args") or [])
    ctx.stage("engine_start", "Lancement de votre ComfyUI MiniMax H3 (profil " + ("mémoire réduite" if low_memory else "standard") + ")")
    rec = launcher.spawn(cmd, cfg.logs_dir / "video_comfyui.log", env={"CUDA_PATH": "", "PYTHONUTF8": "1"}, cwd=str(comfy_dir.parent))
    rec.update({"port": port, "url": f"http://127.0.0.1:{port}", "low_memory": low_memory, "owned": True})
    st = launcher.read_state(cfg)
    st["video_comfy"] = rec
    launcher.write_state(cfg, st)
    t0 = time.time()
    while time.time() - t0 < 600:
        if client.alive():
            return client
        if launcher.owned_alive(rec) is None:
            raise StepFailure("Votre ComfyUI vidéo s'est arrêté au démarrage", code="engine_start",
                              corrective=f"Voir {cfg.logs_dir / 'video_comfyui.log'}")
        ctx.check_cancel()
        time.sleep(2)
    raise StepFailure("Votre ComfyUI vidéo ne répond pas après 10 min", code="engine_start")


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: pathlib.Path) -> dict:
    import imageio_ffmpeg

    nframes, secs = imageio_ffmpeg.count_frames_and_secs(str(path))
    out = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         creationflags=0x08000000)
    info = {"frames": nframes, "duration_s": round(secs, 3), "audio": "Audio:" in out.stderr}
    for line in out.stderr.splitlines():
        if "Video:" in line:
            import re

            m = re.search(r"(\d{2,5})x(\d{2,5})", line)
            f = re.search(r"([\d.]+) fps", line)
            if m:
                info["width"], info["height"] = int(m.group(1)), int(m.group(2))
            if f:
                info["fps"] = float(f.group(1))
    return info


def frame_strip(ctx: StepContext, video: pathlib.Path, info: dict) -> list[str]:
    ids = []
    dur = info.get("duration_s") or 0
    times = [0.0] + [round(dur * k / 4, 2) for k in (1, 2, 3)] + [max(0.0, round(dur - 0.05, 2))]
    for i, t in enumerate(times):
        out = ctx.out_path(f"frame_{i:02d}_{t:05.2f}s.jpg")
        subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)],
                       creationflags=0x08000000, check=False)
        if out.is_file():
            ids.append(ctx.add(out, "video_frame", {"time_s": t}))
    if ids:
        thumb = ctx.out_path("miniature.jpg")
        subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(round(dur / 2, 2)), "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=480:-2", str(thumb)], creationflags=0x08000000, check=False)
        if thumb.is_file():
            ids.insert(0, ctx.add(thumb, "thumbnail"))
    return ids


def exec_video(ctx: StepContext) -> dict:
    p = ctx.params
    mode = p.get("mode", "image_to_video")
    if mode not in MODES:
        raise StepFailure(f"Mode vidéo inconnu : {mode}", code="bad_mode")
    alias = p.get("workflow_id") or "minimax_user"
    base = (ctx.cfg.get("workflows.aliases", {}) or {}).get(alias)
    if not base:
        raise StepFailure(f"Alias vidéo « {alias} » non résolu : aucun workflow MiniMax importé et validé", code="workflow_unresolved")
    wid = f"{base}_{SUFFIX[mode]}"
    if mode == "reference_to_video":
        nslots = len([s for s in ((ctx.ref("references") or {}).get("slots") or []) if s.get("ref") and s["ref"].get("kind") != "missing"])
        wid = f"{base}_r2v" if nslots <= 1 else f"{base}_r2v2"
    try:
        tpl = ctx.templates.get(wid)
    except Exception:
        raise StepFailure(f"Le workflow importé ne fournit pas le mode « {mode} » (template {wid} absent)", code="mode_unavailable")
    prompt = ctx.text("prompt")
    secs = float(p.get("duration_s", 5))
    values = {"prompt": prompt, "seed": int(ctx.payload["seed"]), "width": int(p.get("width", 480)), "height": int(p.get("height", 864)),
              "duration_s": secs}
    if p.get("text_encoder"):
        te = p["text_encoder"]
        if ctx.cfg.get("video.provider") == "comfyui_dedicated" and not (ctx.cfg.home / "models" / "text_encoders" / te).is_file():
            # profil réduit (nvfp4) non téléchargé par l'installateur : l'encodeur du workflow est conservé
            ctx.log(f"Encodeur texte {te} non installé : encodeur du workflow conservé")
        else:
            values["text_encoder"] = te
    if mode in ("image_to_video", "first_last_to_video") and p.get("apply_prompt_guide", True) and not prompt.lstrip().startswith("For the target video"):
        # guide « IMAGE TO VIDEO PROMPTING GUIDE » inclus dans votre workflow : ancre <Picture 1> en première ligne
        values["prompt"] = ("For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n"
                            f"integrated_multimodal_description: [Shot 1] {prompt}")
    values = {k: v for k, v in values.items() if k in tpl.bindings["inputs"]}
    uploads = {}
    refs_desc = []
    if mode in ("image_to_video", "first_last_to_video"):
        uploads["first_frame"] = ctx.ref_file(ctx.ref("image"), prefer=("image",))
    if mode == "first_last_to_video":
        uploads["last_frame"] = ctx.ref_file(ctx.ref("last_image"), prefer=("image",))
    if mode == "reference_to_video":
        refs = ctx.ref("references") or {}
        slots = [s for s in refs.get("slots", []) if s.get("ref") and s["ref"].get("kind") != "missing"]
        maxn = int(tpl.bindings.get("limits", {}).get("max_reference_images", 0))
        if not slots:
            raise StepFailure("Aucune référence image fournie", code="missing_input")
        if len(slots) > maxn:
            raise StepFailure(f"{len(slots)} références mais le workflow importé n'en expose que {maxn}", code="too_many_references")
        tags = []
        for i, s in enumerate(slots, start=1):
            uploads[f"ref_image_{i}"] = ctx.ref_file(s["ref"], prefer=("image",))
            desc = f"<Picture {i}> : {s.get('label') or s.get('id')} ({s.get('role', 'sujet')})" + (f" — {s['description']}" if s.get("description") else "")
            tags.append(desc)
            refs_desc.append({"order": i, "slot": s.get("id"), "role": s.get("role"), "label": s.get("label"), "tag": f"<Picture {i}>"})
        prompt_final = "Références : " + " ; ".join(tags) + ".\n" + prompt
        values["prompt"] = prompt_final
    missing_bind = [k for k in uploads if k not in tpl.bindings["inputs"]]
    if missing_bind:
        raise StepFailure(f"Entrées non exposées par le workflow importé : {', '.join(missing_bind)}", code="unsupported_input")
    hist, tpl2, client = run_comfy(ctx, wid, values, uploads, which="video", timeout_s=4 * 3600)
    vids = comfy_out(ctx, client, hist, tpl2, "video", "video", "video")
    path = ctx.storage.abs(ctx.storage.get_artifact(vids[0])["path"])
    ctx.stage("frames", "Miniature, métadonnées et frames horodatées")
    info = probe(path)
    frames = frame_strip(ctx, path, info)
    meta = {"provider": ctx.cfg.get("video.provider"), "label": ctx.cfg.get("video.label"), "execution": ctx.cfg.get("video.execution"),
            "comfy_dir": ctx.cfg.get("video.comfy_dir"), "comfy_version": ctx.cfg.get("video.version"), "mode": mode,
            "workflow_id": tpl2.id, "workflow_source": tpl2.bindings.get("provenance"), "prompt_original": prompt,
            "prompt_final": values.get("prompt"), "references": refs_desc, "parameters": {k: v for k, v in values.items() if k != "prompt"},
            "frames_requested": frames_for_seconds(secs), "video": info, "note": "Sortie vidéo : aucune armature Blender n'est produite."}
    mp = ctx.out_path("metadata_video.json")
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add(mp, "video_metadata")
    return {"outputs": {"video": vids, "frames": frames}, "engine": {**tpl2.fingerprint(), "prompt_id": ctx.job["engine_ref"]["prompt_id"],
                                                                    "comfy_url": client.base}, "report": meta}


def install(services: Services) -> None:
    services.ensure_video = lambda ctx: ensure_video(services, ctx, bool(ctx.params.get("low_memory", True)))
    EXECUTORS[("video.minimax", "*")] = exec_video
