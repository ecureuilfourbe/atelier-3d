"""Atelier 3D — capture de mouvement depuis une vidéo : MediaPipe Pose Landmarker (Tasks API, mode VIDEO).

Exécuté par le Python où MediaPipe est installé (environnement séparé de l'application). Lit les images avec ffmpeg en flux
(aucune image complète gardée en mémoire), écrit :
  - motion.json (format atelier3d-motion/1 : articulations nommées, mètres, Y haut, +Z avant, +X gauche du personnage),
  - une vidéo de contrôle (squelette détecté dessiné sur la vidéo).
Protocole : arguments JSON (fichier en argument), lignes « @@A3D {json} » sur la sortie standard.

Limites (une seule caméra) : profondeur estimée par le modèle ; le déplacement au sol n'est pas capturé (bassin à l'origine),
seul le contact des pieds avec le sol est reconstruit.
"""
import json
import re
import subprocess
import sys

import numpy as np

# Indices MediaPipe Pose (33 points)
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 2, 5, 7, 8
L_SH, R_SH, L_EL, R_EL, L_WR, R_WR = 11, 12, 13, 14, 15, 16
L_PINKY, R_PINKY, L_INDEX, R_INDEX, L_THUMB, R_THUMB = 17, 18, 19, 20, 21, 22
L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANK, R_ANK, L_HEEL, R_HEEL, L_FOOT, R_FOOT = 23, 24, 25, 26, 27, 28, 29, 30, 31, 32
DRAW = [(L_SH, R_SH), (L_SH, L_EL), (L_EL, L_WR), (R_SH, R_EL), (R_EL, R_WR), (L_SH, L_HIP), (R_SH, R_HIP), (L_HIP, R_HIP),
        (L_HIP, L_KNEE), (L_KNEE, L_ANK), (L_ANK, L_HEEL), (L_HEEL, L_FOOT), (L_ANK, L_FOOT), (R_HIP, R_KNEE), (R_KNEE, R_ANK),
        (R_ANK, R_HEEL), (R_HEEL, R_FOOT), (R_ANK, R_FOOT), (L_WR, L_INDEX), (R_WR, R_INDEX), (NOSE, L_EYE), (NOSE, R_EYE)]


def emit(kind, **data):
    print("@@A3D " + json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def probe(ffmpeg, video):
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", video], capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in r.stderr.splitlines():
        if "Video:" in line:
            size = re.search(r"(\d{2,5})x(\d{2,5})", line)
            fps = re.search(r"([\d.]+) fps", line) or re.search(r"([\d.]+) tbr", line)
            if size:
                return int(size.group(1)), int(size.group(2)), float(fps.group(1)) if fps else 30.0
    raise RuntimeError("flux vidéo illisible")


def gaussian_smooth(a, sigma):
    """Lissage temporel (axe 0) ; bords répétés."""
    if sigma <= 0.05 or len(a) < 3:
        return a
    r = max(1, int(round(sigma * 3)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.concatenate([np.repeat(a[:1], r, 0), a, np.repeat(a[-1:], r, 0)], 0)
    out = np.zeros_like(a)
    for i, w in enumerate(k):
        out += w * pad[i:i + len(a)]
    return out


def fill_gaps(rows):
    """Images sans corps détecté : interpolation linéaire entre les détections voisines."""
    idx = [i for i, r in enumerate(rows) if r is not None]
    if not idx:
        raise RuntimeError("aucun corps détecté dans la vidéo")
    arr = np.array([rows[i] if rows[i] is not None else np.zeros_like(rows[idx[0]]) for i in range(len(rows))], dtype=np.float64)
    for i in range(len(rows)):
        if rows[i] is None:
            prev = max([j for j in idx if j < i], default=None)
            nxt = min([j for j in idx if j > i], default=None)
            if prev is None:
                arr[i] = arr[nxt]
            elif nxt is None:
                arr[i] = arr[prev]
            else:
                t = (i - prev) / (nxt - prev)
                arr[i] = arr[prev] * (1 - t) + arr[nxt] * t
    return arr


def canon(w):
    """Points MediaPipe (repère caméra : x droite, y bas, z s'éloigne) → articulations communes (Y haut, +Z vers la caméra)."""
    p = w * np.array([1.0, -1.0, -1.0])
    P = lambda i: p[:, i]  # noqa: E731
    mid = lambda a, b: (P(a) + P(b)) / 2  # noqa: E731
    hips, neck = mid(L_HIP, R_HIP), mid(L_SH, R_SH)
    # profondeur des oreilles peu fiable (une seule caméra) : direction du cou à moitié ramenée sur l'axe du buste
    ears = mid(L_EAR, R_EAR)
    torso = neck - hips
    torso /= np.maximum(np.linalg.norm(torso, axis=1, keepdims=True), 1e-9)
    hn = ears - neck
    ln = np.linalg.norm(hn, axis=1, keepdims=True)
    blend = 0.5 * hn / np.maximum(ln, 1e-9) + 0.5 * torso
    head = neck + blend / np.maximum(np.linalg.norm(blend, axis=1, keepdims=True), 1e-9) * ln
    up = head - neck
    up /= np.maximum(np.linalg.norm(up, axis=1, keepdims=True), 1e-9)
    c = {"hips": hips, "spine": hips + 0.35 * (neck - hips), "chest": hips + 0.72 * (neck - hips), "neck": neck, "head": head,
         "head_top": head + up * 0.12, "nose": P(NOSE), "l_face": P(L_EAR), "r_face": P(R_EAR)}
    for s, SH, EL, WR, PI, IN, TH, HI, KN, AN, FO in (("l", L_SH, L_EL, L_WR, L_PINKY, L_INDEX, L_THUMB, L_HIP, L_KNEE, L_ANK, L_FOOT),
                                                      ("r", R_SH, R_EL, R_WR, R_PINKY, R_INDEX, R_THUMB, R_HIP, R_KNEE, R_ANK, R_FOOT)):
        c.update({f"{s}_clavicle": neck, f"{s}_shoulder": P(SH), f"{s}_elbow": P(EL), f"{s}_wrist": P(WR),
                  f"{s}_hand_end": (P(PI) + P(IN)) / 2, f"{s}_thumb": P(TH), f"{s}_hip": P(HI), f"{s}_knee": P(KN),
                  f"{s}_ankle": P(AN), f"{s}_toe": P(FO)})
    return c


def main(args):
    import mediapipe as mp

    ffmpeg = args["ffmpeg"]
    w0, h0, fps_src = probe(ffmpeg, args["video"])
    fps = min(fps_src, float(args.get("max_fps", 30))) or 30.0
    side = int(args.get("max_side", 960))
    scale = min(1.0, side / max(w0, h0))
    w, h = max(2, int(w0 * scale) // 2 * 2), max(2, int(h0 * scale) // 2 * 2)
    cmd = [ffmpeg, "-v", "error"]
    if args.get("start_s"):
        cmd += ["-ss", str(args["start_s"])]
    cmd += ["-i", args["video"]]
    if args.get("end_s") and args["end_s"] > (args.get("start_s") or 0):
        cmd += ["-t", str(args["end_s"] - (args.get("start_s") or 0))]
    cmd += ["-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    dec = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc = None
    if args.get("out_overlay"):
        enc = subprocess.Popen([ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart", args["out_overlay"]],
                               stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        Image = None
    emit("stage", stage="detect", message=f"MediaPipe Pose : {w0}×{h0} à {fps:g} i/s, analyse en {w}×{h}")
    with open(args["model"], "rb") as f:  # en mémoire : MediaPipe n'ouvre pas les chemins accentués sous Windows
        model_bytes = f.read()
    opts = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_buffer=model_bytes), running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=1, min_pose_detection_confidence=0.4, min_pose_presence_confidence=0.4, min_tracking_confidence=0.4)
    world, vis = [], []
    n = 0
    frame_bytes = w * h * 3
    with mp.tasks.vision.PoseLandmarker.create_from_options(opts) as det:
        while True:
            buf = dec.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            img = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            res = det.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(img)), int(n * 1000 / fps))
            if res.pose_world_landmarks:
                world.append(np.array([[q.x, q.y, q.z] for q in res.pose_world_landmarks[0]]))
                vis.append(np.array([q.visibility for q in res.pose_landmarks[0]]))
                pts = [(q.x * w, q.y * h) for q in res.pose_landmarks[0]]
            else:
                world.append(None)
                vis.append(None)
                pts = None
            if enc and Image is not None:
                im = Image.fromarray(img.copy())
                if pts:
                    d = ImageDraw.Draw(im)
                    lw = max(2, w // 240)
                    for a, b in DRAW:
                        d.line([pts[a], pts[b]], fill=(40, 220, 120), width=lw)
                    for x, y in pts[11:33]:
                        d.ellipse([x - lw, y - lw, x + lw, y + lw], fill=(255, 80, 60))
                enc.stdin.write(im.tobytes())
            n += 1
            if n % 15 == 0:
                emit("progress", done=n, total=None, message=f"{n} images analysées")
    dec.wait()
    if enc:
        enc.stdin.close()
        enc.wait()
    if n < 2:
        raise RuntimeError("vidéo trop courte ou illisible")
    detected = sum(1 for x in world if x is not None)
    if detected < max(2, n * 0.3):
        raise RuntimeError(f"corps détecté sur {detected} image(s) sur {n} : personnage trop petit, coupé ou caché ?")
    arr = fill_gaps(world)
    sigma = float(args.get("smoothing", 0.5)) * 3.0
    arr = gaussian_smooth(arr, sigma)
    c = canon(arr)
    # contact au sol : point de pied le plus bas à y = 0 à chaque image (lissé), le bassin suit
    feet = np.stack([c[k][:, 1] for k in ("l_ankle", "r_ankle", "l_toe", "r_toe")], 1).min(1) - 0.02
    ground = gaussian_smooth(feet[:, None], max(1.0, sigma))[:, 0]
    for k in c:
        c[k] = c[k] - np.array([0.0, 1.0, 0.0]) * ground[:, None]
    vis_arr = np.array([v for v in vis if v is not None])
    limbs = [L_EL, R_EL, L_WR, R_WR, L_KNEE, R_KNEE, L_ANK, R_ANK]
    low = float((vis_arr[:, limbs] < 0.5).mean()) if len(vis_arr) else 1.0
    names = [k for k in ("hips", "spine", "chest", "neck", "head", "head_top", "nose", "l_face", "r_face", "l_clavicle", "l_shoulder",
                         "l_elbow", "l_wrist", "l_hand_end", "l_thumb", "r_clavicle", "r_shoulder", "r_elbow", "r_wrist", "r_hand_end",
                         "r_thumb", "l_hip", "l_knee", "l_ankle", "l_toe", "r_hip", "r_knee", "r_ankle", "r_toe")]
    pos = np.stack([c[k] for k in names], 1)
    doc = {"format": "atelier3d-motion/1", "source": "mediapipe", "fps": fps, "frames": int(n), "duration_s": round(n / fps, 3),
           "axes": "Y haut, +Z avant, +X gauche du personnage (mètres)", "root_motion": False, "joints": names,
           "positions": np.round(pos, 4).tolist(),
           "quality": {"detected_frames": detected, "interpolated_ratio": round(1 - detected / n, 3), "low_visibility_ratio": round(low, 3),
                       "smoothing_sigma_frames": round(sigma, 2)},
           "video": {"width": w0, "height": h0, "fps": fps_src, "analysed": [w, h]}}
    with open(args["out_motion"], "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    emit("result", frames=n, detected=detected, fps=fps)


if __name__ == "__main__":
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            main(json.load(f))
    except Exception as e:  # noqa: BLE001
        emit("error", message=f"{type(e).__name__} : {e}")
        sys.exit(2)
