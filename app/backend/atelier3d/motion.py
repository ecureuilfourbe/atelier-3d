"""Animation d'un modèle riggé : mouvement depuis un prompt (Kimodo, kimodo.cpp) ou une vidéo (MediaPipe Pose), puis transfert
sur le squelette du modèle (Blender, script motion_retarget.py).

Format commun du mouvement (« atelier3d-motion/1 », motion.json) : positions 3D d'articulations nommées par fonction (bassin,
épaules, coudes…) par image, en mètres, Y vers le haut, personnage tourné vers +Z, sa gauche vers +X. Le transfert aligne la
direction de chaque os du modèle sur le segment correspondant (indépendant des proportions et des axes du squelette source).
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import subprocess
import time

import numpy as np

from .adapters.base import Cancelled, StepFailure
from .executors import EXECUTORS, StepContext, blender

CREATE_NO_WINDOW = 0x08000000
FPS_KIMODO = 30

# Squelette SOMA-30 prédit par Kimodo (noms, parents, décalages au repos en mètres) : définitions NVIDIA Kimodo (Apache-2.0),
# telles que reprises par kimodo.cpp (src/skeleton.hpp).
SOMA30_NAMES = [
    "Hips", "Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head", "Jaw", "LeftEye", "RightEye", "LeftShoulder", "LeftArm",
    "LeftForeArm", "LeftHand", "LeftHandThumbEnd", "LeftHandMiddleEnd", "RightShoulder", "RightArm", "RightForeArm", "RightHand",
    "RightHandThumbEnd", "RightHandMiddleEnd", "LeftLeg", "LeftShin", "LeftFoot", "LeftToeBase", "RightLeg", "RightShin",
    "RightFoot", "RightToeBase"]
SOMA30_PARENTS = [-1, 0, 1, 2, 3, 4, 5, 6, 6, 6, 3, 10, 11, 12, 13, 13, 3, 16, 17, 18, 19, 19, 0, 22, 23, 24, 0, 26, 27, 28]
SOMA30_OFFSETS = np.array([
    [0.0, 0.0, 0.0], [-0.00013727, 0.0500376256, -0.00053726669], [-1.86574103e-09, 0.0712530139, -0.000298248546],
    [-5.75188398e-09, 0.0755006305, -0.00815970992], [-0.00181676517, 0.263112953, -0.00553348292],
    [-2.85102231e-08, 0.0770939664, 0.0230258546], [-4.5975437e-08, 0.0612891595, 0.0195370861],
    [2.63687901e-05, 0.0047559225, 0.0309494062], [0.0320638079, 0.0538020513, 0.0758688308],
    [-0.0322244017, 0.05361869, 0.0755823359], [0.0162165175, 0.232371641, 0.0511341324],
    [0.149198457, 2.19397873e-08, -0.0550232576], [0.287393078, 2.50268389e-09, -2.58787737e-05],
    [0.270939812, -7.06625108e-09, 2.60897248e-05], [0.122686267, -0.0322017573, 0.0483306876],
    [0.190119595, -0.00312878387, -0.000339570373], [-0.0138011824, 0.231803086, 0.0521415786],
    [-0.150371962, 1.17387901e-07, -0.0554560437], [-0.287366393, 1.87628082e-08, -2.59709359e-05],
    [-0.271336198, -1.16767401e-09, 2.61269368e-05], [-0.122642483, -0.0321145448, 0.0480403904],
    [-0.190005945, -0.00306615542, -0.0003157343], [0.10043214, -0.0843452671, 0.0259565473],
    [-1e-08, -0.432217537, -0.00802912805], [1e-08, -0.421550959, -0.0348152298],
    [0.0, -0.0505947206, 0.132315294], [-0.10047278, -0.0829525995, 0.0262031695],
    [1e-08, -0.433622059, -0.00805555828], [2e-08, -0.421173943, -0.0347839785],
    [-3.42907669e-09, -0.0507960932, 0.132841956]], dtype=np.float64)

# Articulations communes (positions) utilisées par le transfert ; gauche/droite = du personnage.
CANON = ["hips", "spine", "chest", "neck", "head", "head_top", "nose", "l_face", "r_face",
         "l_clavicle", "l_shoulder", "l_elbow", "l_wrist", "l_hand_end", "l_thumb",
         "r_clavicle", "r_shoulder", "r_elbow", "r_wrist", "r_hand_end", "r_thumb",
         "l_hip", "l_knee", "l_ankle", "l_toe", "r_hip", "r_knee", "r_ankle", "r_toe"]


# ---------------------------------------------------------------------------- Kimodo (SOMA-30) → articulations communes

def quat_xyzw_to_mat(q: np.ndarray) -> np.ndarray:
    q = q / np.maximum(np.linalg.norm(q, axis=-1, keepdims=True), 1e-12)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    m = np.empty(q.shape[:-1] + (3, 3))
    m[..., 0, 0] = 1 - 2 * (y * y + z * z)
    m[..., 0, 1] = 2 * (x * y - z * w)
    m[..., 0, 2] = 2 * (x * z + y * w)
    m[..., 1, 0] = 2 * (x * y + z * w)
    m[..., 1, 1] = 1 - 2 * (x * x + z * z)
    m[..., 1, 2] = 2 * (y * z - x * w)
    m[..., 2, 0] = 2 * (x * z - y * w)
    m[..., 2, 1] = 2 * (y * z + x * w)
    m[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def soma_fk(root: np.ndarray, quats: np.ndarray) -> np.ndarray:
    """Positions globales [T, 30, 3] : rotation locale relative au repos (décalages ci-dessus), translation du bassin."""
    rot = quat_xyzw_to_mat(quats)
    T, J = quats.shape[:2]
    g = np.zeros((T, J, 3, 3))
    p = np.zeros((T, J, 3))
    for j in range(J):
        par = SOMA30_PARENTS[j]
        if par < 0:
            g[:, j], p[:, j] = rot[:, j], root
        else:
            g[:, j] = g[:, par] @ rot[:, j]
            p[:, j] = p[:, par] + g[:, par] @ SOMA30_OFFSETS[j]
    return p


def read_kimodo_raw(out_dir: pathlib.Path) -> tuple[np.ndarray, np.ndarray]:
    root = np.fromfile(out_dir / "root_positions.f32", dtype="<f4").astype(np.float64)
    rot = np.fromfile(out_dir / "local_rotations_xyzw.f32", dtype="<f4").astype(np.float64)
    T = root.size // 3
    if T < 2 or rot.size != T * len(SOMA30_NAMES) * 4:
        raise StepFailure(f"Sortie Kimodo inattendue ({root.size} / {rot.size} valeurs) : modèle SOMA-30 attendu", code="kimodo_output")
    return root.reshape(T, 3), rot.reshape(T, len(SOMA30_NAMES), 4)


def soma_to_canon(pos: np.ndarray) -> dict[str, np.ndarray]:
    ix = {n: i for i, n in enumerate(SOMA30_NAMES)}
    P = lambda n: pos[:, ix[n]]  # noqa: E731
    head, neck2 = P("Head"), P("Neck2")
    up = head - neck2
    up /= np.maximum(np.linalg.norm(up, axis=-1, keepdims=True), 1e-9)
    c = {"hips": P("Hips"), "spine": P("Spine2"), "chest": P("Chest"), "neck": P("Neck1"), "head": head,
         "head_top": head + up * 0.15, "nose": P("Jaw"), "l_face": P("LeftEye"), "r_face": P("RightEye")}
    for s, S in (("l", "Left"), ("r", "Right")):
        c.update({f"{s}_clavicle": P(f"{S}Shoulder"), f"{s}_shoulder": P(f"{S}Arm"), f"{s}_elbow": P(f"{S}ForeArm"),
                  f"{s}_wrist": P(f"{S}Hand"), f"{s}_hand_end": P(f"{S}HandMiddleEnd"), f"{s}_thumb": P(f"{S}HandThumbEnd"),
                  f"{s}_hip": P(f"{S}Leg"), f"{s}_knee": P(f"{S}Shin"), f"{s}_ankle": P(f"{S}Foot"), f"{s}_toe": P(f"{S}ToeBase")})
    return c


def write_motion(path: pathlib.Path, canon: dict[str, np.ndarray], fps: float, source: str, *, root_motion: bool,
                 meta: dict | None = None) -> dict:
    names = [n for n in CANON if n in canon]
    arr = np.stack([canon[n] for n in names], axis=1)  # [T, J, 3]
    doc = {"format": "atelier3d-motion/1", "source": source, "fps": fps, "frames": int(arr.shape[0]),
           "duration_s": round(arr.shape[0] / fps, 3), "axes": "Y haut, +Z avant, +X gauche du personnage (mètres)",
           "root_motion": root_motion, "joints": names, "positions": np.round(arr, 4).tolist(), **(meta or {})}
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


# ---------------------------------------------------------------------------- processus externes

def run_process(ctx: StepContext, cmd: list[str], *, cwd: str | None = None, env: dict | None = None, timeout_s: float = 1800,
                on_line=None) -> str:
    """Processus externe journalisé (sortie dans le journal de l'étape), annulable ; retourne la fin de la sortie."""
    full_env = {**os.environ, **(env or {})}
    tail: list[str] = []
    t0 = time.time()
    with open(ctx.log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write("\n$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")
        proc = subprocess.Popen(cmd, cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW, text=True, encoding="utf-8", errors="replace", bufsize=1)
        try:
            for line in proc.stdout:
                log.write(line)
                tail = (tail + [line.rstrip()])[-40:]
                if on_line:
                    on_line(line)
                if ctx.cancelled():
                    proc.kill()
                    raise Cancelled("Annulé pendant le calcul du mouvement")
                if time.time() - t0 > timeout_s:
                    proc.kill()
                    raise StepFailure(f"Délai dépassé ({int(timeout_s)} s)", code="timeout")
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
    if proc.returncode != 0:
        raise StepFailure(f"{pathlib.Path(cmd[0]).name} a échoué (code {proc.returncode}) : " + " | ".join(tail[-6:]), code="motion_engine",
                          details={"tail": tail})
    return "\n".join(tail)


# ---------------------------------------------------------------------------- étape 1 : mouvement

def _source(ctx: StepContext) -> str:
    src = ctx.params.get("source") or "auto"
    has_prompt = (ctx.ref("prompt") or {}).get("kind") not in (None, "missing")
    has_video = (ctx.ref("video") or {}).get("kind") not in (None, "missing")
    if src == "auto":
        if has_prompt and has_video:
            raise StepFailure("Prompt ET vidéo branchés : choisissez « Source du mouvement »", code="ambiguous_source")
        src = "video" if has_video else "prompt"
    if src == "prompt" and not has_prompt:
        raise StepFailure("Source « prompt » mais aucun prompt branché", code="missing_input")
    if src == "video" and not has_video:
        raise StepFailure("Source « vidéo » mais aucune vidéo branchée", code="missing_input")
    return src


def exec_motion(ctx: StepContext) -> dict:
    return motion_kimodo(ctx) if _source(ctx) == "prompt" else motion_mediapipe(ctx)


def motion_kimodo(ctx: StepContext) -> dict:
    k = (ctx.cfg.section("animation") or {}).get("kimodo") or {}
    exe, model, text = (pathlib.Path(k.get(x) or "") for x in ("exe", "model", "text_model"))
    if not (k.get("exe") and exe.is_file() and model.is_file() and text.is_file()):
        raise StepFailure("Kimodo (kimodo.cpp) non configuré : exécutable ou modèles introuvables", code="engine_unavailable",
                          corrective="Relancez INSTALL.bat (composant Animation) ou indiquez animation.kimodo dans la configuration")
    p = ctx.params
    lines = [x.strip() for x in ctx.text("prompt").splitlines() if x.strip()]
    if not lines:
        raise StepFailure("Prompt de mouvement vide", code="missing_input")
    total = int(round(float(p.get("duration_s", 4)) * FPS_KIMODO))
    steps = int(p.get("diffusion_steps", 100))
    seed = int(ctx.payload.get("seed") or 0) % (2**31)
    out = ctx.dir / "outputs" / "kimodo"
    out.mkdir(parents=True, exist_ok=True)
    # outil C++ (arguments en code page ANSI) : travail dans un dossier ASCII du runtime, résultats recopiés ensuite
    runtime = pathlib.Path(ctx.cfg.get("paths.runtime") or out)
    work = runtime / "tmp" / "kimodo" / ctx.job["id"] if str(runtime).isascii() else out
    work.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), str(model), str(text)]
    if len(lines) == 1:
        (work / "prompt.txt").write_text(lines[0], encoding="utf-8")
        cmd += [str(work / "prompt.txt"), str(max(2, min(300, total))), str(steps), str(seed), str(work)]
    else:  # un prompt par ligne : enchaînement (transition de 5 images), durée partagée
        per = max(12, min(300, total // len(lines)))
        cmd += ["--sequence", "5", str(steps), str(seed), str(work)]
        for i, line in enumerate(lines, start=1):
            f = work / f"segment-{i:02d}.prompt.txt"
            f.write_text(line, encoding="utf-8")
            cmd += [str(per), str(f)]
    ctx.services.switch_gpu("kimodo", ctx)  # libère d'abord la VRAM tenue par ComfyUI
    ctx.stage("engine", f"Kimodo : {len(lines)} prompt(s), {total / FPS_KIMODO:.1f} s, {steps} étapes de diffusion")
    bin_dir = k.get("bin_dir") or str(exe.parent)
    env = {"KIMODO_BACKEND": k.get("backend", "vulkan"), "KIMODO_TEXT_LAYER_CHUNK": str(k.get("text_layer_chunk", 8)),
           "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}
    t0 = time.time()
    run_process(ctx, cmd, cwd=k.get("dir") or str(exe.parent), env=env, timeout_s=3600)
    if work != out:
        import shutil

        for f in work.iterdir():
            if f.is_file():
                shutil.copy2(f, out / f.name)
        shutil.rmtree(work, ignore_errors=True)
    root, rot = read_kimodo_raw(out)
    canon = soma_to_canon(soma_fk(root, rot))
    mp = ctx.out_path("motion.json")
    doc = write_motion(mp, canon, FPS_KIMODO, "kimodo", root_motion=True,
                       meta={"prompts": lines, "model": model.name, "diffusion_steps": steps, "seed": seed})
    mid = ctx.add(mp, "motion", {"source": "kimodo", "frames": doc["frames"], "fps": FPS_KIMODO, "prompts": lines})
    raw = [ctx.add(out / n, "kimodo_raw") for n in ("root_positions.f32", "local_rotations_xyzw.f32")]
    return {"outputs": {"motion": [mid], "raw": raw},
            "engine": {"engine": "Kimodo (kimodo.cpp)", "model": model.name, "text_model": text.name, "backend": env["KIMODO_BACKEND"]},
            "stats": {"seconds": round(time.time() - t0, 1)},
            "report": {"source": "prompt", "prompts": lines, "frames": doc["frames"], "fps": FPS_KIMODO, "seed": seed,
                       "note": "Mouvement généré par IA (squelette SOMA) : à vérifier visuellement, surtout mains et contacts au sol."}}


def motion_mediapipe(ctx: StepContext) -> dict:
    m = (ctx.cfg.section("animation") or {}).get("mediapipe") or {}
    py, model = pathlib.Path(m.get("python") or ""), pathlib.Path(m.get("model") or "")
    if not (m.get("python") and py.is_file() and model.is_file()):
        raise StepFailure("MediaPipe non configuré : Python avec mediapipe ou modèle pose_landmarker introuvable", code="engine_unavailable",
                          corrective="Relancez INSTALL.bat (composant Animation) ou indiquez animation.mediapipe dans la configuration")
    import imageio_ffmpeg

    video = ctx.ref_file(ctx.ref("video"), prefer=("video",))
    p = ctx.params
    out = ctx.dir / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    args = {"video": str(video), "model": str(model), "ffmpeg": imageio_ffmpeg.get_ffmpeg_exe(), "out_motion": str(out / "motion.json"),
            "out_overlay": str(out / "controle_pose.mp4"), "smoothing": float(p.get("smoothing", 0.5)),
            "start_s": float(p.get("start_s", 0) or 0), "end_s": float(p.get("end_s", 0) or 0), "max_fps": 30, "max_side": 960}
    ap = out / "mediapipe_args.json"
    ap.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
    script = ctx.cfg.dist / "app" / "engines" / "motion_runner" / "mediapipe_pose.py"
    result: dict = {}

    def on_line(line: str):
        if line.startswith("@@A3D "):
            try:
                ev = json.loads(line[6:])
            except ValueError:
                return
            if ev.get("type") == "stage":
                ctx.stage(ev.get("stage", "mediapipe"), ev.get("message", ""))
            elif ev.get("type") == "progress":
                ctx.progress(message=ev.get("message"))
            elif ev.get("type") == "result":
                result.update(ev)
            elif ev.get("type") == "error":
                result["error"] = ev.get("message")

    ctx.stage("engine", "MediaPipe Pose : détection du corps image par image (processeur)")
    t0 = time.time()
    try:
        run_process(ctx, [str(py), str(script), str(ap)], env={"PYTHONUTF8": "1", "TF_CPP_MIN_LOG_LEVEL": "2"}, timeout_s=3600,
                    on_line=on_line)
    except StepFailure as e:
        if result.get("error"):
            raise StepFailure(f"MediaPipe : {result['error']}", code="mediapipe_failed") from e
        raise
    mp = out / "motion.json"
    doc = json.loads(mp.read_text(encoding="utf-8"))
    mid = ctx.add(mp, "motion", {"source": "mediapipe", "frames": doc["frames"], "fps": doc["fps"], "quality": doc.get("quality")})
    overlay = out / "controle_pose.mp4"
    outputs = {"motion": [mid]}
    if overlay.is_file():
        outputs["overlay"] = [ctx.add(overlay, "pose_overlay")]
    q = doc.get("quality") or {}
    warnings = []
    if q.get("interpolated_ratio", 0) > 0.1:
        warnings.append(f"{q['interpolated_ratio']:.0%} des images sans corps détecté (interpolées)")
    if q.get("low_visibility_ratio", 0) > 0.25:
        warnings.append("Membres souvent peu visibles (hors champ ou cachés) : mouvement approximatif sur ces zones")
    return {"outputs": outputs, "engine": {"engine": "MediaPipe Pose Landmarker", "model": model.name},
            "stats": {"seconds": round(time.time() - t0, 1)},
            "report": {"source": "video", "frames": doc["frames"], "fps": doc["fps"], "quality": q, "warnings": warnings,
                       "note": "Capture depuis une seule caméra : profondeur estimée, déplacement au sol non capturé (animation sur place)."}}


# ---------------------------------------------------------------------------- étape 2 : transfert sur le modèle

def _rigged_blend(ctx: StepContext, ref: dict) -> pathlib.Path:
    v = ctx.ref_variant(ref)
    if v:
        for key in ("rigged_mesh",):
            for aid in v["outputs"].get(key, []):
                a = ctx.storage.get_artifact(aid)
                if a["path"].endswith(".blend"):
                    return ctx.storage.abs(a["path"])
    f = ctx.ref_file(ref, prefer=("rigged_mesh", "mesh"))
    if f.suffix.lower() in (".blend", ".glb", ".gltf", ".fbx"):
        return f
    raise StepFailure("Modèle riggé attendu (.blend, .glb ou .fbx avec squelette)", code="missing_input")


def exec_retarget(ctx: StepContext) -> dict:
    import imageio_ffmpeg

    prev = ctx.ref_variant(ctx.ref("previous"))
    if not prev or not prev["outputs"].get("motion"):
        raise StepFailure("Mouvement manquant (étape précédente)", code="missing_input")
    motion = ctx.storage.abs(ctx.storage.get_artifact(prev["outputs"]["motion"][0])["path"])
    rig = _rigged_blend(ctx, ctx.ref("rigged_mesh"))
    ctx.services.switch_gpu("blender", ctx)
    res = blender(ctx, "motion_retarget.py", {"rig_path": str(rig), "motion_path": str(motion), "in_place": bool(ctx.params.get("in_place", False)),
                                              "ffmpeg": imageio_ffmpeg.get_ffmpeg_exe(), "preview_res": 540})
    glb_id = ctx.add(pathlib.Path(res["glb"]), "rigged_mesh", {"animated": True, "frames": res.get("frames")})
    blend_id = ctx.add(pathlib.Path(res["blend"]), "rigged_blend", {"animated": True})
    outputs = {"rigged_mesh": [glb_id, blend_id]}
    if res.get("fbx"):
        outputs["fbx"] = [ctx.add(pathlib.Path(res["fbx"]), "animated_fbx")]
    if res.get("video"):
        outputs["video"] = [ctx.add(pathlib.Path(res["video"]), "video")]
    frames = [ctx.add(pathlib.Path(x), "video_frame") for x in res.get("frames_png", [])]
    if frames:
        outputs["frames"] = frames
    rep = ctx.out_path("rapport_animation.json")
    rep.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    outputs["report"] = [ctx.add(rep, "report")]
    return {"outputs": outputs, "engine": {"engine": "Blender", "version": res.get("blender_version"), "script": "motion_retarget.py"},
            "report": {**res, "status": "animation à vérifier"}}


EXECUTORS[("motion.generate", "*")] = exec_motion
EXECUTORS[("blender", "retarget")] = exec_retarget


def capabilities_for(cfg) -> dict[str, dict]:
    """Statut statique (fichiers présents) ; « disponible » une fois une étape réussie (calculé par capabilities)."""
    a = cfg.section("animation") or {}
    k, m = a.get("kimodo") or {}, a.get("mediapipe") or {}
    k_ok = all(k.get(x) and pathlib.Path(k[x]).is_file() for x in ("exe", "model", "text_model"))
    m_ok = all(m.get(x) and pathlib.Path(m[x]).is_file() for x in ("python", "model"))
    return {
        "motion.kimodo": {"ok": k_ok, "label": "Kimodo — mouvement depuis un prompt (kimodo.cpp, local)",
                          "engine": {"name": "Kimodo (kimodo.cpp)", "model": pathlib.Path(k.get("model") or "").name or None, "dir": k.get("dir")},
                          "reason": None if k_ok else "kimodo.cpp (kmd-generate + modèles GGUF) introuvable"},
        "motion.mediapipe": {"ok": m_ok, "label": "MediaPipe Pose — mouvement depuis une vidéo (local, processeur)",
                             "engine": {"name": "MediaPipe Pose Landmarker", "model": pathlib.Path(m.get("model") or "").name or None},
                             "reason": None if m_ok else "Python avec mediapipe ou modèle pose_landmarker introuvable"},
    }
