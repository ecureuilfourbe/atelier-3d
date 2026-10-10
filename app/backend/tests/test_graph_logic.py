"""Tests de LOGIQUE du graphe (sans moteur IA) : foreach, previews automatiques, identité des items, invalidation,
idempotence, refus de branchements. Les sorties des tâches sont simulées par des variantes insérées directement :
ces tests ne valident PAS la chaîne IA (voir TEST_REPORT.md pour les exécutions réelles).

Exécution : <venv-app>\\python.exe -m pytest app/backend/tests  (ou python app/backend/tests/test_graph_logic.py)
"""
from __future__ import annotations

import copy
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT.parents[1]
sys.path.insert(0, str(ROOT))

from atelier3d.db import Database, dumps, new_id, now  # noqa: E402
from atelier3d.graph.model import GraphError, GraphStore  # noqa: E402
from atelier3d.graph.planner import Planner  # noqa: E402
from atelier3d.graph.state import StateStore, derive_item_seed  # noqa: E402
from atelier3d.jobs import JobError, JobService  # noqa: E402
from atelier3d import presets  # noqa: E402

CAPS = {k: {"status": "disponible"} for k in ("image.create", "image.edit", "image.remove_background", "mesh.generate", "blender", "rig.mia",
                                               "video.minimax", "local")}
ALIASES = {"image_create_default": "krea2_turbo_t2i", "image_edit_default": "krea2_identity_edit",
           "remove_background_default": "birefnet_remove_background", "image_to_mesh_default": "pixal3d_image_to_model",
           "minimax_user": "minimax_h3_ultra_user"}


def env():
    d = pathlib.Path(tempfile.mkdtemp())
    db = Database(d / "t.sqlite3")
    g, s, j = GraphStore(db), StateStore(db), JobService(db)
    p = Planner(db, g, s, capabilities_fn=lambda: CAPS, aliases_fn=lambda: ALIASES)
    return db, g, s, j, p


def fake_success(db, state, job):
    """Simule la réussite d'une tâche : crée une variante comme le ferait le worker."""
    payload = job["payload"]
    resolved = {}
    for k, v in payload["inputs"].items():
        resolved[k] = _resolve(db, v)
    ident = {k: StateStore.ref_identity(v) for k, v in sorted(resolved.items())}
    from atelier3d.graph.state import sha

    fp = sha({"inputs": ident, "params": payload["step_params"]})
    vid = new_id("var")
    with db.tx() as c:
        c.execute("INSERT INTO variants(id, graph_id, node_id, step, item_id, job_id, run_id, status, author, params, seed, inputs, input_fingerprint, "
                  "cache_key, outputs, engine, stats, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vid, job["graph_id"], job["node_id"], job["step"], job["item_id"] or "", job["id"], job["run_id"], "succeeded", "test",
                   dumps(payload["step_params"]), payload.get("seed"), dumps(resolved), fp, None, dumps({"image": []}), "{}", "{}", now()))
        r = c.execute("SELECT active_variant_id FROM slots WHERE graph_id=? AND node_id=? AND step=? AND item_id=?",
                      (job["graph_id"], job["node_id"], job["step"], job["item_id"] or "")).fetchone()
        pol = payload["activate"]
        if pol == "always" or not (r and r["active_variant_id"]):
            state.set_active(job["graph_id"], job["node_id"], job["step"], job["item_id"] or "", vid, author="test", conn=c)
        c.execute("UPDATE jobs SET status='succeeded', result=? WHERE id=?", (dumps({"variant_id": vid}), job["id"]))
    return vid


def _resolve(db, v):
    if isinstance(v, list):
        return [_resolve(db, x) for x in v]
    if isinstance(v, dict) and v.get("kind") == "job":
        r = db.one("SELECT result FROM jobs WHERE id=?", (v["job_id"],))
        import json

        return {"kind": "variant", "variant_id": json.loads(r["result"])["variant_id"]}
    return v


def run_all(db, state, jobsvc, run):
    done = set()
    for _ in range(100):
        progressed = False
        for jj in run["jobs"]:
            if jj["job_id"] in done:
                continue
            if all(d in done for d in jj["depends_on"]):
                fake_success(db, state, jobsvc.get(jj["job_id"]))
                done.add(jj["job_id"])
                progressed = True
        if not progressed:
            break
    return done


def _chain(g):
    """prompt → générer (créer, sans seed) → préparer : le cas du graphe signalé (« tout se relance »)."""
    return g.create({"schema_version": "1.1", "name": "chaîne", "nodes": [
        {"id": "p", "type": "text.prompt", "params": {"text": "A"}},
        {"id": "gen", "type": "image.generate", "params": {"operation": "create"}},
        {"id": "prep", "type": "image.prepare", "params": {}}],
        "edges": [{"id": "e1", "from": ["p", "text"], "to": ["gen", "prompt"]}, {"id": "e2", "from": ["gen", "image"], "to": ["prep", "image"]}]},
        author="test")


def test_generative_node_gets_fixed_seed():
    db, g, s, j, p = env()
    doc = _chain(g)
    seed = next(n for n in doc["nodes"] if n["id"] == "gen")["params"].get("seed")
    assert isinstance(seed, int), "une node générative sans seed reçoit une seed fixe à la création"
    doc, _ = g.patch(doc["id"], [{"op": "add_node", "node": {"id": "gen2", "type": "image.generate", "params": {}}}], author="test")
    assert isinstance(next(n for n in doc["nodes"] if n["id"] == "gen2")["params"].get("seed"), int)


def test_executing_a_stale_node_updates_it_once():
    db, g, s, j, p = env()
    doc = _chain(g)
    run_all(db, s, j, p.create_run(doc["id"], author="test", jobs_service=j))
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "p", "params": {"text": "B"}}], author="test")
    assert s.graph_status(doc)["gen"]["status"] == "stale"
    r = p.create_run(doc["id"], scope="until", node_id="prep", author="test", jobs_service=j)
    assert {(x["node_id"], j.get(x["job_id"])["payload"]["activate"]) for x in r["jobs"]} == {("gen", "always"), ("prep", "always")}
    run_all(db, s, j, r)
    st = s.graph_status(g.get(doc["id"]))
    assert st["gen"]["status"] == "ok" and st["prep"]["status"] == "ok", "l'actualisation doit rendre le résultat utilisé"
    again = p.create_run(doc["id"], scope="until", node_id="prep", author="test", jobs_service=j)
    assert again["run_id"] is None, "une seconde exécution ne doit rien recalculer (plus de boucle)"


def test_execute_reuses_existing_up_to_date_variant():
    db, g, s, j, p = env()
    doc = _chain(g)
    run_all(db, s, j, p.create_run(doc["id"], author="test", jobs_service=j))
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "p", "params": {"text": "B"}}], author="test")
    # « Régénérer » sur la node périmée : candidate calculée avec le nouveau prompt, non activée
    rr = p.create_run(doc["id"], scope="node_only", node_id="gen", params_patch={"seed": 7}, force=True, author="test", jobs_service=j)
    cand = sorted(run_all(db, s, j, rr))
    assert s.graph_status(g.get(doc["id"]))["gen"]["status"] == "stale"
    r = p.create_run(doc["id"], scope="until", node_id="prep", author="test", jobs_service=j)
    assert [a["node_id"] for a in r["adopted"]] == ["gen"], r["adopted"]
    assert {x["node_id"] for x in r["jobs"]} == {"prep"}, "l'image à jour existante est réutilisée, pas régénérée"
    assert cand


def test_mode_switch_moves_prompt_edges():
    """Cas signalé : prompt branché sur l'entrée simple, passage en « pour chaque prompt », mêmes prompts rebranchés sur
    « Prompts » : la noodle restée sur l'entrée simple (invisible dans l'interface) bloquait le lancement."""
    from atelier3d.graph.model import validate
    db, g, s, j, p = env()
    doc = g.create({"schema_version": "1.1", "name": "ballons", "nodes": [
        {"id": "prompt", "type": "text.prompt", "params": {"text": "a girl with a red ballon"}},
        {"id": "prompt_2", "type": "text.prompt", "params": {"text": "a girl with a yellow ballon"}},
        {"id": "generate", "type": "image.generate", "params": {"operation": "create"}}],
        "edges": [{"id": "e1", "from": ["prompt", "text"], "to": ["generate", "prompt"]}]}, author="test")
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "generate", "execution": {"mode": "foreach_prompts"}},
                                 {"op": "add_edge", "edge": {"id": "e2", "from": ["prompt", "text"], "to": ["generate", "prompts"], "item_key": "prompt"}},
                                 {"op": "add_edge", "edge": {"id": "e3", "from": ["prompt_2", "text"], "to": ["generate", "prompts"], "item_key": "prompt_2"}}],
                     author="test")
    assert not any(e["to"] == ["generate", "prompt"] for e in doc["edges"]), "doublon sur l'entrée simple retiré"
    assert not [e for e in validate(doc)["errors"] if e["code"] == "foreach_prompt"]
    # un prompt resté seul sur l'entrée simple est déplacé vers « Prompts » (au lieu d'être supprimé)
    doc, _ = g.patch(doc["id"], [{"op": "add_node", "node": {"id": "prompt_3", "type": "text.prompt", "params": {"text": "black"}}},
                                 {"op": "add_edge", "edge": {"id": "e4", "from": ["prompt_3", "text"], "to": ["generate", "prompt"]}}], author="test")
    e4 = next(e for e in doc["edges"] if e["id"] == "e4")
    assert e4["to"] == ["generate", "prompts"] and e4["item_key"] == "prompt_3"
    assert len([i for i in next(n for n in doc["nodes"] if n["id"] == "generate")["items"] if i.get("status") == "active"]) == 3
    # retour en mode unique avec un seul prompt : il revient sur l'entrée simple
    doc, _ = g.patch(doc["id"], [{"op": "remove_edge", "edge_id": "e3"}, {"op": "remove_edge", "edge_id": "e4"},
                                 {"op": "update_node", "node_id": "generate", "execution": {"mode": "single"}}], author="test")
    assert [e["to"] for e in doc["edges"] if e["to"][0] == "generate"] == [["generate", "prompt"]]


def test_legacy_edit_node_becomes_edit_type_with_same_fingerprint():
    """Séparation Générer / Éditer : un ancien graphe (exemple du cahier des charges, image.generate + operation « edit »)
    est converti en « Éditer une image » ; les réglages vus par l'étape sont identiques (résultats existants valides)."""
    import json as _json

    from atelier3d.graph import nodetypes as NT
    from atelier3d.graph.state import step_params
    db, g, s, j, p = env()
    spec = _json.loads((DIST / "examples" / "pnj_concept_apose_parts.graph.json").read_text(encoding="utf-8"))
    doc = g.create(spec, author="test")
    types = {n["id"]: n["type"] for n in doc["nodes"]}
    assert types["concept"] == "image.generate" and types["apose"] == "image.edit" and types["parts"] == "image.edit", types
    # valeurs exactes que calculait l'ANCIEN schéma (node combinée, défauts steps 10 / ref_boost 4 / grounding 768)
    edit_node = {"type": "image.edit", "params": {"operation": "edit", "seed": 1, "workflow_id": "image_edit_default", "ref_boost": 1.5}}
    st = NT.get("image.edit").step_list(edit_node)[0]
    assert st.id == "edit" and step_params(edit_node, st) == {"operation": "edit", "workflow_id": "image_edit_default", "width": 1024,
                                                              "height": 1024, "steps": 10, "ref_boost": 1.5, "grounding_px": 768,
                                                              "num_variants": 1}
    # node encore non convertie (graphe enregistré avant la séparation) : mêmes réglages que sa version convertie
    legacy = {"type": "image.generate", "params": dict(edit_node["params"])}
    assert step_params(legacy, NT.get("image.generate").step_list(legacy)[0]) == step_params(edit_node, NT.get("image.edit").step_list(edit_node)[0])
    gen_node = {"type": "image.generate", "params": {"operation": "create", "seed": 1}}
    st = NT.get("image.generate").step_list(gen_node)[0]
    assert st.id == "create" and step_params(gen_node, st) == {"operation": "create", "workflow_id": "", "width": 1024, "height": 1024,
                                                               "refine_prompt": False, "num_variants": 1}


def test_openai_engine_needs_key():
    db, g, s, j, p = env()
    doc = _chain(g)
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "gen", "params": {"engine": "openai"}}], author="test")
    gen = next(n for n in doc["nodes"] if n["id"] == "gen")
    from atelier3d.graph import nodetypes as NT
    st = NT.get(gen["type"]).step_list(gen)[0]
    assert (st.capability, st.resource) == ("image.openai", "net")
    p2 = Planner(db, g, s, capabilities_fn=lambda: {**CAPS, "image.openai": {"status": "indisponible",
                                                                             "reason": "Clé d'API OpenAI non configurée"}},
                 aliases_fn=lambda: ALIASES)
    plan = p2.plan(doc["id"], apply=False)
    assert any("Clé d'API OpenAI" in e["message"] for e in plan["errors"]), plan["errors"]
    assert any(w["code"] == "external_service" for w in plan["warnings"]), plan["warnings"]


def test_foreach_plan_items_and_previews():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    parts = next(n for n in doc["nodes"] if n["id"] == "parts")
    assert len(parts["items"]) == 5
    previews = [n for n in doc["nodes"] if (n.get("auto") or {}).get("owner_node_id") == "parts"]
    assert len(previews) == 5 and sorted(x["label"] for x in previews) == sorted(["Tête", "Cheveux", "Tenue", "Accessoires", "Bottes"])
    plan = p.plan(doc["id"])
    assert plan["valid"], plan["errors"]
    assert plan["foreach"]["parts"]["summary"] == "5 prompts × 1 référence commune → 5 résultats"
    ex_parts = [e for e in plan["executions"] if e["node_id"] == "parts"]
    assert len(ex_parts) == 5 and len({e["item_id"] for e in ex_parts}) == 5
    # concept (1) + apose edit + rmbg (2) + 5 parties = 8 exécutions
    assert plan["counts"]["executions"] == 8


def test_seeds_stable_and_independent_of_order():
    assert derive_item_seed(43000, "cheveux") == derive_item_seed(43000, "cheveux")
    assert derive_item_seed(43000, "cheveux") != derive_item_seed(43000, "tete")


def test_run_then_item_rerun_and_targeted_invalidation():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    run = p.create_run(doc["id"], author="test", jobs_service=j)
    assert len(run["jobs"]) == 8
    # chaque item reçoit exactement UN prompt et la même référence A-pose (job de détourage)
    parts_jobs = [j.get(x["job_id"]) for x in run["jobs"] if x["node_id"] == "parts"]
    texts = {pj["payload"]["inputs"]["prompt"]["text"] for pj in parts_jobs}
    assert len(texts) == 5
    refs = {pj["payload"]["inputs"]["image"]["job_id"] for pj in parts_jobs}
    assert len(refs) == 1
    run_all(db, s, j, run)
    doc = g.get(doc["id"])
    st = s.graph_status(doc)
    assert st["parts"]["status"] == "ok", st["parts"]
    items = {i["item_key"]: i for i in next(n for n in doc["nodes"] if n["id"] == "parts")["items"]}
    cheveux = items["cheveux"]["item_id"]
    before_active = s.active_id(doc["id"], "parts", "edit", cheveux)
    # relance de Cheveux seul : un seul job, nouvelle variante candidate (non active)
    rr = p.create_run(doc["id"], scope="item_only", node_id="parts", item_id=cheveux, params_patch={"seed": 53002}, force=True,
                      author="Claude Code", jobs_service=j)
    assert len(rr["jobs"]) == 1 and rr["jobs"][0]["item_id"] == cheveux and rr["jobs"][0]["seed"] == 53002
    run_all(db, s, j, rr)
    assert s.active_id(doc["id"], "parts", "edit", cheveux) == before_active  # pas d'activation automatique
    assert len(s.list_variants(doc["id"], "parts", item_id=cheveux)) == 2
    # modification du seul prompt Cheveux : seul cet item devient obsolète
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "p_cheveux", "params": {"text": "Coiffure modifiée"}}], author="test")
    st = s.graph_status(doc)
    slots = {x["item_id"]: x["status"] for x in st["parts"]["slots"]}
    assert slots[cheveux] == "stale" and sum(1 for v in slots.values() if v == "stale") == 1
    # seule la preview de Cheveux passe « amont à recalculer » ; les previews des 4 autres items restent à jour
    assert st["pv_parts_cheveux"]["status"] == "upstream", st["pv_parts_cheveux"]
    assert all(st[f"pv_parts_{k}"]["status"] == "ok" for k in ("tete", "tenue", "accessoires", "bottes")), \
        {k: st[f"pv_parts_{k}"]["status"] for k in ("tete", "tenue", "accessoires", "bottes")}
    # nouvelle A-pose sélectionnée : les 5 items deviennent obsolètes
    apose_v = s.list_variants(doc["id"], "apose", step="remove_background")[0]
    rr2 = p.create_run(doc["id"], scope="node_only", node_id="apose", force=True, author="test", jobs_service=j)
    run_all(db, s, j, rr2)
    newv = s.list_variants(doc["id"], "apose", step="remove_background")[-1]
    assert newv["id"] != apose_v["id"]
    st = s.graph_status(doc)
    assert all(x["status"] != "stale" for x in st["parts"]["slots"] if x["item_id"] != cheveux)  # candidate non sélectionnée : rien ne change
    s.set_active(doc["id"], "apose", "remove_background", "", newv["id"], author="test")
    st = s.graph_status(doc)
    assert sum(1 for x in st["parts"]["slots"] if x["status"] == "stale") == 5


def test_rename_reorder_keep_identity_and_previews():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    ids_before = {i["item_key"]: i["item_id"] for i in next(n for n in doc["nodes"] if n["id"] == "parts")["items"]}
    pv_before = sorted(n["id"] for n in doc["nodes"] if n.get("auto"))
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "p_cheveux", "label": "Coiffure"},
                                 {"op": "update_edge", "edge_id": "e_cheveux", "order": 9}, {"op": "update_edge", "edge_id": "e_tete", "order": 7}],
                     author="test")
    parts = next(n for n in doc["nodes"] if n["id"] == "parts")
    assert {i["item_key"]: i["item_id"] for i in parts["items"]} == ids_before
    assert sorted(n["id"] for n in doc["nodes"] if n.get("auto")) == pv_before  # pas de doublon de preview
    assert next(n for n in doc["nodes"] if n["id"] == "pv_parts_cheveux")["label"] == "Coiffure"


def test_sixth_prompt_rejected_then_limit_raised():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    doc, _ = g.patch(doc["id"], [{"op": "add_node", "node": {"id": "p_gants", "type": "text.prompt", "label": "Gants", "params": {"text": "Isole les gants."}}},
                                 {"op": "add_edge", "edge": {"id": "e_gants", "from": ["p_gants", "text"], "to": ["parts", "prompts"], "item_key": "gants", "order": 5}}],
                     author="test")
    plan = p.plan(doc["id"])
    assert not plan["valid"] and any(e["code"] == "max_items" for e in plan["errors"])
    try:
        p.create_run(doc["id"], author="test", jobs_service=j)
        raise AssertionError("le lancement aurait dû être refusé")
    except GraphError as e:
        assert e.code == "plan_invalid"
    parts = next(n for n in doc["nodes"] if n["id"] == "parts")
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "parts", "execution": {**parts["execution"], "max_items": 6}}], author="test")
    plan = p.plan(doc["id"])
    assert plan["valid"]
    assert len(plan["foreach"]["parts"]["items"]) == 6


def test_text_only_engine_refuses_reference_and_type_mismatch():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    # « Générer une image » (texte seul) n'a pas d'entrée image : le branchement est refusé dès la modification
    try:
        g.patch(doc["id"], [{"op": "add_node", "node": {"id": "imp", "type": "image.import", "params": {"upload_id": "upl_x"}}},
                            {"op": "add_edge", "edge": {"id": "e_bad", "from": ["imp", "image"], "to": ["concept", "image"]}}], author="test")
        raise AssertionError("image branchée sur « Générer une image » acceptée")
    except GraphError as e:
        assert any(d["code"] in ("port", "text_only_engine") for d in e.details), e.details
    try:
        g.patch(doc["id"], [{"op": "add_edge", "edge": {"id": "e_type", "from": ["p_concept", "text"], "to": ["parts", "image"]}}], author="test")
        raise AssertionError("branchement incompatible accepté")
    except GraphError as e:
        assert any(d["code"] == "type_mismatch" for d in e.details)


def test_prompt_removed_archives_item_and_preview():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    doc, _ = g.patch(doc["id"], [{"op": "remove_edge", "edge_id": "e_bottes"}], author="test")
    parts = next(n for n in doc["nodes"] if n["id"] == "parts")
    bottes = next(i for i in parts["items"] if i["item_key"] == "bottes")
    assert bottes["status"] == "archived"
    pv = next(n for n in doc["nodes"] if n["id"] == "pv_parts_bottes")
    assert pv["archived"] is True


def test_snapshot_kept_when_graph_edited_during_run():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    run = p.create_run(doc["id"], author="test", jobs_service=j)
    g.patch(doc["id"], [{"op": "update_node", "node_id": "p_concept", "params": {"text": "Autre concept"}}], author="test")
    concept_job = j.get(next(x["job_id"] for x in run["jobs"] if x["node_id"] == "concept"))
    assert concept_job["payload"]["inputs"]["prompt"]["text"].startswith("Concept art d'un seul PNJ")
    r = db.one("SELECT revision FROM runs WHERE id = ?", (run["run_id"],))
    assert r["revision"] < g.get(doc["id"])["revision"]


def test_idempotency():
    db, g, s, j, p = env()
    calls = []
    f = lambda: calls.append(1) or {"ok": len(calls)}  # noqa: E731
    a = j.idempotent("k1", "ep", {"x": 1}, f)
    b = j.idempotent("k1", "ep", {"x": 1}, f)
    assert a["ok"] == 1 and b["ok"] == 1 and b.get("idempotent_replay") and len(calls) == 1
    try:
        j.idempotent("k1", "ep", {"x": 2}, f)
        raise AssertionError("même clé + autre charge acceptée")
    except JobError as e:
        assert e.status == 409


def test_pinned_link_stays_valid():
    db, g, s, j, p = env()
    doc = g.create(presets.build("pnj_concept_apose_parts", {}), author="test")
    run = p.create_run(doc["id"], author="test", jobs_service=j)
    run_all(db, s, j, run)
    v1 = s.active_id(doc["id"], "concept", "create", "")
    doc, _ = g.patch(doc["id"], [{"op": "update_edge", "edge_id": "e_concept_image_apose_image", "link": {"mode": "pinned", "variant_id": v1}}], author="test")
    rr = p.create_run(doc["id"], scope="node_only", node_id="concept", force=True, author="test", jobs_service=j)
    run_all(db, s, j, rr)
    v2 = s.list_variants(doc["id"], "concept")[-1]["id"]
    s.set_active(doc["id"], "concept", "create", "", v2, author="test")
    st = s.graph_status(g.get(doc["id"]))
    assert st["apose"]["status"] == "ok"  # épinglé à v1 : reste valide


# ---------------------------------------------------------------------------- Préparer l'image (pose / fond), upscale, prompts aléatoires

def test_prepare_options_keep_existing_results_valid():
    """Sans pose et avec détourage (défauts) : mêmes étapes et mêmes réglages qu'avant les options → résultats toujours à jour."""
    from atelier3d.graph import nodetypes as NT
    from atelier3d.graph.state import step_params

    old = {"id": "prep", "type": "image.prepare", "params": {"crop_to_subject": True, "padding": 0.08}}
    steps = NT.get("image.prepare").step_list(old)
    assert [s.id for s in steps] == ["remove_background", "crop"]
    assert step_params(old, steps[0]) == {} and step_params(old, steps[1]) == {"crop_to_subject": True, "padding": 0.08}
    pose = {"id": "prep", "type": "image.prepare", "params": {"pose": "t_pose"}}
    st = NT.get("image.prepare").step_list(pose)
    assert [s.id for s in st] == ["pose", "remove_background", "crop"] and st[0].capability == "image.edit" and st[0].generative
    assert [s.id for s in NT.get("image.prepare").step_list({"type": "image.prepare", "params": {"remove_background": False}})] == ["crop"]
    oa = NT.get("image.prepare").step_list({"type": "image.prepare", "params": {"pose": "a_pose", "engine": "openai"}})
    assert oa[0].capability == "image.openai" and oa[0].resource == "net"
    # chaîne réelle : la pose alimente le détourage (« previous »), qui alimente le cadrage
    db, g, s, j, pl = env()
    doc = _chain(g)
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "prep", "params": {"pose": "a_pose"}}], author="test")
    run = pl.create_run(doc["id"], author="test", jobs_service=j)
    prep = {x["step"]: j.get(x["job_id"]) for x in run["jobs"] if x["node_id"] == "prep"}
    assert list(prep) == ["pose", "remove_background", "crop"]
    assert prep["remove_background"]["payload"]["inputs"]["previous"]["job_id"] == prep["pose"]["id"]
    assert isinstance(prep["pose"]["payload"]["seed"], int)


def test_upscale_node_plans_one_step_per_engine():
    db, g, s, j, pl = env()
    caps = {**CAPS, "image.upscale.seedvr2": {"status": "disponible"}, "image.upscale.esrgan": {"status": "disponible"}}
    pl.capabilities_fn = lambda: caps
    pl.aliases_fn = lambda: {**ALIASES, "upscale_seedvr2_default": "seedvr2_3b_upscale_image", "upscale_esrgan_default": "realesrgan_upscale_image"}
    doc = _chain(g)
    doc, _ = g.patch(doc["id"], [{"op": "add_node", "node": {"id": "up", "type": "image.upscale", "params": {"resolution": "4k"}}},
                                 {"op": "add_edge", "edge": {"id": "e3", "from": ["prep", "image"], "to": ["up", "image"]}}], author="test")
    plan = pl.plan(doc["id"])
    assert plan["valid"], plan["errors"]
    ex = [e for e in plan["executions"] if e["node_id"] == "up"]
    assert len(ex) == 1 and ex[0]["capability"] == "image.upscale.seedvr2" and ex[0]["engine"]["workflow_id"] == "seedvr2_3b_upscale_image"
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "up", "params": {"engine": "esrgan"}}], author="test")
    ex = [e for e in pl.plan(doc["id"])["executions"] if e["node_id"] == "up"]
    assert ex[0]["capability"] == "image.upscale.esrgan"
    pl.aliases_fn = lambda: ALIASES  # modèles absents : refus explicite, aucune tâche
    assert any(e["code"] == "engine_unavailable" for e in pl.plan(doc["id"])["errors"])


def _randomize_graph(g, count=3):
    return g.create({"schema_version": "1.1", "name": "prompts aléatoires", "nodes": [
        {"id": "rnd", "type": "text.randomize", "label": "Village", "params": {"context": "village sur une planète désertique", "count": count}},
        {"id": "gen", "type": "image.generate", "params": {}, "execution": {"mode": "foreach_prompts"},
         "presentation": {"auto_previews": "per_item"}}],
        "edges": [{"id": "e1", "from": ["rnd", "prompts"], "to": ["gen", "prompts"]}]}, author="test")


def _write_prompts(db, s, doc, prompts, activate=True):
    """Variante simulée de « Prompts aléatoires » : artefact JSON dont les métadonnées portent les prompts."""
    import json

    aid, vid = new_id("art"), new_id("var")
    with db.tx() as c:
        c.execute("INSERT INTO artifacts(id, job_id, variant_id, kind, role, path, sha256, size, mime, meta, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, None, vid, "json", "prompts", "x/prompts.json", "0" * 64, 1, "application/json", json.dumps({"prompts": prompts}), now()))
        c.execute("INSERT INTO variants(id, graph_id, node_id, step, item_id, job_id, run_id, status, author, params, seed, inputs, input_fingerprint, "
                  "cache_key, outputs, engine, stats, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vid, doc["id"], "rnd", "generate", "", None, None, "succeeded", "test", "{}", 1, "{}", "x", None,
                   json.dumps({"prompts": [aid]}), "{}", "{}", now()))
        if activate:
            s.set_active(doc["id"], "rnd", "generate", "", vid, author="test", conn=c)
    return vid


def test_randomize_list_becomes_foreach_items():
    db, g, s, j, pl = env()
    pl.capabilities_fn = lambda: {**CAPS, "text.generate": {"status": "disponible"}}
    pl.aliases_fn = lambda: {**ALIASES, "text_generate_default": "qwen3vl_text_generate"}
    doc = _randomize_graph(g, 3)
    gen = next(n for n in doc["nodes"] if n["id"] == "gen")
    assert [(i["item_key"], i["list_index"]) for i in gen["items"]] == [("rnd_1", 0), ("rnd_2", 1), ("rnd_3", 2)]
    assert len([n for n in doc["nodes"] if (n.get("auto") or {}).get("owner_node_id") == "gen"]) == 3
    plan = pl.plan(doc["id"])
    assert plan["valid"], plan["errors"]
    assert plan["foreach"]["gen"]["summary"] == "3 prompts → 3 résultats"
    # un seul run : les items dépendent de la tâche « Prompts aléatoires » et recevront le n-ième prompt
    run = pl.create_run(doc["id"], author="test", jobs_service=j)
    rjob = next(x for x in run["jobs"] if x["node_id"] == "rnd")
    gj = [j.get(x["job_id"]) for x in run["jobs"] if x["node_id"] == "gen"]
    assert len(gj) == 3 and all(x["depends_on"] == [rjob["job_id"]] for x in gj)
    assert sorted(x["payload"]["inputs"]["prompt"]["text_index"] for x in gj) == [0, 1, 2]
    # le worker résout la tâche en TEXTE : même identité que l'état calculé ensuite → items « à jour » après le run
    vid = _write_prompts(db, s, doc, ["desert village at dawn", "cantina interior with neon", "moisture farm under twin suns"])
    ref = {"kind": "variant", "variant_id": vid, "node_id": "rnd"}
    t1 = s.list_text(ref, 1)
    assert t1["kind"] == "text" and t1["text"] == "cantina interior with neon"
    doc = g.get(doc["id"])
    gen = next(n for n in doc["nodes"] if n["id"] == "gen")
    it2 = next(i for i in gen["items"] if i["item_key"] == "rnd_2")
    ins = s.slot_inputs(doc, gen, 0, it2["item_id"])
    assert ins["prompt"]["kind"] == "text" and ins["prompt"]["sha"] == t1["sha"]
    # régénérer la liste en ne changeant que le 3e prompt : seul l'item 3 passe « à recalculer »
    for jj in run["jobs"]:
        if jj["node_id"] == "gen":
            job = j.get(jj["job_id"])
            idx = job["payload"]["inputs"]["prompt"]["text_index"]
            job["payload"]["inputs"]["prompt"] = s.list_text(ref, idx)
            fake_success(db, s, job)
    st = s.graph_status(g.get(doc["id"]))
    assert all(x["status"] == "ok" for x in st["gen"]["slots"]), st["gen"]
    _write_prompts(db, s, doc, ["desert village at dawn", "cantina interior with neon", "a sandcrawler parked at dusk"])
    st = s.graph_status(g.get(doc["id"]))
    by_item = {x["item_id"]: x["status"] for x in st["gen"]["slots"]}
    keys = {i["item_id"]: i["item_key"] for i in gen["items"]}
    assert {keys[k]: v for k, v in by_item.items()} == {"rnd_1": "ok", "rnd_2": "ok", "rnd_3": "stale"}


def test_randomize_count_change_adds_and_archives_items():
    db, g, s, j, pl = env()
    doc = _randomize_graph(g, 3)
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "rnd", "params": {"count": 5}}], author="test")
    gen = next(n for n in doc["nodes"] if n["id"] == "gen")
    assert [i["item_key"] for i in gen["items"] if i["status"] == "active"] == ["rnd_1", "rnd_2", "rnd_3", "rnd_4", "rnd_5"]
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "rnd", "params": {"count": 2}}], author="test")
    gen = next(n for n in doc["nodes"] if n["id"] == "gen")
    assert {i["item_key"]: i["status"] for i in gen["items"]} == {"rnd_1": "active", "rnd_2": "active", "rnd_3": "archived",
                                                                  "rnd_4": "archived", "rnd_5": "archived"}
    # une sortie « Prompt n » au-delà du nombre de prompts est refusée ; la liste ne se branche pas sur une entrée simple
    from atelier3d.graph.model import validate

    bad = copy.deepcopy(doc)
    bad["nodes"].append({"id": "one", "type": "image.generate", "params": {"seed": 1}})
    bad["edges"].append({"id": "e9", "from": ["rnd", "prompt:4"], "to": ["one", "prompt"]})
    assert any(e["code"] == "item_port" for e in validate(bad)["errors"])
    bad["edges"][-1] = {"id": "e9", "from": ["rnd", "prompts"], "to": ["one", "prompt"]}
    assert any(e["code"] == "type_mismatch" for e in validate(bad)["errors"])
    ok = copy.deepcopy(doc)
    ok["nodes"].append({"id": "one", "type": "image.generate", "params": {"seed": 1}})
    ok["edges"].append({"id": "e9", "from": ["rnd", "prompt:2"], "to": ["one", "prompt"]})
    assert not validate(ok)["errors"], validate(ok)["errors"]


def test_compose_waits_for_prompts_written_in_the_same_run():
    db, g, s, j, pl = env()
    pl.capabilities_fn = lambda: {**CAPS, "text.generate": {"status": "disponible"}}
    pl.aliases_fn = lambda: {**ALIASES, "text_generate_default": "qwen3vl_text_generate"}
    doc = g.create({"schema_version": "1.1", "name": "compose", "nodes": [
        {"id": "rnd", "type": "text.randomize", "params": {"context": "x", "count": 2}},
        {"id": "style", "type": "text.prompt", "params": {"text": "matte painting"}},
        {"id": "cmp", "type": "text.compose", "params": {}},
        {"id": "gen", "type": "image.generate", "params": {}}],
        "edges": [{"id": "e1", "from": ["rnd", "prompt:1"], "to": ["cmp", "fragments"], "order": 0},
                  {"id": "e2", "from": ["style", "text"], "to": ["cmp", "fragments"], "order": 1},
                  {"id": "e3", "from": ["cmp", "text"], "to": ["gen", "prompt"]}]}, author="test")
    run = pl.create_run(doc["id"], author="test", jobs_service=j)
    gj = j.get(next(x["job_id"] for x in run["jobs"] if x["node_id"] == "gen"))
    assert gj["payload"]["inputs"]["prompt"]["kind"] == "compose" and gj["depends_on"]
    vid = _write_prompts(db, s, doc, ["desert village", "spaceport"])
    from atelier3d.graph.state import compose_text

    parts = [s.list_text({"kind": "variant", "variant_id": vid, "node_id": "rnd"}, 0), gj["payload"]["inputs"]["prompt"]["parts"][1]]
    assert compose_text(parts, gj["payload"]["inputs"]["prompt"]["format"], "cmp")["text"] == "desert village, matte painting"
    st = s.graph_status(g.get(doc["id"]))
    assert st["gen"]["status"] == "missing"  # composé disponible : la génération attend seulement son premier calcul


def test_before_after_accepts_images_videos_and_3d():
    from atelier3d.graph.model import validate

    db, g, s, j, pl = env()
    doc = g.create({"schema_version": "1.1", "name": "avant/après", "nodes": [
        {"id": "img", "type": "image.import", "params": {"upload_id": "up_img"}},
        {"id": "msh", "type": "mesh.import", "params": {"upload_id": "up_msh"}},
        {"id": "cmp", "type": "compare.before_after", "params": {}}],
        "edges": [{"id": "e1", "from": ["img", "image"], "to": ["cmp", "before"]}]}, author="test")
    v = validate(doc)
    assert not v["errors"] and any(w["code"] == "compare_incomplete" for w in v["warnings"]), v
    assert s.graph_status(doc)["cmp"]["status"] == "missing"
    doc, _ = g.patch(doc["id"], [{"op": "add_edge", "edge": {"id": "e2", "from": ["msh", "mesh"], "to": ["cmp", "after"]}}], author="test")
    st = s.graph_status(doc)["cmp"]
    assert st["status"] == "ok" and set(st["refs"]) == {"before", "after"}, st
    assert st["refs"]["before"]["upload_id"] == "up_img" and st["refs"]["after"]["upload_id"] == "up_msh"
    plan = pl.plan(doc["id"])
    assert plan["valid"] and not [e for e in plan["executions"] if e["node_id"] == "cmp"], "aucun calcul pour la comparaison"
    # une vidéo et un maillage riggé sont acceptés ; un texte ne l'est pas
    from atelier3d.graph import nodetypes as NT

    assert all(NT.compatible(x, "Media") for x in ("Image", "Video", "Mesh", "RiggedMesh"))
    # deux vidéos importées
    doc2 = g.create({"schema_version": "1.1", "name": "vidéos", "nodes": [
        {"id": "v1", "type": "video.import", "params": {"upload_id": "up_v1"}}, {"id": "v2", "type": "video.import", "params": {"upload_id": "up_v2"}},
        {"id": "cmp", "type": "compare.before_after", "params": {"mode": "side_by_side"}}],
        "edges": [{"id": "a", "from": ["v1", "video"], "to": ["cmp", "before"]}, {"id": "b", "from": ["v2", "video"], "to": ["cmp", "after"]}]},
        author="test")
    assert not validate(doc2)["errors"] and s.graph_status(doc2)["cmp"]["refs"]["after"]["upload_id"] == "up_v2"
    assert not NT.compatible("Prompt", "Media") and not NT.compatible("Report", "Media")


def test_incomplete_node_elsewhere_does_not_block_other_branches():
    """Cas signalé : une « Générer vidéo » posée sans noodle (mode Image → vidéo) bloquait tout le graphe."""
    db, g, s, j, pl = env()
    doc = _chain(g)
    doc, _ = g.patch(doc["id"], [{"op": "add_node", "node": {"id": "generate_2", "type": "video.generate", "params": {}}}], author="test")
    plan = pl.plan(doc["id"], scope="until", node_id="prep")
    assert plan["valid"], plan["errors"]
    assert any(w["code"] == "outside_scope" and w["node_id"] == "generate_2" for w in plan["warnings"])
    plan = pl.plan(doc["id"], scope="graph")
    assert plan["valid"] and {e["node_id"] for e in plan["executions"]} == {"gen", "prep"}, plan["errors"]
    assert any(w["code"] == "skipped_invalid" and w["node_id"] == "generate_2" for w in plan["warnings"])
    plan = pl.plan(doc["id"], scope="until", node_id="generate_2")
    assert not plan["valid"] and any(e.get("node_id") == "generate_2" for e in plan["errors"]), "la node visée reste refusée"
    # node incomplète retirée : plus d'avertissement (les autres erreurs, ex. max_items, bloquent toujours : test dédié)
    doc, _ = g.patch(doc["id"], [{"op": "remove_node", "node_id": "generate_2"}], author="test")
    plan = pl.plan(doc["id"], scope="graph")
    assert plan["valid"] and not [w for w in plan["warnings"] if w["code"] in ("skipped_invalid", "outside_scope")]


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except Exception as e:  # noqa: BLE001
                import traceback

                fails += 1
                print(f"ÉCHEC {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)
