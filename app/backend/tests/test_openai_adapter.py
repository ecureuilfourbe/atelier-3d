"""Tests de l'adaptateur OpenAI Images contre un serveur SIMULÉ (httpx.MockTransport) : aucune requête réelle, aucune clé.
Ils vérifient le format des requêtes (endpoints et champs de la documentation officielle) et la traduction des erreurs.

Exécution : <venv-app>\\python.exe app/backend/tests/test_openai_adapter.py
"""
from __future__ import annotations

import base64
import io
import json
import pathlib
import sys
import tempfile

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atelier3d.adapters import openai_images as O  # noqa: E402
from atelier3d.adapters.base import StepFailure  # noqa: E402


def png_bytes(color=(200, 30, 30, 255)) -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGBA", (8, 8), color).save(b, "PNG")
    return b.getvalue()


def ok_image():
    return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(png_bytes()).decode()}],
                                     "usage": {"input_tokens": 12, "output_tokens": 4160}})


def tmp() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())


def test_generate_request_and_output():
    seen = {}

    def handler(req: httpx.Request):
        seen["path"], seen["auth"], seen["body"] = req.url.path, req.headers["authorization"], json.loads(req.content)
        return ok_image()

    out = tmp() / "image.png"
    meta = O.generate("sk-test", model="gpt-image-2", prompt="a red ballon", size="1024x1024", quality="auto", background="auto",
                      moderation="auto", out=out, transport=httpx.MockTransport(handler))
    assert seen["path"] == "/v1/images/generations" and seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {"model": "gpt-image-2", "prompt": "a red ballon", "size": "1024x1024", "quality": "auto",
                            "background": "auto", "moderation": "auto", "n": 1, "output_format": "png"}
    assert out.read_bytes().startswith(b"\x89PNG") and meta["usage"]["output_tokens"] == 4160


def test_edit_multipart_with_two_images():
    seen = {}

    def handler(req: httpx.Request):
        seen["path"], seen["ctype"], seen["body"] = req.url.path, req.headers["content-type"], req.content
        return ok_image()

    d = tmp()
    a, b = d / "a.png", d / "b.png"
    a.write_bytes(png_bytes()), b.write_bytes(png_bytes((0, 0, 255, 255)))
    O.edit("sk-test", model="gpt-image-2.5-sunburst", prompt="put a hat", images=[a, b], size="1024x1536", quality="high",
           background="transparent", moderation="low", out=d / "out.png", transport=httpx.MockTransport(handler))
    body = seen["body"].decode("latin-1")
    assert seen["path"] == "/v1/images/edits" and seen["ctype"].startswith("multipart/form-data")
    assert body.count('name="image[]"') == 2 and 'name="prompt"' in body and "gpt-image-2.5-sunburst" in body and "transparent" in body


def test_errors_are_explained():
    cases = {
        401: ({"error": {"message": "Incorrect API key", "code": "invalid_api_key"}}, "openai_auth"),
        403: ({"error": {"message": "Your organization must be verified"}}, "openai_forbidden"),
        400: ({"error": {"message": "Your request was rejected by the safety system", "code": "moderation_blocked"}}, "openai_moderation"),
        429: ({"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}, "openai_quota"),
    }
    for status, (body, code) in cases.items():
        try:
            O.generate("sk", model="m", prompt="p", size="1024x1024", quality="auto", background="auto", moderation="auto",
                       out=tmp() / "x.png", transport=httpx.MockTransport(lambda r, s=status, b=body: httpx.Response(s, json=b)))
            raise AssertionError(f"{status} aurait dû échouer")
        except StepFailure as e:
            assert e.code == code, (status, e.code, str(e))
            assert "sk" not in str(e) or "sk-" not in str(e)  # jamais la clé dans un message


def test_rate_limit_retried_once():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limit", "code": "rate_limit_exceeded"}}, headers={"retry-after": "0"})
        return ok_image()

    O.generate("sk", model="m", prompt="p", size="1024x1024", quality="auto", background="auto", moderation="auto",
               out=tmp() / "x.png", transport=httpx.MockTransport(handler))
    assert len(calls) == 2


def test_models_and_sizes():
    def handler(req):
        return httpx.Response(200, json={"data": [{"id": i} for i in ("gpt-4o", "gpt-image-1", "gpt-image-2", "gpt-image-2-2026-04-21",
                                                                      "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
                                                                      "gpt-image-2.5-sunburst-2026-09-08", "dall-e-3")]})

    models = O.list_image_models("sk", transport=httpx.MockTransport(handler))
    assert "gpt-4o" not in models and "dall-e-3" not in models and "gpt-image-2.5-flare" in models
    assert O.pick_default(models, "edit") == "gpt-image-2.5-sunburst"
    assert O.pick_default(models, "generate") == "gpt-image-2.5-flare"
    assert O.pick_default(["gpt-image-1"], "edit") == "gpt-image-1"
    assert O.size_for("gpt-image-1", 1024, 1024) == "1024x1024" and O.size_for("gpt-image-1", 480, 864) == "1024x1536"
    assert O.size_for("gpt-image-2", 1024, 1024) == "1024x1024" and O.size_for("gpt-image-2", 1920, 1088) == "1920x1088"
    assert O.size_for("gpt-image-2", 256, 256) == "1024x1024"  # trop petit pour la taille libre : taille fixe la plus proche


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
