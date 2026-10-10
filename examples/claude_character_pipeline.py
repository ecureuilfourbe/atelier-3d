"""Claude crée un humanoïde texturé et riggé depuis une image, suit le job, récupère aperçus/rapport et télécharge le .blend.

  python examples/claude_character_pipeline.py chemin\\image.png [--name Kaelith] [--height 1.75]
L'image doit se trouver dans un dossier autorisé (Images, Téléchargements ou <installation>\\data).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D  # noqa: E402


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--name", default="Personnage")
    ap.add_argument("--height", type=float, default=1.8)
    a = ap.parse_args()
    c = Atelier3D(author="Claude Code")
    caps = c.capabilities()["capabilities"]
    for k in ("mesh.generate", "rig.mia", "blender"):
        print(f"{k:16s} {caps[k]['status']}")
        if caps[k]["status"] == "indisponible":
            print("Arrêt : capacité indisponible —", caps[k].get("reason"))
            return 2
    up = c.upload(a.image)
    job = c.create_job("full_pipeline", {"category": "humanoid", "name": a.name, "target_height_m": a.height},
                       upload_id=up["id"], idempotency_key=f"claude-char-{up['sha256'][:16]}")
    print("tâche", job["job_id"], "graphe", job["graph_id"])
    j = c.wait(job["job_id"], timeout=4 * 3600, on_update=lambda j: print(f"  {j['status']:9s} {j.get('summary') or ''} | {(j.get('current') or {}).get('node_id') or ''} {(j.get('current') or {}).get('stage') or ''}"))
    print("statut final :", j.get("final_status"))
    out = home() / "data" / "exports" / a.name
    for a_ in c.artifacts(job["job_id"]):
        if a_["role"] in ("export_blend", "export_glb", "export_fbx", "validation") or a_["role"] == "pose_preview":
            d = c.download(a_["id"], out)
            print(f"  {a_['role']:14s} {d['path']} ({d['size']} o, sha256 {d['sha256'][:12]}…)")
    rep = c.validation(job["job_id"])["reports"]
    for r in rep:
        data = r["data"]
        if "verdict" in data:
            print("tests de déformation :", data["verdict"], [(p["pose"], p["warnings"]) for p in data["poses"]])
        if "verification" in data:
            print("vérification export :", data["status"], {k: v.get("ok") for k, v in data["verification"].items() if isinstance(v, dict)})
    return 0 if j["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
