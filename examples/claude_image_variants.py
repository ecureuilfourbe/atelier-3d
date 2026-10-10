"""Claude génère une image Krea 2, crée une seconde variante, compare les deux et choisit une sortie.

  python examples/claude_image_variants.py "prompt" [--pick 2]
Les deux images sont téléchargées dans <installation>\\data\\exports\\image_variants pour inspection.
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="Full body front view of a single original fantasy ranger character in A-pose, plain light grey background, no text")
    ap.add_argument("--pick", type=int, default=2, help="variante à utiliser (1 ou 2)")
    a = ap.parse_args()
    c = Atelier3D(author="Claude Code")
    job = c.create_job("image_generate", {"prompt": a.prompt, "seed": 1101, "graph_name": "Claude · variantes d'image"},
                       idempotency_key=f"claude-img-{abs(hash(a.prompt)) % 10**8}")
    gid = job["graph_id"]
    print("graphe", gid, "tâche", job["job_id"])
    c.wait(job["job_id"], on_update=lambda j: print(" ", j["status"], j.get("summary") or ""))
    g = c.graph(gid, status=False)["graph"]
    rr = c.rerun_node(gid, "image", scope="node_only", params_patch={"seed": 1102}, force_recompute=True,
                      expected_graph_revision=g["revision"], idempotency_key=f"claude-img-v2-{gid}")
    c.wait(rr["jobs"][0]["job_id"])
    vs = [v for v in c.variants(gid, "image")["variants"] if v["step"] == "create"]
    out = pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"]) / "data" / "exports" / "image_variants"
    for i, v in enumerate(vs, 1):
        art = v["artifacts"]["image"][0]
        d = c.download(art["id"], out / f"{gid}_v{i}_seed{v['seed']}.png")
        print(f"variante {i} : {v['id']} seed {v['seed']} auteur {v['author']} active={v['active']} → {d['path']}")
    chosen = vs[a.pick - 1]
    sel = c.select_variant(gid, "image", chosen["id"])
    print(f"Sortie active : {chosen['id']} ; à recalculer : {sel['newly_stale'] or 'aucune'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
