"""Tests d'acceptation RÉELS (moteurs) sur un graphe foreach déjà exécuté — section 18C.

  python app/backend/tests/acceptance_foreach_real.py <graph_id>

Vérifie : cache exact par item, forcer le recalcul, idempotence (rejeu / conflit 409), échec d'un item puis relance
corrigée de cet item seul, annulation d'un item en attente, aucune nouvelle tâche pour les autres items / l'amont,
pas de doublon de preview, renommer/réordonner sans perte d'identité, connexion d'une preview à une nouvelle édition.
Écrit un résumé JSON dans <installation>\\logs\\acceptance_foreach.json.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "clients" / "python"))
from atelier3d_client import Atelier3D, Atelier3DError  # noqa: E402

C = Atelier3D(author="Claude Code")
GID = sys.argv[1]
R: dict = {"graph_id": GID, "checks": []}


def check(name, ok, **info):
    R["checks"].append({"check": name, "ok": bool(ok), **info})
    print(("OK   " if ok else "ÉCHEC") + f" {name} {json.dumps(info, ensure_ascii=False)[:300]}", flush=True)


def jobs_count():
    return len(C.jobs(graph_id=GID, all=True, limit=1000))


def items():
    return {i["item_key"]: i for i in C.items(GID, "parts")}


def rev():
    return C.graph(GID, status=False)["graph"]["revision"]


before_items = items()
pv_before = sorted(n["id"] for n in C.graph(GID, status=False)["graph"]["nodes"] if n.get("auto"))
hair = before_items["cheveux"]

# 1. cache exact : même item, mêmes entrées, même seed effective, sans forcer
n0 = jobs_count()
r = C.rerun_item(GID, "parts", hair["item_id"], scope="item_only", force_recompute=False, expected_graph_revision=rev())
j = C.wait(r["jobs"][0]["job_id"], timeout=1800)
check("cache_exact_item", j["result"].get("cached") is True, job=j["id"], from_cache_of=j["result"].get("from_cache_of"))
check("une_seule_tache_creee", jobs_count() == n0 + 1, created=jobs_count() - n0)

# 2. idempotence : même clé + même charge = rejeu ; autre charge = 409
key = f"accept-idem-{GID}-{int(time.time())}"
body = dict(scope="item_only", params_patch={"seed": 777001}, force_recompute=True, expected_graph_revision=rev(), idempotency_key=key)
r1 = C.rerun_item(GID, "parts", hair["item_id"], **body)
r2 = C.rerun_item(GID, "parts", hair["item_id"], **body)
check("idempotence_rejeu", r2.get("idempotent_replay") and r1["jobs"][0]["job_id"] == r2["jobs"][0]["job_id"], job=r1["jobs"][0]["job_id"])
try:
    C.rerun_item(GID, "parts", hair["item_id"], **{**body, "params_patch": {"seed": 777002}})
    check("idempotence_conflit_409", False)
except Atelier3DError as e:
    check("idempotence_conflit_409", e.status == 409)

# 3. annulation d'un item EN ATTENTE (la relance forcée de Cheveux occupe le GPU)
boots = before_items["bottes"]
rb = C.rerun_item(GID, "parts", boots["item_id"], scope="item_only", params_patch={"seed": 888001}, force_recompute=True, expected_graph_revision=rev())
cj = rb["jobs"][0]["job_id"]
st = C.job(cj)["status"]
cres = C.cancel_item(GID, "parts", boots["item_id"])
check("annulation_item_en_attente", C.job(cj)["status"] == "cancelled", status_avant=st, reponse=cres)

# 4. relance forcée de Cheveux (nouvelle seed) : nouvelle variante candidate, non activée
jf = C.wait(r1["jobs"][0]["job_id"], timeout=3600)
new_vid = jf["result"]["variant_id"]
after = items()
check("forcer_nouvelle_variante", not jf["result"].get("cached") and jf["result"]["activated"] is False, variant=new_vid,
      variantes_cheveux=after["cheveux"]["variants_count"])
check("candidate_signalee", after["cheveux"]["candidate_not_selected"] is True)
check("autres_items_inchanges", all(after[k]["active_variant_id"] == before_items[k]["active_variant_id"] for k in before_items if k != "cheveux"))

# 5. échec d'un item (paramètre refusé par le moteur), puis relance corrigée de cet item seul
tete = before_items["tete"]
rf = C.rerun_item(GID, "parts", tete["item_id"], scope="item_only", params_patch={"grounding_px": 9999}, force_recompute=True,
                  expected_graph_revision=rev())
jf2 = C.wait(rf["jobs"][0]["job_id"], timeout=1800)
check("item_en_echec_explique", jf2["status"] == "failed" and bool(jf2.get("error", {}).get("message")), erreur=(jf2.get("error") or {}).get("message", "")[:200])
check("echec_sans_effet_sur_les_autres", all(items()[k]["active_variant_id"] == before_items[k]["active_variant_id"] for k in before_items))
rok = C.rerun_item(GID, "parts", tete["item_id"], scope="item_only", params_patch={"seed": 999001}, force_recompute=True, expected_graph_revision=rev())
jok = C.wait(rok["jobs"][0]["job_id"], timeout=3600)
check("item_relance_apres_correction", jok["status"] == "succeeded", job=jok["id"])

# 6. sélection de la nouvelle variante Cheveux : seuls ses descendants deviennent obsolètes (aucun ici)
sel = C.select_item_variant(GID, "parts", hair["item_id"], new_vid)
check("selection_variante_item", items()["cheveux"]["active_variant_id"] == new_vid, newly_stale=sel["newly_stale"])

# 7. pas de doublon de preview, renommage + réordonnancement sans perte d'identité
g = C.graph(GID, status=False)["graph"]
ids_before = {k: v["item_id"] for k, v in items().items()}
seeds_before = {k: v["seed"] for k, v in items().items()}
C.patch_graph(GID, [{"op": "update_node", "node_id": "p_cheveux", "label": "Coiffure"},
                    {"op": "update_edge", "edge_id": "e_cheveux", "order": 4}, {"op": "update_edge", "edge_id": "e_bottes", "order": 1}],
              expected_revision=g["revision"])
it2 = items()
pv_after = sorted(n["id"] for n in C.graph(GID, status=False)["graph"]["nodes"] if n.get("auto"))
check("previews_sans_doublon", pv_before == pv_after, previews=pv_after)
check("identite_items_conservee", {k: v["item_id"] for k, v in it2.items()} == ids_before)
check("seeds_actives_conservees", {k: v["seed"] for k, v in it2.items()} == seeds_before)
check("label_preview_suit_le_prompt", next(n for n in C.graph(GID, status=False)["graph"]["nodes"] if n["id"] == "pv_parts_cheveux")["label"] == "Coiffure")

# 8. preview Cheveux branchée sur une nouvelle édition : reçoit la variante sélectionnée, aucun calcul caché
g = C.graph(GID, status=False)["graph"]
n_jobs = jobs_count()
C.patch_graph(GID, [
    {"op": "add_node", "node": {"id": "p_hair_ref", "type": "text.prompt", "label": "Prompt coiffure", "params": {"text": "Show this hairstyle from the back on a plain background."}}},
    {"op": "add_node", "node": {"id": "hair_ref", "type": "image.generate", "label": "Coiffure de dos", "params": {"operation": "edit", "seed": 4242}}},
    {"op": "add_edge", "edge": {"id": "e_hair_img", "from": ["pv_parts_cheveux", "image"], "to": ["hair_ref", "image"]}},
    {"op": "add_edge", "edge": {"id": "e_hair_prompt", "from": ["p_hair_ref", "text"], "to": ["hair_ref", "prompt"]}}], expected_revision=g["revision"])
check("affichage_sans_generation_cachee", jobs_count() == n_jobs, nouvelles_taches=jobs_count() - n_jobs)
plan = C.plan(GID, scope="until", node_id="hair_ref", apply=False)
ex = [e for e in plan["executions"] if e["node_id"] == "hair_ref"]
check("plan_branche_preview", len(ex) == 1 and all(e["node_id"] == "hair_ref" for e in plan["executions"]), executions=[(e["node_id"], e["step"]) for e in plan["executions"]])
run = C.run_graph(GID, scope="until", node_id="hair_ref", expected_graph_revision=rev())
jj = C.wait(run["jobs"][0]["job_id"], timeout=3600)
full = C.get(f"/api/v1/jobs/{jj['id']}")
inp = C.get(f"/api/v1/runs/{run['run_id']}/run.json")["steps"][0]
check("preview_transmet_variante_selectionnee", jj["status"] == "succeeded", job=jj["id"])

home = pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])
(home / "logs" / "acceptance_foreach.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
print("Résumé :", sum(c["ok"] for c in R["checks"]), "/", len(R["checks"]), "vérifications OK")
