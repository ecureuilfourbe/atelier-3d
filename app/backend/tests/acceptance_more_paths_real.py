"""Tests d'acceptation RÉELS des chemins secondaires : rig « poids automatiques Blender », rig articulé (objet),
vidéo texte → vidéo, vidéo première/dernière image.

  python app/backend/tests/acceptance_more_paths_real.py <glb_humanoide> <glb_objet> <image_debut> <image_fin> [--rigs-only]
Écrit <installation>\\logs\\acceptance_more_paths.json.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D  # noqa: E402

C = Atelier3D(author="Claude Code")
R: dict = {"checks": []}
HOME = pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])
OUT = HOME / "logs" / "acceptance_more_paths"


def check(name, ok, **info):
    R["checks"].append({"check": name, "ok": bool(ok), **info})
    print(("OK   " if ok else "ÉCHEC") + f" {name} {json.dumps(info, ensure_ascii=False)[:500]}", flush=True)


def run(doc: dict, label: str) -> tuple[dict, dict]:
    g = C.create_graph(doc)["graph"]
    plan = C.plan(g["id"])
    if plan.get("errors"):
        check(f"{label}_plan", False, errors=plan["errors"])
        return g, {"status": "invalid", "jobs": []}
    t0 = time.time()
    r = C.run_graph(g["id"], idempotency_key=f"more-{label}-{g['id']}")
    res = C.wait_run(g["id"], r["run_id"], timeout=3 * 3600)
    res["duration_s"] = round(time.time() - t0)
    check(f"{label}_run", res["status"] == "succeeded", status=res["status"], duree_s=res["duration_s"],
          etapes=[(j["node_id"], j["step"], j["status"]) for j in res["jobs"]])
    return g, res


def export_report(res: dict) -> dict:
    ex = next((j for j in res["jobs"] if j["node_id"] == "export"), None)
    if not ex:
        return {}
    arts = C.artifacts(ex.get("job_id") or ex["id"])
    rep = next((a for a in arts if a["role"] in ("export_result", "validation", "report") and a["path"].endswith(".json")), None)
    data = {}
    for a in arts:
        if a["path"].endswith("asset_export_result.json") or a["path"].endswith("verify_export_result.json"):
            d = C.download(a["id"], OUT / (ex.get("job_id") or ex["id"]))
            data[pathlib.Path(a["path"]).name] = json.loads(pathlib.Path(d["path"]).read_text(encoding="utf-8"))
    return data or ({"report": rep} if rep else {})


def video_outputs(res: dict, label: str) -> None:
    vj = next((j for j in res["jobs"] if j["node_id"] == "video"), None)
    if not vj:
        return
    arts = C.artifacts(vj.get("job_id") or vj["id"])
    meta = next((a for a in arts if a["role"] == "video_metadata"), None)
    if meta:
        m = json.loads(pathlib.Path(C.download(meta["id"], OUT / label)["path"]).read_text(encoding="utf-8"))
        check(f"{label}_mp4", (m.get("video") or {}).get("frames", 0) > 24, workflow=m.get("workflow_id"), video=m.get("video"),
              prompt_final=(m.get("prompt_final") or "")[:200])
    R[label + "_frames"] = [C.download(a["id"], OUT / label)["path"] for a in arts if a["role"] == "video_frame"]


RIGS_ONLY = "--rigs-only" in sys.argv
if RIGS_ONLY:
    sys.argv.remove("--rigs-only")
hum, obj, first, last = sys.argv[1:5]
uh, uo, uf, ul = C.upload(hum), C.upload(obj), C.upload(first), C.upload(last)

# 1. squelette MIA + poids automatiques Blender
doc = {"schema_version": "1.1", "name": "Rig poids automatiques Blender", "nodes": [
    {"id": "source", "type": "mesh.import", "params": {"upload_id": uh["id"]}},
    {"id": "rig", "type": "mesh.rig", "params": {"profile": "humanoid", "engine": "blender_auto"}},
    {"id": "validate", "type": "mesh.validate", "params": {}},
    {"id": "export", "type": "asset.export", "params": {"name": "Rig_auto_Blender", "target_height_m": 1.8}}],
    "edges": [{"id": "e1", "from": ["source", "mesh"], "to": ["rig", "mesh"]},
              {"id": "e2", "from": ["rig", "rigged_mesh"], "to": ["validate", "rigged_mesh"]},
              {"id": "e3", "from": ["validate", "rigged_mesh"], "to": ["export", "asset"]}]}
g, res = run(doc, "rig_blender_auto")
steps = [(j["node_id"], j["step"]) for j in res["jobs"]]
check("rig_blender_auto_etapes", ("rig", "skeleton") in steps and ("rig", "bind") in steps, etapes=steps)
R["rig_blender_auto_export"] = export_report(res)

# 2. objet articulé (pièces détachées → un os par pièce)
doc = {"schema_version": "1.1", "name": "Rig articulé", "nodes": [
    {"id": "source", "type": "mesh.import", "params": {"upload_id": uo["id"]}},
    {"id": "rig", "type": "mesh.rig", "params": {"profile": "articulated"}},
    {"id": "export", "type": "asset.export", "params": {"name": "Objet_articule"}}],
    "edges": [{"id": "e1", "from": ["source", "mesh"], "to": ["rig", "mesh"]},
              {"id": "e2", "from": ["rig", "rigged_mesh"], "to": ["export", "asset"]}]}
g, res = run(doc, "rig_articule")
R["rig_articule_export"] = export_report(res)

if RIGS_ONLY:
    (HOME / "logs" / "acceptance_more_paths_rigs.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Résumé :", sum(c["ok"] for c in R["checks"]), "/", len(R["checks"]), "vérifications OK", flush=True)
    sys.exit(0)

# 3. texte → vidéo
doc = {"schema_version": "1.1", "name": "Texte → vidéo", "nodes": [
    {"id": "p", "type": "text.prompt", "params": {"text": "A small wooden sailboat drifts slowly across a calm turquoise lagoon at sunset, "
                                                          "gentle ripples, warm light, static camera."}},
    {"id": "video", "type": "video.generate", "params": {"mode": "text_to_video", "duration_s": 3, "width": 480, "height": 864, "seed": 8801}},
    {"id": "view", "type": "video.preview", "params": {}}],
    "edges": [{"id": "e1", "from": ["p", "text"], "to": ["video", "prompt"]}, {"id": "e2", "from": ["video", "video"], "to": ["view", "video"]}]}
g, res = run(doc, "video_texte")
video_outputs(res, "video_texte")

# 4. première + dernière image → vidéo
doc = {"schema_version": "1.1", "name": "Première/dernière image → vidéo", "nodes": [
    {"id": "p", "type": "text.prompt", "params": {"text": "The man walks in from the left, kneels by the treasure chest and opens its lid; "
                                                          "golden light shines from inside. Static camera."}},
    {"id": "a", "type": "image.import", "params": {"upload_id": uf["id"]}},
    {"id": "b", "type": "image.import", "params": {"upload_id": ul["id"]}},
    {"id": "video", "type": "video.generate", "params": {"mode": "first_last_to_video", "duration_s": 3, "width": 480, "height": 864, "seed": 8802}},
    {"id": "view", "type": "video.preview", "params": {}}],
    "edges": [{"id": "e1", "from": ["p", "text"], "to": ["video", "prompt"]}, {"id": "e2", "from": ["a", "image"], "to": ["video", "image"]},
              {"id": "e3", "from": ["b", "image"], "to": ["video", "last_image"]}, {"id": "e4", "from": ["video", "video"], "to": ["view", "video"]}]}
g, res = run(doc, "video_premiere_derniere")
video_outputs(res, "video_premiere_derniere")
(HOME / "logs" / "acceptance_more_paths.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
print("Résumé :", sum(c["ok"] for c in R["checks"]), "/", len(R["checks"]), "vérifications OK", flush=True)
