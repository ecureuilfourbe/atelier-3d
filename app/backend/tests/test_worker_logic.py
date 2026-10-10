"""Tests de LOGIQUE du worker (exécuteurs simulés) : OOM → libération + UNE nouvelle tentative en profil réduit
journalisée ; OOM persistant → échec explicite ; dépendants bloqués ; validations vidéo.
Ces tests ne remplacent pas les exécutions réelles (voir TEST_REPORT.md).

Exécution : <venv-app>\\python.exe app/backend/tests/test_worker_logic.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT.parents[1]
sys.path.insert(0, str(ROOT))

from atelier3d import config as C  # noqa: E402
from atelier3d.adapters.base import StepFailure  # noqa: E402
from atelier3d.db import Database, dumps, new_id, now  # noqa: E402
from atelier3d.graph.model import GraphStore, validate  # noqa: E402
from atelier3d.graph.planner import Planner  # noqa: E402
from atelier3d.graph.state import StateStore  # noqa: E402
from atelier3d.jobs import JobService  # noqa: E402
from atelier3d import executors, worker as W  # noqa: E402
from atelier3d.db import reset_db_singleton  # noqa: E402


def make_worker():
    home = pathlib.Path(tempfile.mkdtemp())
    (home / "data").mkdir()
    cfg = C.Config(home=home, raw={"paths": {"dist": str(DIST), "runtime": str(home / "rt")}, "workflows": {"aliases": {}}})
    reset_db_singleton()
    w = W.Worker(cfg)
    return w


def insert_job(w, capability="image.create", deps=None, run_id=None):
    jid = new_id("job")
    payload = {"node": {"id": "n", "type": "image.generate", "label": "n"}, "capability": capability, "step": "create",
               "params": {"width": 1024, "height": 1024}, "step_params": {}, "seed": 1, "inputs": {}, "engine": {},
               "activate": "activate_first_success_only"}
    with w.db.tx() as c:
        w.jobs.insert_job(c, {"id": jid, "kind": "graph_step", "run_id": run_id, "graph_id": None, "node_id": "n", "node_type": "image.generate",
                              "step": "create", "resource": "gpu", "depends_on": deps or [], "payload": payload}, author="test")
    return jid


def test_oom_retry_reduced_profile_then_success():
    w = make_worker()
    calls = []

    def fake(ctx):
        calls.append(dict(ctx.params))
        if len(calls) == 1:
            raise StepFailure("CUDA out of memory", oom=True)
        return {"outputs": {}, "engine": {}}

    executors.EXECUTORS[("image.create", "*")] = fake
    jid = insert_job(w)
    w.execute(w.claim("gpu"))
    j = w.jobs.get(jid)
    assert j["status"] == "succeeded", j
    assert len(calls) == 2 and calls[1]["width"] == 768  # profil réduit
    v = w.db.one("SELECT engine FROM variants WHERE job_id = ?", (jid,))
    assert "quality_reduction" in v["engine"]
    evs = [r["type"] for r in w.db.all("SELECT type FROM events WHERE job_id = ?", (jid,))]
    assert "oom_retry" in evs


def test_oom_twice_fails_explicitly_and_blocks_dependents():
    w = make_worker()

    def always(ctx):
        raise StepFailure("CUDA out of memory", oom=True, corrective="Libérez la VRAM")

    executors.EXECUTORS[("image.create", "*")] = always
    run = new_id("run")
    with w.db.tx() as c:
        c.execute("INSERT INTO runs(id, scope, status, author, created_at) VALUES(?,?,?,?,?)", (run, "{}", "queued", "test", now()))
    a = insert_job(w, run_id=run)
    b = insert_job(w, deps=[a], run_id=run)
    w.execute(w.claim("gpu"))
    ja = w.jobs.get(a)
    assert ja["status"] == "failed" and ja["error"]["oom"] is True and ja["error"]["corrective_action"]
    assert w.jobs.get(b)["status"] == "blocked"
    assert w.db.one("SELECT status FROM runs WHERE id = ?", (run,))["status"] == "failed"
    assert w.db.one("SELECT COUNT(*) AS n FROM variants WHERE job_id = ?", (a,))["n"] == 0  # aucun faux « terminé »


def test_cache_invalidated_when_code_changes():
    w = make_worker()
    calls = []

    def fake(ctx):
        calls.append(1)
        return {"outputs": {}, "engine": {}}

    executors.EXECUTORS[("image.create", "*")] = fake
    insert_job(w)
    w.execute(w.claim("gpu"))
    a = insert_job(w)
    w.execute(w.claim("gpu"))
    assert w.db.one("SELECT from_cache_of FROM variants WHERE job_id = ?", (a,))["from_cache_of"], "même code : réutilisation du cache attendue"
    n = len(calls)
    w.code_fp = "script-corrige"  # un script Blender / exécuteur modifié change l'empreinte du code
    b = insert_job(w)
    w.execute(w.claim("gpu"))
    assert len(calls) == n + 1, "code modifié : le résultat en cache ne doit pas être réutilisé"
    assert not w.db.one("SELECT from_cache_of FROM variants WHERE job_id = ?", (b,))["from_cache_of"]


def test_openai_step_runs_on_net_slot_without_leaking_key():
    """Bout en bout dans le worker, OpenAI SIMULÉ (aucun appel réseau) : file « net », variante créée, clé jamais exposée."""
    import base64
    import io
    import json as _json

    from PIL import Image

    from atelier3d.adapters import openai_images as O
    from atelier3d.graph.model import text_sha
    from atelier3d.secrets_store import OPENAI_KEY, SecretStore

    w = make_worker()
    w.cfg.config_dir.mkdir(parents=True, exist_ok=True)

    def run(capability_key: str | None):
        jid = new_id("job")
        payload = {"node": {"id": "n", "type": "image.generate", "label": "n"}, "capability": "image.openai", "step": "create",
                   "params": {"engine": "openai", "width": 1024, "height": 1024, "quality": "low"}, "step_params": {}, "seed": 1,
                   "inputs": {"prompt": {"kind": "text", "text": "a red ballon", "sha": text_sha("a red ballon")}}, "engine": {},
                   "activate": "activate_first_success_only"}
        with w.db.tx() as c:
            w.jobs.insert_job(c, {"id": jid, "kind": "graph_step", "run_id": None, "graph_id": None, "node_id": "n", "node_type": "image.generate",
                                  "step": "create", "resource": "net", "depends_on": [], "payload": payload}, author="test")
        assert w.claim("gpu") is None, "un appel OpenAI ne doit pas occuper la file GPU"
        w.execute(w.claim("net"))
        return w.jobs.get(jid)

    j = run(None)
    assert j["status"] == "failed" and j["error"]["code"] == "openai_key_missing", j["error"]

    SecretStore(w.cfg.config_dir).set(OPENAI_KEY, "sk-cle-simulee-0123456789abcdef")
    seen = {}

    def fake_generate(key, **kw):
        seen["key"], seen["kw"] = key, kw
        b = io.BytesIO()
        Image.new("RGB", (16, 16), (200, 0, 0)).save(b, "PNG")
        kw["out"].write_bytes(b.getvalue())
        return {"usage": {"output_tokens": 1}, "revised_prompt": None}

    orig = O.generate
    O.generate = fake_generate
    try:
        j = run("ok")
    finally:
        O.generate = orig
    assert j["status"] == "succeeded", j.get("error")
    assert seen["key"] == "sk-cle-simulee-0123456789abcdef" and seen["kw"]["quality"] == "low" and seen["kw"]["size"] == "1024x1024"
    v = w.db.one("SELECT outputs, engine FROM variants WHERE job_id = ?", (j["id"],))
    assert _json.loads(v["outputs"]).get("image"), v["outputs"]
    dump = _json.dumps(dict(w.db.one("SELECT payload, result FROM jobs WHERE id = ?", (j["id"],)))) + v["engine"]
    assert "sk-cle-simulee" not in dump, "la clé ne doit apparaître ni dans la tâche, ni dans la variante"


def test_openai_node_defaults_come_from_settings():
    """Qualité / modération vides sur la node (valeur par défaut du schéma) → valeurs de Réglages → OpenAI."""
    import io

    from PIL import Image

    from atelier3d.adapters import openai_images as O
    from atelier3d.graph import nodetypes as NT
    from atelier3d.graph.model import text_sha
    from atelier3d.secrets_store import OPENAI_KEY, SecretStore, load_services, save_services

    w = make_worker()
    w.cfg.config_dir.mkdir(parents=True, exist_ok=True)
    SecretStore(w.cfg.config_dir).set(OPENAI_KEY, "sk-cle-simulee-0123456789abcdef")
    svc = load_services(w.cfg.config_dir)
    svc["openai"].update({"quality": "high", "moderation": "low", "model_generate": "gpt-image-test"})
    save_services(w.cfg.config_dir, svc)
    params = NT.effective_params({"id": "n", "type": "image.generate", "params": {"engine": "openai"}})
    assert params["quality"] == "" and params["moderation"] == "", params
    seen = {}

    def fake_generate(key, **kw):
        seen.update(kw)
        b = io.BytesIO()
        Image.new("RGB", (16, 16)).save(b, "PNG")
        kw["out"].write_bytes(b.getvalue())
        return {"usage": {}, "revised_prompt": None}

    jid = new_id("job")
    payload = {"node": {"id": "n", "type": "image.generate", "label": "n"}, "capability": "image.openai", "step": "create",
               "params": params, "step_params": {}, "seed": 1,
               "inputs": {"prompt": {"kind": "text", "text": "a red ballon", "sha": text_sha("a red ballon")}}, "engine": {},
               "activate": "activate_first_success_only"}
    with w.db.tx() as c:
        w.jobs.insert_job(c, {"id": jid, "kind": "graph_step", "run_id": None, "graph_id": None, "node_id": "n", "node_type": "image.generate",
                              "step": "create", "resource": "net", "depends_on": [], "payload": payload}, author="test")
    orig = O.generate
    O.generate = fake_generate
    try:
        w.execute(w.claim("net"))
    finally:
        O.generate = orig
    assert w.jobs.get(jid)["status"] == "succeeded"
    assert (seen["quality"], seen["moderation"], seen["model"]) == ("high", "low", "gpt-image-test"), seen


def test_worker_resolves_generated_prompt_as_text():
    """Une tâche « Prompts aléatoires » du même run → le n-ième prompt en TEXTE (même identité que l'état), et un
    « Composer prompt » différé est composé à l'exécution."""
    import json as _json

    from atelier3d.graph.state import StateStore, compose_text  # noqa: F401
    from atelier3d.graph.model import text_sha

    w = make_worker()
    aid, vid, jid = new_id("art"), new_id("var"), new_id("job")
    with w.db.tx() as c:
        c.execute("INSERT INTO artifacts(id, job_id, variant_id, kind, role, path, sha256, size, mime, meta, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, jid, vid, "json", "prompts", "x.json", "0" * 64, 1, "application/json", _json.dumps({"prompts": ["a dune sea", "a cantina"]}), now()))
        c.execute("INSERT INTO variants(id, graph_id, node_id, step, item_id, job_id, run_id, status, author, params, seed, inputs, input_fingerprint, "
                  "cache_key, outputs, engine, stats, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vid, None, "rnd", "generate", "", jid, None, "succeeded", "t", "{}", 1, "{}", "x", None, _json.dumps({"prompts": [aid]}), "{}", "{}", now()))
        w.jobs.insert_job(c, {"id": jid, "kind": "graph_step", "run_id": None, "graph_id": None, "node_id": "rnd", "node_type": "text.randomize",
                              "step": "generate", "resource": "gpu", "depends_on": [], "payload": {}}, author="t")
        c.execute("UPDATE jobs SET status = 'succeeded', result = ? WHERE id = ?", (_json.dumps({"variant_id": vid}), jid))
    r = w.resolve({"kind": "job", "job_id": jid, "node_id": "rnd", "step": "generate", "output": "prompts", "text_index": 1, "edge_id": "e1"})
    assert r["kind"] == "text" and r["text"] == "a cantina" and r["sha"] == text_sha("a cantina"), r
    assert StateStore.ref_identity(r) == {"text": text_sha("a cantina")}
    comp = w.resolve({"kind": "compose", "node_id": "cmp", "format": {"prefix": "", "separator": ", ", "suffix": ""},
                      "parts": [{"kind": "job", "job_id": jid, "node_id": "rnd", "text_index": 0},
                                {"kind": "text", "text": "matte painting", "sha": text_sha("matte painting")}]})
    assert comp == {**comp, "kind": "text", "text": "a dune sea, matte painting"}
    missing = w.resolve({"kind": "job", "job_id": jid, "node_id": "rnd", "text_index": 5})
    assert missing["kind"] == "missing"


def test_cancelling_queued_step_blocks_dependents_and_closes_run():
    """Constaté le 09/10/2026 : étapes annulées avant démarrage → run « en attente » pour toujours, dépendantes bloquées
    sans le savoir. L'annulation bloque maintenant la suite et clôt le run (et la fiche parent)."""
    w = make_worker()
    run = new_id("run")
    with w.db.tx() as c:
        c.execute("INSERT INTO runs(id, scope, status, author, created_at) VALUES(?,?,?,?,?)", (run, "{}", "queued", "test", now()))
    a = insert_job(w, run_id=run)
    b = insert_job(w, deps=[a], run_id=run)
    parent = w.jobs.create_parent("graph_run", run_id=run, graph_id=None, payload={}, author="test")
    w.jobs.request_cancel(a, author="test")
    assert w.jobs.get(a)["status"] == "cancelled" and w.jobs.get(b)["status"] == "blocked"
    assert w.db.one("SELECT status FROM runs WHERE id = ?", (run,))["status"] == "cancelled"
    assert w.jobs.get(parent["id"])["status"] == "cancelled", "la fiche du run n'est plus « en attente »"
    # réparation au démarrage du worker : un run resté ouvert sans rien à faire est clos
    run2 = new_id("run")
    with w.db.tx() as c:
        c.execute("INSERT INTO runs(id, scope, status, author, created_at) VALUES(?,?,?,?,?)", (run2, "{}", "running", "test", now()))
    j = insert_job(w, run_id=run2)
    with w.db.tx() as c:
        c.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ?", (j,))
    w.reconcile()
    assert w.db.one("SELECT status FROM runs WHERE id = ?", (run2,))["status"] == "cancelled"


def test_video_mode_validation():
    doc = {"id": "g", "schema_version": "1.1", "nodes": [
        {"id": "p", "type": "text.prompt", "params": {"text": "x"}},
        {"id": "img", "type": "image.import", "params": {"upload_id": "u"}},
        {"id": "refs", "type": "reference.collection", "params": {"slots": [{"id": "a", "role": "subject", "label": "A"}]}},
        {"id": "v", "type": "video.generate", "params": {"mode": "reference_to_video"}}],
        "edges": [{"id": "e1", "from": ["p", "text"], "to": ["v", "prompt"]}, {"id": "e2", "from": ["img", "image"], "to": ["v", "image"]},
                  {"id": "e3", "from": ["refs", "references"], "to": ["v", "references"]}, {"id": "e4", "from": ["img", "image"], "to": ["refs", "image:a"]}]}
    v = validate(doc)
    assert any(e["code"] == "video_mix" for e in v["errors"]), v


def test_unimplemented_modes_refused():
    doc = {"id": "g", "schema_version": "1.1", "nodes": [
        {"id": "p", "type": "text.prompt", "params": {"text": "x"}},
        {"id": "gen", "type": "image.generate", "params": {"operation": "create"}, "execution": {"mode": "zip"}}],
        "edges": [{"id": "e1", "from": ["p", "text"], "to": ["gen", "prompts"], "item_key": "a"}]}
    v = validate(doc)
    assert any(e["code"] == "exec_mode" and "non intégré" in e["message"] for e in v["errors"]), v


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
