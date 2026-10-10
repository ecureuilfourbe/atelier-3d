"""Claude réutilise l'image CHOISIE d'un graphe et un prompt de mouvement dans une branche vidéo MiniMax H3 (votre accès).

  python examples/claude_video_from_image.py <graph_id> <node_id_image> "prompt de mouvement" [--seconds 5]
La branche vidéo est ajoutée au MÊME graphe : la 3D et l'image existantes ne sont pas recalculées (portée « until » sur la vidéo).
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("graph_id")
    ap.add_argument("image_node")
    ap.add_argument("prompt")
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=864)
    a = ap.parse_args()
    c = Atelier3D(author="Claude Code")
    cap = c.capabilities()["capabilities"]["video.minimax"]
    print("vidéo :", cap["status"], cap.get("execution_label") or cap.get("reason"), cap.get("modes"))
    if cap["status"] == "indisponible" or (cap.get("modes") or {}).get("image_to_video") != "disponible":
        print("Arrêt : mode image → vidéo indisponible avec le workflow importé")
        return 2
    g = c.graph(a.graph_id, status=False)["graph"]
    ops = [
        {"op": "add_node", "node": {"id": "p_motion", "type": "text.prompt", "label": "Prompt mouvement", "params": {"text": a.prompt}}},
        {"op": "add_node", "node": {"id": "video", "type": "video.generate", "label": "Générer vidéo · MiniMax H3 Ultra",
                                    "params": {"mode": "image_to_video", "duration_s": a.seconds, "width": a.width, "height": a.height, "seed": 20261005}}},
        {"op": "add_node", "node": {"id": "video_view", "type": "video.preview", "label": "Preview vidéo", "params": {}}},
        {"op": "add_edge", "edge": {"id": "e_vid_img", "from": [a.image_node, "image"], "to": ["video", "image"]}},
        {"op": "add_edge", "edge": {"id": "e_vid_prompt", "from": ["p_motion", "text"], "to": ["video", "prompt"]}},
        {"op": "add_edge", "edge": {"id": "e_vid_view", "from": ["video", "video"], "to": ["video_view", "video"]}},
    ]
    g = c.patch_graph(a.graph_id, ops, expected_revision=g["revision"])["graph"]
    plan = c.plan(a.graph_id, scope="until", node_id="video")
    print("plan :", [(e["node_id"], e["step"], e["reason"]) for e in plan["executions"]], plan["errors"])
    run = c.run_graph(a.graph_id, scope="until", node_id="video", expected_graph_revision=g["revision"], idempotency_key=f"claude-video-{a.graph_id}")
    r = c.wait_run(a.graph_id, run["run_id"], timeout=4 * 3600)
    print("run :", r["status"])
    v = c.variants(a.graph_id, "video")["variants"][-1]
    for art in v["artifacts"].get("video", []):
        print("clip :", art["path"])
    print("frames :", [a_["meta"].get("time_s") for a_ in v["artifacts"].get("frames", []) if a_.get("meta")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
