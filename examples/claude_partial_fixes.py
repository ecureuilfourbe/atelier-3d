"""Corrections ciblées sans tout recalculer.

  python examples/claude_partial_fixes.py rig <graph_id>      # relance SEULEMENT le rig (+ descendants : tests, export)
  python examples/claude_partial_fixes.py video <graph_id>    # régénère SEULEMENT la vidéo (nouvelle seed)
Affiche les étapes planifiées pour prouver qu'image, maillage, textures (et vidéo / 3D) ne sont pas recalculés.
"""
from __future__ import annotations

import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D  # noqa: E402


def main() -> int:
    what, gid = sys.argv[1], sys.argv[2]
    c = Atelier3D(author="Claude Code")
    g = c.graph(gid, status=False)["graph"]
    if what == "rig":
        rr = c.rerun_node(gid, "rig", scope="node_and_descendants", params_patch={"no_fingers": True}, force_recompute=True,
                          activate_output=True, expected_graph_revision=g["revision"], idempotency_key=f"fix-rig-{gid}-{g['revision']}")
    else:
        rr = c.rerun_node(gid, "video", scope="node_only", params_patch={"seed": random.randint(1, 2**31)}, force_recompute=True,
                          expected_graph_revision=g["revision"], idempotency_key=f"fix-video-{gid}-{g['revision']}")
    print("étapes relancées :", [(j["node_id"], j["step"]) for j in rr["jobs"]])
    relaunched = {j["node_id"] for j in rr["jobs"]}
    passive = ("text.prompt", "image.import", "mesh.import", "image.preview", "mesh.preview", "video.preview", "reference.collection")
    kept = sorted(n["id"] for n in g["nodes"] if n["type"] not in passive and n["id"] not in relaunched)
    print("non recalculées (résultats conservés) :", kept)
    for j in rr["jobs"]:
        print(" ", j["node_id"], j["step"], c.wait(j["job_id"], timeout=4 * 3600)["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
