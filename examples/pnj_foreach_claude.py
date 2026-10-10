"""Scénario « PNJ : concept → A-pose détourée → 5 parties (foreach) » piloté sans clic, tel que Claude Code l'exécute.

Toutes les valeurs (graph_id, révision, item_id, variant_id) sont obtenues dynamiquement depuis l'API.

  python examples/pnj_foreach_claude.py run            # crée le graphe, planifie, exécute, attend, résume
  python examples/pnj_foreach_claude.py fix-hair <graph_id>   # relance UNIQUEMENT « Cheveux », inspecte, sélectionne
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D  # noqa: E402

C = Atelier3D(author="Claude Code")


def check_capabilities():
    caps = C.capabilities()["capabilities"]
    for cap in ("image.create", "image.edit", "image.remove_background"):
        st = caps[cap]["status"]
        print(f"  {cap:26s} {st}")
        if st == "indisponible":
            raise SystemExit(f"Capacité indisponible : {cap} — {caps[cap].get('reason')}")


def run() -> str:
    print("1. Capacités")
    check_capabilities()
    doc = json.loads((ROOT / "examples" / "pnj_concept_apose_parts.graph.json").read_text(encoding="utf-8"))
    g = C.create_graph(doc)["graph"]
    gid = g["id"]
    print(f"2. Graphe créé : {gid} (révision {g['revision']})")
    plan = C.plan(gid)
    fe = plan["foreach"]["parts"]
    print(f"   Plan : {fe['summary']} ; previews : {', '.join(fe['previews'])}")
    assert len(fe["items"]) == 5 and len(fe["previews"]) == 5, "le plan doit comporter 5 items et 5 previews"
    run = C.run_graph(gid, idempotency_key=f"pnj-run-{gid}")
    print(f"3. Run {run['run_id']} : {len(run['jobs'])} tâches")
    t0 = time.time()
    seen = {}

    def upd(r):
        line = " | ".join(f"{j['node_id']}{'[' + j['item_key'] + ']' if j.get('item_key') else ''}/{j['step']}={j['status']}" for j in r["jobs"])
        if line != seen.get("l"):
            seen["l"] = line
            print(f"   [{time.time() - t0:5.0f}s] {line}")

    r = C.wait_run(gid, run["run_id"], on_update=upd)
    print(f"   Run terminé : {r['status']} en {time.time() - t0:.0f} s")
    for it in C.items(gid, "parts"):
        img = (it["outputs"].get("image") or [{}])[0]
        print(f"   item {it['item_key']:12s} {it['item_id']}  seed={it['seed']}  variante={it['active_variant_id']}  image={img.get('path')}")
    return gid


def fix_hair(gid: str):
    g = C.graph(gid, status=False)["graph"]
    it = C.item_by_key(gid, "parts", "cheveux")
    print(f"Cheveux : item {it['item_id']}, variante active {it['active_variant_id']}")
    before = {x["item_key"]: x["variants_count"] for x in C.items(gid, "parts")}
    rr = C.rerun_item(gid, "parts", it["item_id"], scope="item_only", params_patch={"seed": 53002}, force_recompute=True,
                      activate_output=False, expected_graph_revision=g["revision"], idempotency_key=f"pnj-cheveux-candidate-2-{gid}")
    print(f"Relance : {len(rr['jobs'])} tâche(s) → {rr['jobs'][0]['job_id']} (portée {rr['scope']})")
    j = C.wait(rr["jobs"][0]["job_id"])
    vid = j["result"]["variant_id"]
    print(f"Nouvelle variante candidate {vid} (activée : {j['result']['activated']})")
    after = {x["item_key"]: x["variants_count"] for x in C.items(gid, "parts")}
    assert all(after[k] == before[k] for k in before if k != "cheveux") and after["cheveux"] == before["cheveux"] + 1
    sel = C.select_item_variant(gid, "parts", it["item_id"], vid)
    print(f"Sélection : nodes devenues à recalculer : {sel['newly_stale'] or 'aucune'}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fix-hair":
        fix_hair(sys.argv[2])
    else:
        gid = run()
        print(f"\nPour corriger seulement les cheveux : python {pathlib.Path(__file__).name} fix-hair {gid}")
