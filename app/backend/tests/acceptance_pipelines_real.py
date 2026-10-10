"""Tests d'acceptation RÉELS : objet statique (sans rig), maillage existant → rig seul, délai des événements,
reconnexion du flux d'événements (Last-Event-ID).

  python app/backend/tests/acceptance_pipelines_real.py <glb_existant>
Écrit <installation>\\logs\\acceptance_pipelines.json.
"""
from __future__ import annotations

import json
import os
import pathlib
import statistics
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D  # noqa: E402

C = Atelier3D(author="Claude Code")
R: dict = {"checks": []}


def check(name, ok, **info):
    R["checks"].append({"check": name, "ok": bool(ok), **info})
    print(("OK   " if ok else "ÉCHEC") + f" {name} {json.dumps(info, ensure_ascii=False)[:400]}", flush=True)


latencies: list[float] = []
seen_ids: list[int] = []
stop = threading.Event()


def listen():
    """Client SSE indépendant : mesure (réception - horodatage serveur) pour chaque événement."""
    import datetime as dt

    for ev in C.watch(after=C.health()["events_last_id"]):
        recv = time.time()
        ts = dt.datetime.fromisoformat(ev["ts"]).timestamp()
        latencies.append(recv - ts)
        seen_ids.append(ev["id"])
        if stop.is_set():
            return


th = threading.Thread(target=listen, daemon=True)
th.start()

# 1. objet statique : prompt → image détourée → 3D → préparation → export, sans armature
g = C.create_from_preset("prompt_to_character", prompt="A single wooden treasure chest with iron bands and a brass lock, three-quarter view, "
                         "centered, plain light grey background, no text, game asset", category="static", seed=5150, name="Coffre",
                         target_height_m=0.6, triangle_budget=15000)["graph"]
types = [n["type"] for n in g["nodes"]]
check("objet_sans_noeud_rig", "mesh.rig" not in types and "mesh.validate" not in types, nodes=types)
run = C.run_graph(g["id"], idempotency_key=f"accept-object-{g['id']}")
r = C.wait_run(g["id"], run["run_id"], timeout=3 * 3600)
check("objet_run_termine", r["status"] == "succeeded", status=r["status"], steps=[(j["node_id"], j["step"], j["status"]) for j in r["jobs"]])
ex = next((j for j in r["jobs"] if j["node_id"] == "export"), None)
if ex and ex["status"] == "succeeded":
    rep = C.validation(next(x["id"] for x in C.jobs(graph_id=g["id"]) if x["kind"] == "graph_run"))["reports"]
    v = next((x["data"] for x in rep if "verification" in x["data"]), {})
    check("objet_export_verifie_sans_armature", v.get("status") == "export Blender vérifié" and not v.get("rigged")
          and (v.get("verification") or {}).get("blend", {}).get("armatures") == 0,
          status=v.get("status"), dims=(v.get("export") or {}).get("dimensions_m"))

# 2. maillage existant → rig seul
mesh_path = sys.argv[1]
up = C.upload(mesh_path)
job = C.create_job("rig_existing_mesh", {"profile": "humanoid", "name": "Rig_existant", "target_height_m": 1.8}, upload_id=up["id"],
                   idempotency_key=f"accept-rig-{up['sha256'][:16]}")
j = C.wait(job["job_id"], timeout=3 * 3600)
kids = [(c["node_id"], c["step"], c["status"]) for c in j.get("children", [])]
check("rig_seul_sans_generation", all(n in ("rig", "validate", "export") for n, _, _ in kids), etapes=kids)
check("rig_seul_resultat", j["status"] == "succeeded", final=j.get("final_status"))

# 3. délai des événements (session locale) et reconnexion par identifiant
stop.set()
time.sleep(1)
if latencies:
    lat = sorted(latencies)
    p95 = lat[int(0.95 * (len(lat) - 1))]
    check("delai_evenements_moins_de_2s", p95 < 2.0, n=len(lat), median_s=round(statistics.median(lat), 3), p95_s=round(p95, 3), max_s=round(lat[-1], 3))
last = seen_ids[-10] if len(seen_ids) > 10 else 0
replay = C.events(after=last)["events"]
check("rejeu_par_identifiant", [e["id"] for e in replay[:9]] == seen_ids[-9:][:len(replay[:9])], rejoues=len(replay))

home = pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])
(home / "logs" / "acceptance_pipelines.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
print("Résumé :", sum(c["ok"] for c in R["checks"]), "/", len(R["checks"]), "vérifications OK", flush=True)
