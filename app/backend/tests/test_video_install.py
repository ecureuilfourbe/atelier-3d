"""Vidéo MiniMax H3 installée avec Atelier 3D (composants « video » / « video_ref ») : configuration, capacités par mode,
client ComfyUI utilisé. Sans réseau ni GPU (ports inutilisés).

  <venv-app>\\python.exe app\\backend\\tests\\test_video_install.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT.parents[1]
sys.path.insert(0, str(ROOT))

from atelier3d import config as C, setup_install  # noqa: E402
from atelier3d.db import get_db, reset_db_singleton  # noqa: E402
from atelier3d.executors import Services  # noqa: E402

FL2VA_SET = [("diffusion_models", "minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
             ("text_encoders", "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"),
             ("vae", "minimax_h3_video_vae_fp16.safetensors"), ("vae", "minimax_h3_audio_vae_fp32.safetensors"),
             ("loras", "minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors")]


def install(components: str, models=FL2VA_SET) -> C.Config:
    home = pathlib.Path(tempfile.mkdtemp())
    for folder, name in models:
        (home / "models" / folder).mkdir(parents=True, exist_ok=True)
        (home / "models" / folder / name).write_bytes(b"")
    argv = sys.argv
    sys.argv = ["setup_install", "--home", str(home), "--runtime", str(home / "runtime"), "--dist", str(DIST), "--components", components]
    try:
        assert setup_install.main() == 0
    finally:
        sys.argv = argv
    raw = json.loads((home / "config" / "atelier3d.json").read_text(encoding="utf-8"))
    raw["comfyui"]["port"] = 8999  # aucun ComfyUI réel interrogé
    return C.Config(home=home, raw=raw)


def test_setup_dedicated_video():
    cfg = install("image,video")
    v = cfg.section("video")
    assert v["status"] == "detected" and v["provider"] == "comfyui_dedicated", v
    assert v["comfy_dir"].endswith("ComfyUI") and v["python"].endswith("python.exe")
    assert "minimax_h3_fl2va_pruned_int8_convrot.safetensors" in v["models"]["diffusion_models"]
    assert cfg.section("comfyui")["enabled"] is True
    assert cfg.get("workflows.aliases")["minimax_user"] == "minimax_h3_ultra_user"


def test_video_alone_enables_dedicated_comfy():
    cfg = install("video")
    assert cfg.section("comfyui")["enabled"] is True


def test_capabilities_modes_follow_installed_models():
    from atelier3d import capabilities

    cfg = install("video")
    reset_db_singleton()
    caps = capabilities.compute(cfg, get_db(cfg.db_path), refresh=True, comfy_url="http://127.0.0.1:8999")["capabilities"]
    vm = caps["video.minimax"]
    assert vm["status"] == "à tester", vm
    assert vm["modes"]["image_to_video"] == "à tester" and vm["modes"]["text_to_video"] == "à tester", vm["modes"]
    assert vm["modes"]["reference_to_video"] == "indisponible", vm["modes"]
    assert "minimax_h3_ref2va_pruned_int8_convrot.safetensors" in vm["mode_reasons"]["reference_to_video"]
    assert vm["url"] == "http://127.0.0.1:8999"

    cfg2 = install("video,video_ref", FL2VA_SET + [("diffusion_models", "minimax_h3_ref2va_pruned_int8_convrot.safetensors")])
    reset_db_singleton()
    caps2 = capabilities.compute(cfg2, get_db(cfg2.db_path), refresh=True, comfy_url="http://127.0.0.1:8999")["capabilities"]
    assert caps2["video.minimax"]["modes"]["reference_to_video"] == "à tester", caps2["video.minimax"]


def test_planner_refuses_mode_whose_model_is_missing():
    from atelier3d import capabilities
    from atelier3d.graph import nodetypes as NT
    from atelier3d.graph.planner import Planner

    cfg = install("video")
    reset_db_singleton()
    data = capabilities.compute(cfg, get_db(cfg.db_path), refresh=True, comfy_url="http://127.0.0.1:8999")
    caps, aliases = data["capabilities"], data["aliases"]
    step = NT.get("video.generate").steps({"type": "video.generate", "params": {}})[0]
    ref = Planner._engine_for(None, {"type": "video.generate", "params": {"mode": "reference_to_video"}}, step, caps, aliases)
    assert ref["status"] == "indisponible" and "ref2va" in ref["reason"], ref
    i2v = Planner._engine_for(None, {"type": "video.generate", "params": {"mode": "image_to_video"}}, step, caps, aliases)
    assert i2v["status"] == "à tester" and i2v["workflow_id"] == "minimax_h3_ultra_user", i2v


def test_video_client_is_dedicated_comfy():
    cfg = install("video")
    reset_db_singleton()
    s = Services(get_db(cfg.db_path), cfg)
    assert s.comfy("video").base.rstrip("/") == "http://127.0.0.1:8999"


def test_attached_video_unchanged():
    cfg = install("image")  # sans composant vidéo : branche désactivée, ComfyUI dédié pour l'image seulement
    assert cfg.section("video")["status"] == "disabled"
    reset_db_singleton()
    s = Services(get_db(cfg.db_path), cfg)
    assert s.comfy("video").base.rstrip("/") == "http://127.0.0.1:8188"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except Exception as e:  # noqa: BLE001
                import traceback

                fails += 1
                print(f"ÉCHEC {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)
