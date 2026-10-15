"""Configuration post-installation (appelée par installer/install.ps1). Relançable : fusionne avec l'existant.

- écrit <home>/config/atelier3d.json (chemins, ports par défaut, moteurs, alias de workflows)
- écrit <home>/config/extra_model_paths.yaml pour le ComfyUI dédié
- crée le jeton local d'API (<home>/config/api_token), jamais journalisé
- vidéo MiniMax H3 : soit installée avec Atelier 3D (composants « video » / « video_ref » : workflows MiniMax H3 Ultra livrés,
  exécutés par le ComfyUI dédié), soit rattachée à un ComfyUI MiniMax existant (« video_attach » : COPIE de ses workflows,
  l'original n'est pas modifié)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import secrets
import shutil

from . import __version__


def read_json(p: pathlib.Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def write_json(p: pathlib.Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def parse_launcher(bat: pathlib.Path) -> list[str]:
    """Arguments ComfyUI du lanceur .bat existant (lecture seule)."""
    try:
        txt = bat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in txt.splitlines():
        m = re.search(r"main\.py(.*)$", line)
        if m:
            return [a for a in m.group(1).split() if a and not a.startswith("%")]
    return []


LOW_MEMORY_ARGS = ["--fast-disk", "--disable-async-offload", "--disable-pinned-memory", "--reserve-vram", "1"]
VIDEO_COMPONENTS = ("video", "video_ref")


def dedicated_video(home: pathlib.Path, runtime: pathlib.Path, dist: pathlib.Path) -> dict:
    """Vidéo installée avec Atelier 3D : poids MiniMax H3 dans <home>/models, nodes rgthree + VideoHelperSuite dans le
    ComfyUI dédié. Même processus que les autres moteurs (lancé en profil mémoire réduite pour la vidéo)."""
    lock = read_json(dist / "installer" / "versions.lock.json", {}) or {}
    found = {}
    for folder in ("diffusion_models", "text_encoders", "vae", "loras"):
        d = home / "models" / folder
        if d.is_dir():
            found[folder] = sorted(p.name for p in d.iterdir() if "minimax" in p.name.lower())
    comfy = runtime / "comfyui"
    return {
        "status": "detected",
        "label": "MiniMax H3 Ultra",
        "provider": "comfyui_dedicated",
        "execution": "local",
        "comfy_dir": str(comfy / "ComfyUI"),
        "python": str(comfy / "python_embeded" / "python.exe"),
        "version": ((lock.get("comfyui") or {}).get("portable") or {}).get("version"),
        "low_memory_args": LOW_MEMORY_ARGS,
        "models": found,
        "may_launch": True,
    }


def _drive_roots() -> list[pathlib.Path]:
    import string

    return [pathlib.Path(f"{d}:\\") for d in string.ascii_uppercase if pathlib.Path(f"{d}:\\").exists()]


def _kimodo_ok(k: dict) -> bool:
    return all(k.get(x) and pathlib.Path(k[x]).is_file() for x in ("exe", "model", "text_model"))


def _mediapipe_ok(m: dict) -> bool:
    return all(m.get(x) and pathlib.Path(m[x]).is_file() for x in ("python", "model"))


def detect_animation(home: pathlib.Path, runtime: pathlib.Path, prev: dict | None, dist: pathlib.Path | None = None,
                     comps: list | tuple = ()) -> dict:
    """Moteurs d'animation (« Animer le modèle 3D ») : installés avec Atelier 3D (composants motion_prompt / motion_video),
    sinon déjà présents sur la machine (lecture seule, rien n'est modifié). Les chemins déjà validés sont conservés."""
    import subprocess

    prev = prev or {}
    k = dict(prev.get("kimodo") or {})
    if dist is not None and "motion_prompt" in comps:
        kdir = runtime / "kimodo-models" if (runtime / "kimodo-models").is_dir() else home / "models" / "kimodo"  # chemin ASCII si possible
        exe = dist / "app" / "engines" / "kimodo_cpp" / "bin" / "kmd-generate.exe"
        motion = sorted(kdir.glob("kimodo-soma-rp-v1.1-*.gguf")) if kdir.is_dir() else []
        inst = {"dir": str(kdir), "exe": str(exe), "bin_dir": str(exe.parent), "model": str(motion[0]) if motion else "",
                "text_model": str(kdir / "Llama-3-Kimodo-Q8_0.gguf"), "backend": "vulkan", "text_layer_chunk": 8,
                "origin": "installé avec Atelier 3D"}
        if _kimodo_ok(inst) and (kdir / "tokenizer.gguf").is_file():
            k = inst
    if not _kimodo_ok(k):
        roots = [pathlib.Path(os.environ["ATELIER3D_KIMODO_DIR"])] if os.environ.get("ATELIER3D_KIMODO_DIR") else []
        roots += [runtime / "kimodo-cpp"] + [r / "AI" / "Animation" / "kimodo-cpp" for r in _drive_roots()] + [r / "kimodo-cpp" for r in _drive_roots()]
        for root in roots:
            exe = root / "build" / "Release" / "kmd-generate.exe"
            models = sorted((root / "models").glob("kimodo-soma-rp-v1.1-*.gguf")) if (root / "models").is_dir() else []
            text = root / "Llama-3-Kimodo-Q8_0.gguf"
            if exe.is_file() and models and text.is_file():
                k = {"dir": str(root), "exe": str(exe), "bin_dir": str(root / "build" / "bin" / "Release"), "model": str(models[0]),
                     "text_model": str(text), "backend": "vulkan", "text_layer_chunk": 8, "origin": "existant (lecture seule)"}
                break
    m = dict(prev.get("mediapipe") or {})
    if "motion_video" in comps:
        inst = {"python": str(runtime / "venv-motion" / "Scripts" / "python.exe"),
                "model": str(home / "models" / "mediapipe" / "pose_landmarker_heavy.task"), "origin": "venv-motion"}
        if _mediapipe_ok(inst):
            m = inst
    if not _mediapipe_ok(m):
        model = home / "models" / "mediapipe" / "pose_landmarker_heavy.task"
        pys = [runtime / "venv-motion" / "Scripts" / "python.exe"]
        pys += [pathlib.Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / f"Python3{v}" / "python.exe" for v in (12, 11, 13, 10)]
        pys += [pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / f"Python3{v}" / "python.exe" for v in (12, 11, 13, 10)]
        for py in pys:
            if not py.is_file():
                continue
            try:
                r = subprocess.run([str(py), "-c", "import mediapipe, numpy, PIL; print(mediapipe.__version__)"], capture_output=True,
                                   text=True, timeout=120, creationflags=0x08000000)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                m = {"python": str(py), "model": str(model), "version": r.stdout.strip().splitlines()[-1] if r.stdout.strip() else None,
                     "origin": "venv-motion" if "venv-motion" in str(py) else "Python existant (lecture seule)"}
                break
    return {"kimodo": k, "mediapipe": m}


def detect_minimax(manifest: dict, home: pathlib.Path) -> dict:
    installs = (manifest.get("components") or {}).get("existing_comfyui") or []
    if isinstance(installs, dict):
        installs = [installs]
    cands = [i for i in installs if i.get("has_minimax_h3")]
    best = None
    for c in cands:
        if c.get("minimax_h3_models"):
            best = c
            break
    if best is None and cands:
        best = cands[0]
    if best is None:
        return {"status": "absent", "label": "MiniMax H3 Ultra",
                "message": "Aucun ComfyUI avec nodes MiniMax H3 et poids locaux détecté : indiquez son dossier dans la configuration."}
    comfy = pathlib.Path(best["comfy_dir"])
    root = comfy.parent
    models = comfy / "models"
    found = {}
    for folder in ("diffusion_models", "text_encoders", "vae", "loras", "vae_approx"):
        d = models / folder
        if d.is_dir():
            found[folder] = sorted(p.name for p in d.iterdir() if "minimax" in p.name.lower() or "h3" in p.name.lower())
    workflows = []
    for pattern_dir in (root, pathlib.Path(os.environ.get("USERPROFILE", "")) / "Downloads", comfy / "user" / "default" / "workflows"):
        if pattern_dir.is_dir():
            for p in pattern_dir.glob("*.json"):
                if re.search(r"minimax|h3", p.name, re.I):
                    workflows.append(str(p))
    imported = []
    dest = home / "data" / "imported_workflows" / "minimax"
    dest.mkdir(parents=True, exist_ok=True)
    for w in workflows:
        src = pathlib.Path(w)
        target = dest / src.name
        if not target.exists():
            shutil.copy2(src, target)  # copie : l'original reste intact
        imported.append({"original": str(src), "copy": str(target), "size": src.stat().st_size})
    launcher = root / "run_nvidia_gpu.bat"
    return {
        "status": "detected",
        "label": "MiniMax H3 Ultra",
        "provider": "comfyui_attached",
        "execution": "local",  # poids locaux dans ce ComfyUI : fonctionne hors ligne une fois lancé
        "comfy_dir": str(comfy),
        "python": best.get("python"),
        "version": best.get("version"),
        "url": "http://127.0.0.1:8188",
        "launcher": str(launcher) if launcher.exists() else None,
        "launch_args": parse_launcher(launcher) if launcher.exists() else ["--windows-standalone-build"],
        # profil qui a réussi le 30/09/2026 (« fast-disk, synchronous offload, pinned memory disabled, 1 GB VRAM reserve »)
        "low_memory_args": LOW_MEMORY_ARGS,
        "models": found,
        "workflows": imported,
        "may_launch": True,
        "never_interrupt_foreign_jobs": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--runtime", required=True)
    ap.add_argument("--dist", required=True)
    ap.add_argument("--components", default="")
    ap.add_argument("--blender", default="")
    a = ap.parse_args()
    home, runtime, dist = pathlib.Path(a.home), pathlib.Path(a.runtime), pathlib.Path(a.dist)
    cfg_path = home / "config" / "atelier3d.json"
    cfg = read_json(cfg_path, {}) or {}
    manifest = read_json(home / "config" / "install-manifest.json", {}) or {}
    comps = [c for c in a.components.split(",") if c]

    cfg["version"] = __version__
    cfg["paths"] = {"home": str(home), "runtime": str(runtime), "dist": str(dist)}
    cfg["components"] = comps
    cfg.setdefault("app", {"host": "127.0.0.1", "port": 8765})
    cfg.setdefault("security", {"allowed_dirs": [str(pathlib.Path(os.environ.get("USERPROFILE", str(home))) / "Pictures"),
                                                  str(pathlib.Path(os.environ.get("USERPROFILE", str(home))) / "Downloads")],
                                "lan_mode": False})
    comfy_dir = runtime / "comfyui"
    cfg["comfyui"] = {**cfg.get("comfyui", {}), "dir": str(comfy_dir), "python": str(comfy_dir / "python_embeded" / "python.exe"),
                      "main": str(comfy_dir / "ComfyUI" / "main.py"), "port": (cfg.get("comfyui") or {}).get("port", 8190),
                      "enabled": any(c in comps for c in ("image", "image_edit", "mesh", "upscale") + VIDEO_COMPONENTS),
                      "extra_args": (cfg.get("comfyui") or {}).get("extra_args", []),
                      "low_memory_args": LOW_MEMORY_ARGS}
    blender_rec = (manifest.get("components") or {}).get("blender") or {}
    cfg["blender"] = {"path": a.blender or blender_rec.get("path"), "version": blender_rec.get("version")}
    mia = (manifest.get("components") or {}).get("mia") or {}
    cfg["mia"] = {"enabled": "rig_mia" in comps, "python": str(runtime / "venv-mia" / "Scripts" / "python.exe"),
                  "code": str(runtime / "engines" / "mia"), "weights": str(home / "models" / "mia"),
                  "runner": str(dist / "app" / "engines" / "mia_runner" / "run_mia.py"), "commit": mia.get("commit")}
    if any(c in comps for c in VIDEO_COMPONENTS):
        cfg["video"] = dedicated_video(home, runtime, dist)
    elif "video_attach" in comps:
        prev = cfg.get("video") or {}
        det = detect_minimax(manifest, home)
        # conserve les réglages déjà validés par l'utilisateur
        for k in ("url", "may_launch", "launch_args", "low_memory_args", "workflow_alias"):
            if k in prev and det.get("status") == "detected":
                det[k] = prev[k]
        cfg["video"] = det
    else:
        cfg.setdefault("video", {"status": "disabled", "label": "MiniMax H3 Ultra"})

    # alias de workflows : uniquement vers des templates API livrés ET dont les modèles requis sont présents
    aliases = {}
    wf_root = dist / "app" / "workflows" / "comfy"
    models_dir = home / "models"
    defaults = {"image_create_default": "krea2_turbo_t2i", "image_edit_default": "krea2_identity_edit",
                "remove_background_default": "birefnet_remove_background", "image_to_mesh_default": "pixal3d_image_to_model",
                "upscale_seedvr2_default": "seedvr2_3b_upscale_image", "upscale_esrgan_default": "realesrgan_upscale_image",
                "text_generate_default": "qwen3vl_text_generate", "vision_generate_default": "qwen3vl_vision_generate"}
    for alias, wid in defaults.items():
        b = read_json(wf_root / wid / "bindings.json")
        if not b or not (wf_root / wid / "workflow.api.json").is_file():
            continue
        missing = [m for m in b.get("required_models", []) if not (models_dir / m["folder"] / m["name"]).is_file()]
        if not missing:
            aliases[alias] = wid
    if (cfg.get("video") or {}).get("status") == "detected" and any((wf_root / f"minimax_h3_ultra_user_{m}" / "workflow.api.json").is_file() for m in ("t2v", "i2v", "flf", "r2v")):
        aliases["minimax_user"] = "minimax_h3_ultra_user"
    cfg["workflows"] = {"aliases": aliases}
    cfg["animation"] = detect_animation(home, runtime, cfg.get("animation"), dist, comps)

    # chemins de modèles du ComfyUI dédié (modèles partagés par liens physiques dans <home>/models)
    yaml_lines = ["# Généré par Atelier 3D — modèles du ComfyUI dédié", "atelier3d:", f"  base_path: {models_dir.as_posix()}/"]
    for folder in ("diffusion_models", "text_encoders", "vae", "loras", "clip_vision", "background_removal", "geometry_estimation", "checkpoints",
                   "upscale_models"):
        yaml_lines.append(f"  {folder}: {folder}/")
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "extra_model_paths.yaml").write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")

    tok = home / "config" / "api_token"
    if not tok.exists():
        tok.write_text(secrets.token_hex(32), encoding="utf-8")
    for d in ("data/jobs", "data/uploads", "data/comfy/input", "data/comfy/output", "data/comfy/temp", "data/comfy/user", "run", "logs"):
        (home / d).mkdir(parents=True, exist_ok=True)
    write_json(cfg_path, cfg)
    # Configuration MCP prête pour Claude Code (Windows natif) : .mcp.json de projet ou commande « claude mcp add »
    app_py = runtime / "venv-app" / "Scripts" / "python.exe"
    mcp_script = dist / "mcp" / "atelier3d_mcp.py"
    mcp_json = {"mcpServers": {"atelier3d": {"type": "stdio", "command": str(app_py), "args": [str(mcp_script)],
                                             "env": {"ATELIER3D_HOME": str(home), "PYTHONUTF8": "1"}}}}
    write_json(home / "config" / "claude_code.mcp.json", mcp_json)
    (home / "config" / "claude_mcp_add.txt").write_text(
        f'claude mcp add atelier3d --scope user -e ATELIER3D_HOME="{home}" -e PYTHONUTF8=1 -- "{app_py}" "{mcp_script}"\n', encoding="utf-8")
    print(f"Configuration écrite : {cfg_path}")
    print(f"Configuration MCP pour Claude Code : {home / 'config' / 'claude_code.mcp.json'}")
    print(f"Alias de workflows résolus : {', '.join(f'{k}→{v}' for k, v in aliases.items()) or 'aucun (templates à valider)'}")
    v = cfg.get("video") or {}
    if v.get("status") == "detected" and v.get("provider") == "comfyui_dedicated":
        n = sum(len(x) for x in (v.get("models") or {}).values())
        print(f"Vidéo MiniMax H3 installée : workflows MiniMax H3 Ultra exécutés par le ComfyUI dédié ({n} fichier(s) de poids MiniMax)")
    elif v.get("status") == "detected":
        print(f"Accès MiniMax H3 détecté : ComfyUI {v.get('version')} local ({v.get('comfy_dir')}), {len(v.get('workflows', []))} workflow(s) copiés")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
