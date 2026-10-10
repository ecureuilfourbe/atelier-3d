"""Outil de conversion : crée app/workflows/comfy/minimax_h3_ultra_user_<mode>/ à partir des JSON API produits par le
frontend officiel de l'instance MiniMax de l'utilisateur (app.graphToPrompt), avec bindings et provenance."""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

SRC = pathlib.Path(sys.argv[1])  # dossier contenant minimax_<mode>.api.json / .visual.json
DEST = pathlib.Path(sys.argv[2])  # app/workflows/comfy
LORA_OK = "minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors"

MODES = {
    "t2v": {"label": "texte → vidéo", "cond": "1983", "prompt": ("1983", "prompt"), "seed": "30", "dur": "28", "vc": "1980", "lora": "1981", "uploads": {}},
    "i2v": {"label": "image → vidéo", "cond": "2024", "prompt": ("2024", "prompt"), "seed": "2020", "dur": "2021", "vc": "2030", "lora": "2017",
            "uploads": {"first_frame": "2037"}},
    "flf": {"label": "début/fin → vidéo", "cond": "2024", "prompt": ("2024", "prompt"), "seed": "2020", "dur": "2021", "vc": "2030", "lora": "2017",
            "uploads": {"first_frame": "2037", "last_frame": "2048"}},
    "r2v": {"label": "références → vidéo (1 image)", "cond": "2112", "prompt": ("2124", "value"), "seed": "2098", "dur": "2097", "vc": "2110", "lora": "2095",
            "uploads": {"ref_image_1": "2099"}},
    "r2v2": {"label": "références → vidéo (2 images)", "cond": "2112", "prompt": ("2124", "value"), "seed": "2098", "dur": "2097", "vc": "2110", "lora": "2095",
             "uploads": {"ref_image_1": "2099", "ref_image_2": "2100"}},
}

for mode, m in MODES.items():
    api = json.loads((SRC / f"minimax_{mode}.api.json").read_text(encoding="utf-8"))
    d = DEST / f"minimax_h3_ultra_user_{mode}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workflow.api.json").write_text(json.dumps(api, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(SRC / f"minimax_{mode}.visual.json", d / "workflow.visual.json")
    inputs = {
        "prompt": {"node": m["prompt"][0], "input": m["prompt"][1], "type": "STRING"},
        "seed": {"node": m["seed"], "input": "noise_seed", "type": "INT"},
        "duration_s": {"node": m["dur"], "input": "value", "type": "FLOAT", "description": "secondes ; le workflow convertit en 17k+5 images à 24 i/s"},
        "width": {"node": m["cond"], "input": "width", "type": "INT", "replaces_link": True},
        "height": {"node": m["cond"], "input": "height", "type": "INT", "replaces_link": True},
        "filename_prefix": {"node": m["vc"], "input": "filename_prefix", "type": "STRING"},
        "turbo_lora": {"node": m["lora"], "input": "lora_1", "type": "POWER_LORA",
                       "default": {"on": True, "lora": LORA_OK, "strength": 1},
                       "description": "Correction documentée : le workflow importé nomme « minimax_h3_turbo_4step_ckpt500_comfyui_pruned » ; le fichier présent "
                                      "(et utilisé par la génération réussie du 30/09/2026) est « …_pruned_comfyui ». Sans correction, rgthree ignorerait la LoRA."},
        "text_encoder": {"node": "12", "input": "clip_name", "type": "STRING",
                         "description": "Défaut du workflow : int8 (25 Go). Profil réduit : nvfp4_awq (14,6 Go), utilisé par la génération réussie du 30/09."},
    }
    for k, nid in m["uploads"].items():
        inputs[k] = {"node": nid, "input": "image", "type": "IMAGE_UPLOAD"}
    nref = sum(1 for k in m["uploads"] if k.startswith("ref_image"))
    b = {
        "id": f"minimax_h3_ultra_user_{mode}",
        "label": f"MiniMax H3 Ultra (votre workflow) — {m['label']}",
        "capability": "video.minimax",
        "engine": {"name": "MiniMax H3 (" + ("Ref2VA" if mode.startswith("r2v") else "FL2VA") + ", int8, LoRA turbo 4 steps)",
                   "runtime": "votre ComfyUI portable 0.30.2 (E:\\AI\\VidGen) — poids locaux",
                   "model": "minimax_h3_ref2va_pruned_int8_convrot" if mode.startswith("r2v") else "minimax_h3_fl2va_pruned_int8_convrot"},
        "provenance": {
            "source": "MINIMAX_H3_ULTRA_+TURBO-LORA_WORKFLOW.json (copie importée de E:\\AI\\VidGen\\ComfyUI_windows_portable, pack Aitrepreneur)",
            "conversion": "groupes activés comme le ferait « Fast Groups Bypasser » (installer/tools/minimax_mode_variants.py), puis app.graphToPrompt() "
                          "du frontend 1.47.12 de VOTRE ComfyUI (custom nodes rgthree, VHS, KJ, Spectrum chargés)",
            "configuration": "reproduit la génération réussie du 30/09/2026 : LoRA turbo, MiniMaxH3SigmaShift (12,19 / 3), euler/beta 8 steps ; "
                             "Spectrum et Patch Sage contournés ; piste audio du modèle décodée et muxée dans le MP4",
        },
        "inputs": inputs,
        "outputs": {"video": {"node": m["vc"], "kind": "video"}},
        "required_models": [
            {"folder": "diffusion_models", "name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors" if mode.startswith("r2v") else "minimax_h3_fl2va_pruned_int8_convrot.safetensors"},
            {"folder": "vae", "name": "minimax_h3_video_vae_fp16.safetensors"},
            {"folder": "vae", "name": "minimax_h3_audio_vae_fp32.safetensors"},
            {"folder": "loras", "name": LORA_OK},
        ],
        "limits": {"max_reference_images": nref, "fps": 24, "frame_grid": "17k+5",
                   "note": "Modes exposés par votre workflow : texte, image, début/fin, références (1 ou 2 images ; le modèle Ref2VA en accepte jusqu'à 9, "
                           "votre workflow en câble 2). Références vidéo/audio présentes dans le workflow mais non exposées en V1."},
    }
    (d / "bindings.json").write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    print("créé", d.name)
