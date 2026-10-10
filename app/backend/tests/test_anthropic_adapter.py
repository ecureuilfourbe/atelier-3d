"""Claude (Anthropic) SIMULÉ (httpx.MockTransport, aucun appel réseau) : requêtes, réponses, erreurs, et bout en bout dans
le worker (file « net », clé jamais exposée), empreintes OpenAI / local inchangées.

  <venv-app>\\python.exe app\\backend\\tests\\test_anthropic_adapter.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from atelier3d.adapters import anthropic_api as A  # noqa: E402
from atelier3d.adapters.base import StepFailure  # noqa: E402

KEY = "sk-ant-cle-simulee-0123456789abcdef"


def test_models_pagination_and_default():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == A.VERSION
        if "after_id" not in req.url.params:
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"}], "has_more": True,
                                             "last_id": "claude-sonnet-5-5"})
        return httpx.Response(200, json={"data": [{"id": "claude-haiku-5-5"}, {"id": "claude-sonnet-4-5"}], "has_more": False})

    ids = A.list_models(KEY, transport=httpx.MockTransport(handler))
    assert ids == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-sonnet-4-5"], ids
    assert len(calls) == 2 and calls[1].url.params["after_id"] == "claude-sonnet-5-5"
    assert A.pick_default(ids) == "claude-sonnet-5-5"  # le Sonnet le plus récent (ordre de l'API)
    assert A.pick_default(["claude-haiku-5-5"]) == "claude-haiku-5-5" and A.pick_default([]) == ""


def test_structured_answer_with_image():
    img = pathlib.Path(tempfile.mkdtemp()) / "a.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5},
                                         "content": [{"type": "tool_use", "name": "props_breakdown", "id": "t1",
                                                      "input": {"character": "knight", "parts": []}}]})

    schema = {"type": "object", "properties": {"character": {"type": "string"}}, "required": ["character"]}
    data, usage = A.respond_json(KEY, model="claude-sonnet-5-5", prompt="analyse", schema=schema, name="props_breakdown",
                                 images=[img], transport=httpx.MockTransport(handler))
    assert data == {"character": "knight", "parts": []} and usage["output_tokens"] == 5
    b = seen["body"]
    assert b["model"] == "claude-sonnet-5-5" and b["tool_choice"] == {"type": "tool", "name": "props_breakdown"}
    assert b["tools"][0]["input_schema"] == schema
    content = b["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg" and content[-1]["text"] == "analyse"


def test_errors_and_single_retry():
    def auth(req):
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})

    try:
        A.list_models(KEY, transport=httpx.MockTransport(auth))
        raise AssertionError("401 attendu")
    except StepFailure as e:
        assert e.code == "anthropic_auth" and "Réglages → Claude" in str(e)

    n = {"c": 0}

    def busy_then_ok(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(529, headers={"retry-after": "0"}, json={"error": {"type": "overloaded_error", "message": "Overloaded"}})
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5-5"}], "has_more": False})

    assert A.list_models(KEY, transport=httpx.MockTransport(busy_then_ok)) == ["claude-sonnet-5-5"] and n["c"] == 2

    def refusal(req):
        return httpx.Response(200, json={"stop_reason": "refusal", "content": []})

    try:
        A.respond_json(KEY, model="claude-sonnet-5-5", prompt="x", schema={"type": "object"}, name="n", transport=httpx.MockTransport(refusal))
        raise AssertionError("refus attendu")
    except StepFailure as e:
        assert e.code == "anthropic_refusal"


def test_claude_engine_steps_keep_other_fingerprints():
    from atelier3d.graph import nodetypes as NT

    for t, cap in (("text.randomize", "text.claude"), ("image.props", "vision.claude")):
        node = {"id": "n", "type": t, "params": {"engine": "claude"}}
        st = NT.get(t).steps(node)[0]
        assert st.capability == cap and st.resource == "net" and "claude_model" in st.params, st
    # moteurs existants : paramètres d'étape inchangés (aucun résultat existant ne devient « à recalculer »)
    assert "claude_model" not in NT.get("image.props").steps({"type": "image.props", "params": {}})[0].params
    assert "claude_model" not in NT.get("text.randomize").steps({"type": "text.randomize", "params": {"engine": "openai"}})[0].params


def test_worker_claude_prompts_on_net_slot_without_leaking_key():
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_worker_logic import make_worker

    from atelier3d.db import new_id
    from atelier3d.secrets_store import ANTHROPIC_KEY, SecretStore, load_services, save_services

    w = make_worker()
    w.cfg.config_dir.mkdir(parents=True, exist_ok=True)

    def run():
        jid = new_id("job")
        payload = {"node": {"id": "r", "type": "text.randomize", "label": "r"}, "capability": "text.claude", "step": "generate",
                   "params": {"engine": "claude", "context": "village sur une planète désertique", "count": 3, "language": "en"},
                   "step_params": {}, "seed": 1, "inputs": {}, "engine": {}, "activate": "activate_first_success_only"}
        with w.db.tx() as c:
            w.jobs.insert_job(c, {"id": jid, "kind": "graph_step", "run_id": None, "graph_id": None, "node_id": "r", "node_type": "text.randomize",
                                  "step": "generate", "resource": "net", "depends_on": [], "payload": payload}, author="test")
        assert w.claim("gpu") is None, "un appel Claude ne doit pas occuper la file GPU"
        w.execute(w.claim("net"))
        return w.jobs.get(jid)

    j = run()
    assert j["status"] == "failed" and j["error"]["code"] == "anthropic_key_missing", j["error"]

    SecretStore(w.cfg.config_dir).set(ANTHROPIC_KEY, KEY)
    svc = load_services(w.cfg.config_dir)
    svc["anthropic"].update({"models": ["claude-sonnet-5-5"], "model": "claude-sonnet-5-5"})
    save_services(w.cfg.config_dir, svc)
    seen = {}

    def fake(key, **kw):
        seen["key"], seen["kw"] = key, kw
        return {"prompts": ["desert village at dawn", "market under twin suns", "sandstone towers at dusk"]}, {"output_tokens": 30}

    orig = A.respond_json
    A.respond_json = fake
    try:
        j = run()
    finally:
        A.respond_json = orig
    assert j["status"] == "succeeded", j.get("error")
    assert seen["key"] == KEY and seen["kw"]["model"] == "claude-sonnet-5-5" and "3 prompts" in seen["kw"]["prompt"]
    v = w.db.one("SELECT outputs, engine FROM variants WHERE job_id = ?", (j["id"],))
    assert json.loads(v["engine"]).get("engine") == "claude", v["engine"]
    dump = json.dumps(dict(w.db.one("SELECT payload, result FROM jobs WHERE id = ?", (j["id"],)))) + v["engine"]
    assert "sk-ant-cle-simulee" not in dump, "la clé ne doit apparaître ni dans la tâche, ni dans la variante"


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
