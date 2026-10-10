"""Outil de développement : génère installer/versions.lock.json.

Interroge les sources officielles (GitHub Releases, API Hugging Face, download.blender.org)
pour figer URL, taille et empreinte de chaque composant. L'installateur n'exécute PAS ce
script : il lit uniquement le fichier verrouillé produit ici.

Usage : python installer/tools/generate_lock.py              (régénère tout depuis les révisions actuelles)
        python installer/tools/generate_lock.py --add-missing (ajoute seulement les entrées absentes : les entrées
                                                                déjà figées — URL, révision, SHA-256 — restent intactes)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import urllib.request

try:  # validation TLS via le magasin de certificats Windows (jamais désactivée)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

UA ={"User-Agent": "atelier3d-lock-generator"}
ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "installer" / "versions.lock.json"


def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get_text(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def head_size(url: str) -> int:
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def gh_asset(repo: str, tag: str, name: str) -> dict:
    rel = get_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    for a in rel["assets"]:
        if a["name"] == name:
            digest = a.get("digest") or ""
            return {
                "url": a["browser_download_url"],
                "size": a["size"],
                "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
                "source": f"GitHub Releases {repo}@{tag}",
            }
    raise KeyError(f"{name} absent de {repo}@{tag}")


def hf_file(repo: str, path: str, revision: str = "main", kind: str = "models") -> dict:
    info = get_json(f"https://huggingface.co/api/{kind}/{repo}/revision/{revision}")
    sha = info["sha"]
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    tree = get_json(f"https://huggingface.co/api/{kind}/{repo}/tree/{sha}/{parent}")
    for item in tree:
        if item["path"] == path:
            lfs = item.get("lfs") or {}
            prefix = "" if kind == "models" else f"{kind}/"
            return {
                "url": f"https://huggingface.co/{prefix}{repo}/resolve/{sha}/{path}",
                "size": item["size"],
                "sha256": lfs.get("oid"),
                "source": f"Hugging Face {repo}@{sha[:12]}",
                "revision": sha,
                "gated": bool(info.get("gated")),
            }
    raise KeyError(f"{path} absent de {repo}")


def gh_tree(repo: str, commit: str, include: list[str] | None, exclude: tuple[str, ...] = ()) -> dict:
    """Liste des fichiers (avec SHA1 de blob git) d'un dépôt à un commit figé (include=None : tout le dépôt)."""
    tree = get_json(f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1")
    files = {}
    for item in tree["tree"]:
        if item["type"] != "blob":
            continue
        if any(item["path"].startswith(x) for x in exclude):
            continue
        if include is None or any(item["path"] == p or item["path"].startswith(p.rstrip("/") + "/") for p in include):
            files[item["path"]] = {"git_blob_sha1": item["sha"], "size": item.get("size", 0)}
    info = get_json(f"https://api.github.com/repos/{repo}")
    return {
        "url": f"https://codeload.github.com/{repo}/zip/{commit}",
        "commit": commit,
        "license": (info.get("license") or {}).get("spdx_id"),
        "archive_root": f"{repo.split('/')[1]}-{commit}",
        "files": files,
        "source": f"GitHub {repo}@{commit[:12]}",
    }


def pypi_wheel(name: str, version: str, filename: str) -> dict:
    for f in get_json(f"https://pypi.org/pypi/{name}/{version}/json")["urls"]:
        if f["filename"] == filename:
            return {"url": f["url"], "size": f["size"], "sha256": f["digests"]["sha256"], "filename": filename,
                    "source": f"PyPI {name}=={version}", "version": version}
    raise KeyError(f"{filename} absent de PyPI {name}=={version}")


# Branche vidéo installée (composants « video » / « video_ref ») : les workflows MiniMax H3 Ultra livrés (pack Aitrepreneur
# importé, app/workflows/comfy/minimax_h3_ultra_user_*) exécutés par le ComfyUI dédié. Commits = ceux du ComfyUI qui a
# servi à valider ces workflows ; nodes MiniMax H3 natives de ComfyUI (aucune node tierce pour le modèle lui-même).
VIDEO_CUSTOM_NODES = {
    "rgthree-comfy": ("rgthree/rgthree-comfy", "6b76ee6f2c5a007710b5a16f97c94330d6ecc871"),
    "ComfyUI-VideoHelperSuite": ("Kosinkadink/ComfyUI-VideoHelperSuite", "4ee72c065db22c9d96c2427954dc69e7b908444b"),
}
NODE_EXCLUDE = (".github/", "tests/", "docs/", "examples/")
VIDEO_WHEELS = [  # dépendances de VideoHelperSuite, installées sans dépendances transitives (numpy déjà fourni par ComfyUI)
    ("opencv-python-headless", "4.13.0.92", "opencv_python_headless-4.13.0.92-cp37-abi3-win_amd64.whl"),
    ("imageio-ffmpeg", "0.6.0", "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl"),
]
VIDEO_MODELS = [
    ("minimax_h3_fl2va_pruned_int8", "Comfy-Org/MiniMax-H3", "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "diffusion_models", ["video"]),
    ("minimax_h3_ref2va_pruned_int8", "Comfy-Org/MiniMax-H3", "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors", "diffusion_models", ["video_ref"]),
    ("qwen3vl_32b_minimax_h3_int8", "Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "text_encoders", ["video", "video_ref"]),
    ("minimax_h3_video_vae_fp16", "Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_fp16.safetensors", "vae", ["video", "video_ref"]),
    ("minimax_h3_audio_vae_fp32", "Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors", "vae", ["video", "video_ref"]),
    # LoRA turbo 4 steps référencée par le workflow (Power Lora Loader rgthree) ; Apache-2.0
    ("minimax_h3_turbo_4step_ckpt500", "drbaph/MiniMax-H3-Turbo-Lora-ComfyUI", "minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors", "loras", ["video", "video_ref"]),
]


def video_nodes() -> dict:
    return {k: gh_tree(repo, commit, None, exclude=NODE_EXCLUDE) for k, (repo, commit) in VIDEO_CUSTOM_NODES.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add-missing", action="store_true")
    a = ap.parse_args()
    if a.add_missing:
        add_missing()
        return
    blender_sha = {}
    for line in get_text("https://download.blender.org/release/Blender5.2/blender-5.2.1.sha256").splitlines():
        if line.strip():
            h, name = line.split()
            blender_sha[name] = h
    blender_url = "https://download.blender.org/release/Blender5.2/blender-5.2.1-windows-x64.zip"

    lock = {
        "schema": "atelier3d-lock/1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "python": {
            "app_version": "3.12.15",
            "mia_version": "3.11.17",
            "note": "CPython portable (python-build-standalone) installé par uv dans <runtime>\\python",
        },
        "tools": {
            "uv": {**gh_asset("astral-sh/uv", "0.12.23", "uv-x86_64-pc-windows-msvc.zip"), "version": "0.12.23"},
            "7zr": {**gh_asset("ip7z/7zip", "26.03", "7zr.exe"), "version": "26.03"},
        },
        "comfyui": {
            "portable": {
                **gh_asset("Comfy-Org/ComfyUI", "v0.38.0", "ComfyUI_windows_portable_nvidia.7z"),
                "version": "v0.38.0",
                "commit": get_json("https://api.github.com/repos/Comfy-Org/ComfyUI/git/ref/tags/v0.38.0")["object"]["sha"],
            },
            "custom_nodes": {
                "comfyui-krea2edit": gh_tree(
                    "lbouaraba/comfyui-krea2edit",
                    "86f886dac23013d88996e3a2e99093ba44d322fb",
                    include=["__init__.py", "workflows", "LICENSE", "README.md", "CHANGELOG.md", "pyproject.toml"],
                ),
                **video_nodes(),
            },
            "python_packages": {"video": [pypi_wheel(*w) for w in VIDEO_WHEELS]},
        },
        "blender": {
            "minimum_version": "4.2.0",
            "download": {
                "url": blender_url,
                "size": head_size(blender_url),
                "sha256": blender_sha["blender-5.2.1-windows-x64.zip"],
                "version": "5.2.1",
                "source": "download.blender.org",
            },
        },
        "engines": {
            "mia": {
                "code": gh_tree(
                    "jasongzy/Make-It-Animatable",
                    "8fb51382ff6da556cdb95cc03a48200603f3a493",
                    include=["model.py", "models_ae.py", "util/utils.py", "util/dataset_mixamo.py",
                             "util/dataset_mixamo_additional.py", "util/blender_utils.py", "util/misc.py",
                             "app.py", "LICENSE", "README.md"],
                ),
                "weights": {
                    name: hf_file("jasongzy/Make-It-Animatable", f"output/best/new/{name}")
                    for name in ["bw.pth", "bw_normal.pth", "joints.pth", "joints_coarse.pth", "pose.pth"]
                },
            },
        },
        "models": {},
    }

    models = [
        # id, repo, chemin HF, dossier ComfyUI, profils
        ("krea2_turbo_fp8_scaled", "Comfy-Org/Krea-2", "diffusion_models/krea2_turbo_fp8_scaled.safetensors", "diffusion_models", ["image"]),
        ("qwen3vl_4b_fp8_scaled", "Comfy-Org/Krea-2", "text_encoders/qwen3vl_4b_fp8_scaled.safetensors", "text_encoders", ["image"]),
        ("qwen_image_vae", "Comfy-Org/Krea-2", "vae/qwen_image_vae.safetensors", "vae", ["image"]),
        ("krea2_identity_edit_v1_2", "conradlocke/krea2-identity-edit", "krea2_identity_edit_v1_2.safetensors", "loras/Krea2", ["image_edit"]),  # chemin attendu par workflows/krea2_identity_edit.json
        # LoRA de style référencée (désactivée par un switch) dans le template officiel Krea 2 Turbo : validée par ComfyUI, donc requise
        ("krea2_darkbrush", "Comfy-Org/Krea-2", "loras/krea2_darkbrush.safetensors", "loras", ["image"]),
        ("pixal3d_int8_convrot", "Comfy-Org/Pixal3D", "diffusion_models/pixal3d_int8_convrot.safetensors", "diffusion_models", ["mesh"]),
        ("dino_v3_L_naf_fp32", "Comfy-Org/Pixal3D", "clip_vision/dino_v3_L_naf_fp32.safetensors", "clip_vision", ["mesh"]),
        ("trellis_2_shape_vae_bf16", "Comfy-Org/Pixal3D", "vae/trellis_2_shape_vae_bf16.safetensors", "vae", ["mesh"]),
        ("trellis_2_texture_vae_bf16", "Comfy-Org/Pixal3D", "vae/trellis_2_texture_vae_bf16.safetensors", "vae", ["mesh"]),
        # Le template officiel « Pixal3D & TRELLIS.2: Image to Model » valide aussi ces fichiers :
        ("trellis_2_int8_convrot", "Comfy-Org/TRELLIS.2", "diffusion_models/trellis_2_int8_convrot.safetensors", "diffusion_models", ["mesh"]),
        ("moge_2_vitl_normal_fp16", "Comfy-Org/MoGe", "geometry_estimation/moge_2_vitl_normal_fp16.safetensors", "geometry_estimation", ["mesh"]),
        ("birefnet", "Comfy-Org/BiRefNet", "background_removal/birefnet.safetensors", "background_removal", ["image_edit", "mesh"]),
        # Upscale 2K / 4K (templates officiels « SeedVR2 3B Int8: Upscale Image » et « GAN Upscaler »)
        ("seedvr2_3b_int8_convrot", "Comfy-Org/SeedVR2", "diffusion_models/seedvr2_3b_int8_convrot.safetensors", "diffusion_models", ["upscale"]),
        ("seedvr2_ema_vae_fp16", "Comfy-Org/SeedVR2", "vae/seedvr2_ema_vae_fp16.safetensors", "vae", ["upscale"]),
        ("realesrgan_x4plus", "Comfy-Org/Real-ESRGAN_repackaged", "RealESRGAN_x4plus.safetensors", "upscale_models", ["upscale"]),
        *VIDEO_MODELS,
    ]
    extra = json.loads((ROOT / "installer" / "tools" / "extra_models.json").read_text("utf-8")) \
        if (ROOT / "installer" / "tools" / "extra_models.json").exists() else []
    for m in extra:
        models.append((m["id"], m["repo"], m["path"], m["folder"], m["profiles"]))
    for mid, repo, path, folder, profiles in models:
        entry = hf_file(repo, path)
        entry.update({"filename": path.rsplit("/", 1)[-1], "comfy_folder": folder, "profiles": profiles})
        lock["models"][mid] = entry

    OUT.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(m["size"] for m in lock["models"].values())
    print(f"écrit {OUT} — {len(lock['models'])} modèles, {total / 1024**3:.1f} Gio")


def add_missing() -> None:
    """Complète le fichier verrouillé existant sans modifier ce qui y est déjà figé."""
    lock = json.loads(OUT.read_text("utf-8"))
    added = []
    cn = lock["comfyui"].setdefault("custom_nodes", {})
    for k, (repo, commit) in VIDEO_CUSTOM_NODES.items():
        if k not in cn:
            cn[k] = gh_tree(repo, commit, None, exclude=NODE_EXCLUDE)
            added.append(k)
    pp = lock["comfyui"].setdefault("python_packages", {})
    if "video" not in pp:
        pp["video"] = [pypi_wheel(*w) for w in VIDEO_WHEELS]
        added.append("python_packages.video")
    for mid, repo, path, folder, profiles in VIDEO_MODELS:
        if mid not in lock["models"]:
            entry = hf_file(repo, path)
            entry.update({"filename": path.rsplit("/", 1)[-1], "comfy_folder": folder, "profiles": profiles})
            lock["models"][mid] = entry
            added.append(mid)
    lock["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    OUT.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("ajouté :", ", ".join(added) or "rien")


if __name__ == "__main__":
    main()
