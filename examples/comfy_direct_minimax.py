"""Exemple Python DIRECT ComfyUI (sans l'API du studio) : vidéo MiniMax H3 avec VOTRE workflow importé, mode image → vidéo.

Procédure bas niveau que le studio automatise (atelier3d/video.py) :
/object_info + modèles présents → vérification du template → envoi de l'image (/upload/image) → clone du workflow →
modification des seules entrées mappées (bindings.json) → POST /prompt avec un prompt_id choisi → /ws + /history → /view.

Le ComfyUI vidéo doit déjà tourner (lancé par START.bat / le studio avec le profil basse mémoire, ou par vous).
L'exemple n'interrompt jamais une tâche existante : la demande est simplement ajoutée à la file de ComfyUI.
Pour l'usage courant, préférez le client du studio (examples/claude_video_from_image.py) ou le serveur MCP.

Usage :
  <venv-app>\\python.exe examples\\comfy_direct_minimax.py --image perso.png --out clip.mp4 "the character waves slowly"
  <venv-app>\\python.exe examples\\comfy_direct_minimax.py --check-only        (vérifie seulement le workflow)
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "backend"))

from atelier3d.adapters.comfyui import ComfyClient, history_files, new_prompt_id  # noqa: E402
from atelier3d.adapters.workflows import TemplateRegistry, normalize_combo_paths  # noqa: E402

GUIDE = ("For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n"
         "integrated_multimodal_description: [Shot 1] {prompt}")


def default_url() -> str:
    """URL du ComfyUI vidéo enregistrée par l'installateur (config/atelier3d.json), sinon le port ComfyUI habituel."""
    try:
        from atelier3d import config

        return config.load().get("video.url") or "http://127.0.0.1:8188"
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:8188"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="")
    ap.add_argument("--image", help="première image (PNG/JPG)")
    ap.add_argument("--url", default=default_url())
    ap.add_argument("--seed", type=int, default=7001)
    ap.add_argument("--duration", type=float, default=5.0, help="secondes (le workflow arrondit à 17k+5 images à 24 i/s)")
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=864)
    ap.add_argument("--text-encoder", default=None, help="ex. le fichier nvfp4 si la mémoire manque (profil réduit)")
    ap.add_argument("--no-guide", action="store_true", help="n'ajoute pas l'ancre <Picture 1> du guide de votre workflow")
    ap.add_argument("--out", default="minimax_direct.mp4")
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()

    client = ComfyClient(a.url)
    if not client.alive():
        print(f"ComfyUI vidéo injoignable sur {a.url} : lancez START.bat (ou votre ComfyUI) puis réessayez.")
        return 2
    tpl = TemplateRegistry(ROOT / "app" / "workflows" / "comfy").get("minimax_h3_ultra_user_i2v")
    folders = {m["folder"].split("/")[0] for m in tpl.bindings["required_models"]}
    oi = client.object_info()
    problems = tpl.verify(oi, {f: client.models(f) for f in folders})
    if problems:
        print("Workflow non exécutable sur ce ComfyUI :\n - " + "\n - ".join(problems))
        return 3
    print(f"Workflow {tpl.id} vérifié sur {a.url} (sha256 {tpl.sha256[:12]}…)")
    if a.check_only:
        return 0
    if not a.image or not a.prompt:
        print("Indiquez --image et un prompt (ou --check-only).")
        return 1

    q = client.queue()
    if q["running"] or q["pending"]:
        print(f"File ComfyUI occupée ({len(q['running'])} en cours, {len(q['pending'])} en attente) : la demande attendra son tour.")
    pid = new_prompt_id()
    values = {"prompt": a.prompt if a.no_guide else GUIDE.format(prompt=a.prompt), "seed": a.seed, "duration_s": a.duration,
              "width": a.width, "height": a.height, "filename_prefix": f"exemple_direct/minimax_{pid[:8]}"}
    if a.text_encoder:
        values["text_encoder"] = a.text_encoder
    values["first_frame"] = client.upload_image(pathlib.Path(a.image), f"exemple_direct_{pid[:8]}{pathlib.Path(a.image).suffix.lower()}")
    wf = tpl.instantiate(values)
    for line in normalize_combo_paths(wf, oi):
        print(" ", line)
    t0 = time.time()

    def on_event(kind, data):
        if kind == "progress":
            print(f"  progression {data.get('value')}/{data.get('max')} (node {data.get('node')})", flush=True)
        elif kind in ("execution_start", "execution_success", "execution_error", "execution_interrupted"):
            print(f"  {kind}", flush=True)

    h = client.run(wf, prompt_id=pid, client_id="exemple-direct-minimax", on_event=on_event, timeout_s=4 * 3600)
    files = history_files(h, tpl.bindings["outputs"]["video"]["node"], "video")
    if not files:
        print("ComfyUI n'a renvoyé aucune vidéo (voir son journal).")
        return 4
    out = client.download(files[0], pathlib.Path(a.out))
    print(f"Vidéo : {out.resolve()}  ({time.time() - t0:.0f} s, prompt_id {pid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
