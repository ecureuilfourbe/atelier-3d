"""Test d'acceptation RÉEL : vidéo MiniMax « références → vidéo » avec DEUX sujets distincts (personnage + objet).

Vérifie : choix du workflow à deux références (r2v2), ordre et étiquettes <Picture 1>/<Picture 2> dans le prompt final,
métadonnées des références, MP4 lisible, frames horodatées. L'attribution visuelle (qui est qui dans la vidéo) se
contrôle sur les frames extraites, listées en fin de test.

  python app/backend/tests/acceptance_video_refs_real.py <image_sujet_1> <image_sujet_2>
  python app/backend/tests/acceptance_video_refs_real.py --job <job_id>   (vérifie une génération existante)
Écrit <installation>\\logs\\acceptance_video_refs.json.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D  # noqa: E402

C = Atelier3D(author="Claude Code")
R: dict = {"checks": []}


def check(name, ok, **info):
    R["checks"].append({"check": name, "ok": bool(ok), **info})
    print(("OK   " if ok else "ÉCHEC") + f" {name} {json.dumps(info, ensure_ascii=False)[:500]}", flush=True)


if sys.argv[1] == "--job":  # vérifier une génération déjà faite, sans la relancer
    vjid = sys.argv[2]
    jj = C.job(vjid)
    check("video_run_termine", jj["status"] == "succeeded", status=jj["status"], job=vjid)
else:
    a, b = sys.argv[1], sys.argv[2]
    ua, ub = C.upload(a), C.upload(b)
    slots = [{"id": "aventurier", "role": "subject", "label": "Aventurier", "description": "man in olive and grey sci-fi outfit, brown boots",
              "upload_id": ua["id"]},
             {"id": "coffre", "role": "subject", "label": "Coffre", "description": "wooden treasure chest with iron bands and a brass lock",
              "upload_id": ub["id"]}]
    prompt = ("The man from <Picture 1> walks up to the wooden treasure chest from <Picture 2>, kneels down beside it and slowly lifts its lid; "
              "warm golden light shines from inside onto his face. Static camera, plain light grey studio background.")
    g = C.create_from_preset("references_to_video", prompt=prompt, slots=slots, seed=31415, name="Deux références")["graph"]
    v = next(n for n in g["nodes"] if n["id"] == "video")
    ops = [{"op": "update_node", "node_id": "video", "params": {**v["params"], "duration_s": 5, "width": 480, "height": 864}}]
    g = C.patch_graph(g["id"], ops, expected_revision=g["revision"])["graph"]
    plan = C.plan(g["id"])
    check("plan_sans_erreur", not plan.get("errors"), errors=plan.get("errors"))
    run = C.run_graph(g["id"], idempotency_key=f"accept-video-refs-{g['id']}")
    r = C.wait_run(g["id"], run["run_id"], timeout=3 * 3600)
    check("video_run_termine", r["status"] == "succeeded", status=r["status"], steps=[(j["node_id"], j["step"], j["status"]) for j in r["jobs"]])
    vj = next(j for j in r["jobs"] if j["node_id"] == "video")
    vjid = vj.get("job_id") or vj["id"]

arts = C.artifacts(vjid)
meta_art = next((x for x in arts if x["role"] == "video_metadata"), None)
home = pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])
if meta_art:
    dl = C.download(meta_art["id"], home / "logs" / "acceptance_video_refs")
    meta = json.loads(pathlib.Path(dl["path"]).read_text(encoding="utf-8"))
    check("workflow_deux_references", meta["workflow_id"].endswith("_r2v2"), workflow=meta["workflow_id"])
    pf = meta.get("prompt_final") or ""
    check("prompt_final_etiquettes_ordonnees", pf.find("<Picture 1> : Aventurier") >= 0 and pf.find("<Picture 2> : Coffre") > pf.find("<Picture 1>"),
          prompt_final=pf[:400])
    check("references_dans_metadonnees", [x["slot"] for x in meta["references"]] == ["aventurier", "coffre"], references=meta["references"])
    vinfo = meta.get("video") or {}
    check("mp4_lisible", (vinfo.get("frames") or 0) > 24 and vinfo.get("width") and vinfo.get("height"), video=vinfo)
    R["video_metadata"] = meta
frames = [x for x in arts if x["role"] == "video_frame"]
check("frames_horodatees", len(frames) >= 3, n=len(frames))
R["frames"] = []
for f in frames:
    d = C.download(f["id"], home / "logs" / "acceptance_video_refs")
    R["frames"].append(d["path"])
    print("frame :", d["path"], flush=True)
vid = next((x for x in arts if x["role"] == "video"), None)
if vid:
    d = C.download(vid["id"], home / "logs" / "acceptance_video_refs")
    R["video_path"] = d["path"]
    print("vidéo :", d["path"], flush=True)
(home / "logs" / "acceptance_video_refs.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
print("Résumé :", sum(c["ok"] for c in R["checks"]), "/", len(R["checks"]), "vérifications OK", flush=True)
