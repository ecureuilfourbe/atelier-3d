"""Tests de LOGIQUE de l'extraction de props (sans moteur IA) : lecture de l'analyse, fusion avec les choix de
l'utilisateur, groupe « Props » automatique, items par partie, report de l'extraction après l'analyse et suite
automatique dans le worker, appel OpenAI simulé. Ne valident PAS la qualité des extractions (voir TEST_REPORT.md).

Exécution : <venv-app>\\python.exe app/backend/tests/test_props_logic.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT.parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from atelier3d import props as P  # noqa: E402
from atelier3d.db import Database, new_id, now  # noqa: E402
from atelier3d.graph.model import GraphStore, validate  # noqa: E402
from atelier3d.graph.planner import Planner  # noqa: E402
from atelier3d.graph.state import StateStore  # noqa: E402
from atelier3d.jobs import JobService  # noqa: E402

CAPS = {k: {"status": "disponible"} for k in ("image.create", "image.edit", "image.openai", "vision.openai", "vision.local",
                                               "image.remove_background", "local")}
ALIASES = {"image_edit_default": "krea2_identity_edit", "vision_generate_default": "qwen3vl_vision_generate"}
PARTS = [{"name": "head", "kind": "body", "symmetric": False, "description": "bearded face"},
         {"name": "mask", "kind": "prop", "symmetric": False, "description": "burlap mask with horns"},
         {"name": "left boot", "kind": "prop", "symmetric": False, "description": "green leather boot"},
         {"name": "right boot", "kind": "prop", "symmetric": False, "description": "green leather boot"},
         {"name": "cape", "kind": "prop", "symmetric": False, "description": "torn purple cape"}]


def env():
    db = Database(pathlib.Path(tempfile.mkdtemp()) / "t.sqlite3")
    g, s, j = GraphStore(db), StateStore(db), JobService(db)
    return db, g, s, j, Planner(db, g, s, capabilities_fn=lambda: CAPS, aliases_fn=lambda: ALIASES)


def graph(g, engine="local"):
    return g.create({"schema_version": "1.1", "name": "props", "nodes": [
        {"id": "src", "type": "image.import", "params": {"upload_id": "up_1"}},
        {"id": "props", "type": "image.props", "label": "Épouvantail", "params": {"engine": engine}}],
        "edges": [{"id": "e1", "from": ["src", "image"], "to": ["props", "image"]}]}, author="test")


def set_parts(g, gid, parts, character="A scarecrow."):
    return g.patch(gid, [{"op": "update_node", "node_id": "props", "params": {"parts": parts, "character": character}}], author="test")[0]


def test_normalize_merge_and_prompts():
    norm = P.normalize({"character": "A scarecrow.", "parts": PARTS + [{"name": "Left arm", "kind": "body"}]}, {"include": "props_body"})
    assert [(x["key"], x["kind"], x["symmetric"]) for x in norm["parts"]] == [
        ("head", "body", False), ("mask", "prop", False), ("right_boot", "prop", True), ("cape", "prop", False)]
    assert [x["key"] for x in P.normalize({"parts": PARTS}, {"include": "props", "merge_symmetric": False})["parts"]] == \
        ["mask", "left_boot", "right_boot", "cape"]
    old = [{**norm["parts"][1], "enabled": False, "name": "horned mask"}, {"key": "lantern", "name": "lantern", "kind": "prop", "manual": True}]
    merged = P.merge_parts(old, norm["parts"])
    assert next(x for x in merged if x["key"] == "mask") == {**norm["parts"][1], "enabled": False, "name": "horned mask"}
    assert merged[-1]["key"] == "lantern", "une partie ajoutée à la main est conservée"
    t = P.extraction_prompt(norm["parts"][2], "A scarecrow.")
    assert "right boot" in t and "mirror" in t and "no person" in t and "scarecrow" not in t
    assert "base suit" in P.extraction_prompt({"key": "body", "name": "body", "kind": "body"}, "A scarecrow.")
    assert "mannequin" in P.extraction_prompt({"key": "tunic", "name": "Tunic", "kind": "prop"})
    assert "Close-up portrait" in P.extraction_prompt({"key": "head", "name": "head", "kind": "body"})
    loc = P.parse_local("CHARACTER: A scarecrow.\n1. **Mask** | prop | no | burlap mask\n2. Boots | foot | yes | green boots")
    assert loc["character"] == "A scarecrow." and [x["name"] for x in loc["parts"]] == ["Mask", "Boots"] and loc["parts"][1]["symmetric"]


def test_props_group_items_follow_parts():
    db, g, s, j, pl = env()
    doc = graph(g)
    grp = next(n for n in doc["nodes"] if n["id"] == "props_props")
    assert grp["type"] == "image.edit" and grp["execution"]["mode"] == "foreach_prompts" and grp["params"]["engine"] == "krea2"
    assert any(x.get("owner_node_id") == "props" for x in doc["groups"])
    v = validate(doc)
    assert not v["errors"], v["errors"]
    assert any(w["code"] == "props_pending" for w in v["warnings"]), "en attente de l'analyse : avertissement, pas d'erreur"
    parts = P.normalize({"parts": PARTS}, {})["parts"]
    doc = set_parts(g, doc["id"], parts)
    grp = next(n for n in doc["nodes"] if n["id"] == "props_props")
    assert [i["item_key"] for i in grp["items"]] == ["head", "mask", "right_boot", "cape"]
    pvs = [n for n in doc["nodes"] if (n.get("auto") or {}).get("owner_node_id") == "props_props"]
    assert len(pvs) == 4 and set(next(x for x in doc["groups"] if x.get("owner_node_id") == "props")["node_ids"]) == {"props_props", *[n["id"] for n in pvs]}
    # décocher « cape » : item archivé ; renommer « mask » : seule sa consigne change
    it = {i["item_key"]: i["item_id"] for i in grp["items"]}
    before = {k: s.slot_inputs(doc, grp, 0, iid)["prompt"]["sha"] for k, iid in it.items()}
    parts2 = [{**x, "enabled": x["key"] != "cape", "name": "horned mask" if x["key"] == "mask" else x["name"]} for x in parts]
    doc = set_parts(g, doc["id"], parts2)
    grp = next(n for n in doc["nodes"] if n["id"] == "props_props")
    assert {i["item_key"]: i["status"] for i in grp["items"]}["cape"] == "archived"
    after = {k: s.slot_inputs(doc, grp, 0, it[k])["prompt"]["sha"] for k in ("head", "mask", "right_boot")}
    assert after["head"] == before["head"] and after["right_boot"] == before["right_boot"] and after["mask"] != before["mask"]
    assert "horned mask" in s.slot_inputs(doc, grp, 0, it["mask"])["prompt"]["text"]
    # l'image source arrive par la sortie « Image source » de la node d'extraction
    assert s.slot_inputs(doc, grp, 0, it["head"])["image"]["kind"] == "upload"


def test_group_engine_follows_analysis_engine_until_customized():
    db, g, s, j, pl = env()
    doc = graph(g, engine="openai")
    eng = lambda d: next(n for n in d["nodes"] if n["id"] == "props_props")["params"]["engine"]  # noqa: E731
    assert eng(doc) == "openai"
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "props", "params": {"engine": "local"}}], author="test")
    assert eng(doc) == "krea2", "analyse locale : extraction locale (sinon échec sans clé OpenAI)"
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "props_props", "params": {"engine": "openai"}}], author="test")
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "props", "params": {"detail": "detailed"}}], author="test")
    assert eng(doc) == "openai", "choix explicite du groupe conservé"


def test_extraction_waits_for_analysis_then_continues():
    db, g, s, j, pl = env()
    doc = graph(g)
    plan = pl.plan(doc["id"], scope="until", node_id="props_props")
    assert plan["valid"], plan["errors"]
    assert [(e["node_id"], e["step"]) for e in plan["executions"]] == [("props", "analyze")]
    assert plan["continue_after_analysis"], "1re analyse (liste vide) : l'extraction demandée doit suivre automatiquement"
    # avec des parties déjà connues mais une analyse à refaire : l'extraction est reportée
    doc = set_parts(g, doc["id"], P.normalize({"parts": PARTS}, {})["parts"])
    run = pl.create_run(doc["id"], scope="until", node_id="props_props", author="test", jobs_service=j)
    assert [x["node_id"] for x in run["jobs"]] == ["props"] and len(run["deferred"]) == 4
    assert j.get(run["jobs"][0]["job_id"])["payload"]["continue_run"] == {"scope": "until", "node_id": "props_props"}
    # « Analyser » seul (portée jusqu'à la node d'extraction) : rien de reporté, pas de suite automatique
    plan = pl.plan(doc["id"], scope="until", node_id="props")
    assert not plan["deferred"] and not plan["continue_after_analysis"]


def test_worker_writes_parts_and_launches_extraction():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_worker_logic import make_worker  # noqa: E402

    from atelier3d import capabilities, executors

    w = make_worker()
    g = GraphStore(w.db)
    doc = graph(g)
    with w.db.tx() as c:  # fichier importé simulé
        c.execute("INSERT INTO uploads(id, filename, path, sha256, size, mime, kind, meta, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  ("up_1", "x.png", "uploads/x.png", "0" * 64, 1, "image/png", "image", "{}", now()))
    pl = Planner(w.db, g, w.state, capabilities_fn=lambda: CAPS, aliases_fn=lambda: ALIASES)
    run = pl.create_run(doc["id"], scope="until", node_id="props_props", author="test", jobs_service=w.jobs)
    assert [x["node_id"] for x in run["jobs"]] == ["props"]
    j0 = w.jobs.get(run["jobs"][0]["job_id"])
    assert j0["payload"]["continue_run"] == {"scope": "until", "node_id": "props_props"}, "liste vide : suite automatique prévue"

    def fake(ctx):
        norm = P.normalize({"character": "A scarecrow.", "parts": PARTS}, ctx.params)
        out = ctx.out_path("analysis.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(norm), encoding="utf-8")
        return {"outputs": {"analysis": [ctx.add(out, "analysis", norm)]}, "engine": {}}

    executors.EXECUTORS[("vision.local", "*")] = fake
    orig = capabilities.flat
    capabilities.flat = lambda cfg, db: CAPS
    w.cfg.raw["workflows"] = {"aliases": ALIASES}
    try:
        w.execute(w.claim("gpu"))
    finally:
        capabilities.flat = orig
    doc = g.get(doc["id"])
    props = next(n for n in doc["nodes"] if n["id"] == "props")
    assert [x["key"] for x in props["params"]["parts"]] == ["head", "mask", "right_boot", "cape"]
    assert props["params"]["character"] == "A scarecrow."
    runs = w.db.all("SELECT id FROM runs WHERE graph_id = ? ORDER BY created_at", (doc["id"],))
    assert len(runs) == 2, "l'extraction reportée est lancée automatiquement après l'analyse"
    jobs2 = w.db.all("SELECT node_id, item_key FROM jobs WHERE run_id = ?", (runs[1]["id"],))
    assert sorted(r["item_key"] for r in jobs2) == ["cape", "head", "mask", "right_boot"] and {r["node_id"] for r in jobs2} == {"props_props"}


def test_openai_responses_request_and_parsing():
    from atelier3d.adapters import openai_images as O

    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "completed", "usage": {"input_tokens": 1200}, "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"character": "x", "parts": []})}]}]})

    img = pathlib.Path(tempfile.mkdtemp()) / "a.png"
    img.write_bytes(b"\x89PNG fake")
    data, usage = O.respond_json("sk-test-0123456789abcdefghij", model="gpt-6.1-sol", prompt="list", schema=P.OPENAI_SCHEMA,
                                 name="props_breakdown", images=[img], transport=httpx.MockTransport(handler))
    assert data == {"character": "x", "parts": []} and usage["input_tokens"] == 1200
    b = seen["body"]
    assert b["text"]["format"]["type"] == "json_schema" and b["text"]["format"]["strict"] is True
    c = b["input"][0]["content"]
    assert c[0]["type"] == "input_text" and c[1]["type"] == "input_image" and c[1]["image_url"].startswith("data:image/png;base64,")

    def refusal(req):
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]})

    try:
        O.respond_json("sk-test-0123456789abcdefghij", model="m", prompt="p", schema={}, name="n", transport=httpx.MockTransport(refusal))
        raise AssertionError("refus attendu")
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "code", "") == "openai_refusal", e
    assert O.pick_text_default(["gpt-5.4-mini", "gpt-6-luna", "gpt-6.1-sol"]) == "gpt-6.1-sol"
    assert O.text_models(["gpt-6-luna", "gpt-image-2.5-flare", "gpt-realtime-2", "tts-1"]) == ["gpt-6-luna"]


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
