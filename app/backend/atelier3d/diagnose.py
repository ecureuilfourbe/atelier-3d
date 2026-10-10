"""Diagnostic : matériel, runtimes (CUDA vérifiée via le PyTorch réellement utilisé), moteurs, modèles, ports.

Classe chaque étape en « réalisable » (testée avec succès ici), « à tester » (installée, vérifiée statiquement)
ou « indisponible » (avec raison). Écrit <home>/logs/diagnostic.json.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from . import config as C

CREATE_NO_WINDOW = 0x08000000


def run(cmd: list[str], timeout=120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return -1, str(e)


def torch_probe(python: str) -> dict:
    code = ("import json,torch;d={'torch':torch.__version__,'cuda_runtime':torch.version.cuda,'cuda_available':torch.cuda.is_available()};"
            "d.update({'device':torch.cuda.get_device_name(0),'vram_total_mb':torch.cuda.get_device_properties(0).total_memory//2**20,"
            "'vram_free_mb':torch.cuda.mem_get_info()[0]//2**20} if torch.cuda.is_available() else {});print(json.dumps(d))")
    rc, out = run([python, "-c", code], timeout=180)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"error": out[-500:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = C.load(a.home)
    rep: dict = {"home": str(cfg.home)}
    rc, out = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"])
    rep["gpu_nvidia_smi"] = out if rc == 0 else None
    rep["note_cuda"] = "La version CUDA affichée par nvidia-smi est le maximum supporté par le pilote ; la version utilisée est celle de chaque PyTorch ci-dessous."
    runtimes = {}
    if cfg.section("comfyui").get("enabled"):
        runtimes["comfyui_dedie"] = {"python": cfg.get("comfyui.python"), **torch_probe(cfg.get("comfyui.python"))}
    if cfg.section("mia").get("enabled"):
        runtimes["mia"] = {"python": cfg.get("mia.python"), **torch_probe(cfg.get("mia.python"))}
    if cfg.section("video").get("status") == "detected" and cfg.section("video").get("provider") != "comfyui_dedicated" and not a.quick:
        runtimes["comfyui_video_utilisateur"] = {"python": cfg.get("video.python"), **torch_probe(cfg.get("video.python"))}
    rep["runtimes"] = runtimes
    bl = cfg.get("blender.path")
    rc, out = run([bl, "--factory-startup", "--version"]) if bl else (-1, "absent")
    rep["blender"] = {"path": bl, "version": out.splitlines()[0] if rc == 0 and out else None}
    from .launcher import port_free

    rep["ports"] = {str(p): ("libre" if port_free(p) else "occupé") for p in (cfg.app_port, int(cfg.get("comfyui.port", 8190)), 8188)}
    try:
        from .capabilities import compute
        from .db import get_db

        caps = compute(cfg, get_db(cfg.db_path), refresh=True)["capabilities"]
    except Exception as e:
        caps = {"erreur": {"status": "indisponible", "reason": str(e)}}
    rep["capabilities"] = caps
    out_path = cfg.logs_dir / "diagnostic.json"
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"GPU (nvidia-smi) : {rep['gpu_nvidia_smi'] or 'non détecté'}")
    for k, v in runtimes.items():
        if "error" in v:
            print(f"{k:28s} PyTorch : ERREUR {v['error'][:120]}")
        else:
            print(f"{k:28s} PyTorch {v.get('torch')} / CUDA runtime {v.get('cuda_runtime')} / GPU {'oui' if v.get('cuda_available') else 'NON'} {v.get('device', '')}")
    print(f"Blender : {rep['blender']['version'] or 'introuvable'} ({bl})")
    print("Ports : " + ", ".join(f"{p} {s}" for p, s in rep["ports"].items()))
    order = {"disponible": 0, "à tester": 1, "indisponible": 2}
    labels = {"disponible": "réalisable (testé ici)", "à tester": "à tester", "indisponible": "indisponible"}
    for k, v in sorted(caps.items(), key=lambda kv: order.get(kv[1].get("status"), 3)):
        st = v.get("status", "?")
        print(f"  [{labels.get(st, st):22s}] {k}" + (f" — {v.get('reason')}" if v.get("reason") and st != "disponible" else ""))
    print(f"Rapport : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
