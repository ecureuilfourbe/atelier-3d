"""OpenAI Images (« ChatGPT Images ») : génération et édition par l'API officielle.

Service EXTERNE et facturé sur le compte de l'utilisateur : utilisé seulement par les nodes réglées explicitement sur
le moteur OpenAI. Endpoints (documentation officielle) : POST /v1/images/generations (JSON) et POST /v1/images/edits
(multipart, champ `image` / `image[]`) ; réponse `data[].b64_json`. Les modèles ne sont pas figés ici : ils sont lus
avec la clé de l'utilisateur (GET /v1/models). La clé n'est jamais journalisée.
"""
from __future__ import annotations

import base64
import pathlib
import ssl
import time
from typing import Any

import httpx

from .base import StepFailure

API = "https://api.openai.com/v1"
FIXED_SIZES = [(1024, 1024), (1536, 1024), (1024, 1536)]
QUALITIES = ["auto", "low", "medium", "high", "xhigh", "max"]
BACKGROUNDS = ["auto", "opaque", "transparent"]
MODERATION = ["auto", "low"]


def _verify():
    try:  # magasin de certificats Windows (proxy d'entreprise, antivirus) ; validation TLS toujours active
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return True


def _client(key: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=httpx.Timeout(300, connect=20),
                        verify=_verify(), trust_env=True, transport=transport)


def _error(r: httpx.Response) -> StepFailure:
    try:
        e = r.json().get("error") or {}
    except ValueError:
        e = {}
    msg, code = str(e.get("message") or r.text[:300]), e.get("code") or e.get("type")
    if r.status_code == 401:
        return StepFailure("Clé OpenAI refusée (401) : vérifiez-la dans Réglages → OpenAI.", code="openai_auth",
                           corrective="Réglages → OpenAI : saisissez une clé valide puis « Tester la clé »")
    if r.status_code == 403:
        return StepFailure(f"Accès refusé par OpenAI (403) : {msg} — les modèles GPT Image peuvent exiger la vérification de "
                           "l'organisation sur platform.openai.com.", code="openai_forbidden")
    if code == "moderation_blocked":
        det = (r.json().get("error") or {}).get("moderation_details") if r.headers.get("content-type", "").startswith("application/json") else None
        return StepFailure(f"Refusé par la modération d'OpenAI : {msg}" + (f" ({det})" if det else ""), code="openai_moderation",
                           corrective="Reformulez la consigne ; le réglage « modération : low » reste soumis aux règles d'OpenAI")
    if r.status_code == 429:
        quota = code == "insufficient_quota"
        return StepFailure(("Quota OpenAI épuisé" if quota else "Trop de requêtes vers OpenAI") + f" (429) : {msg}",
                           code="openai_quota" if quota else "openai_rate_limit",
                           corrective="Vérifiez la facturation de votre compte OpenAI" if quota else "Réessayez dans quelques instants")
    return StepFailure(f"OpenAI a refusé la requête (HTTP {r.status_code}) : {msg}", code="openai_http", details={"status": r.status_code, "code": code})


def _post(key: str, path: str, transport=None, **kw) -> dict:
    """Une nouvelle tentative seulement pour une limite de débit ou une erreur serveur/réseau transitoire."""
    last: Exception | None = None
    for attempt in range(2):
        try:
            with _client(key, transport) as c:
                r = c.post(path, **kw)
        except httpx.HTTPError as e:
            last = StepFailure(f"OpenAI injoignable : {type(e).__name__} {e}", code="openai_network",
                               corrective="Vérifiez la connexion Internet ou le proxy (HTTPS_PROXY)")
            time.sleep(3)
            continue
        if r.status_code == 200:
            return r.json()
        err = _error(r)
        retry = (r.status_code == 429 and err.code == "openai_rate_limit") or r.status_code >= 500
        if not retry or attempt:
            raise err
        last = err
        time.sleep(min(30.0, float(r.headers.get("retry-after", "5") or 5)))
    raise last  # type: ignore[misc]


def list_models(key: str, transport=None) -> list[str]:
    """Tous les modèles accessibles avec cette clé (GET /v1/models)."""
    with _client(key, transport) as c:
        r = c.get("/models", timeout=30)
    if r.status_code != 200:
        raise _error(r)
    return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))


def image_models(ids: list[str]) -> list[str]:
    return [m for m in ids if m.startswith("gpt-image")]


def list_image_models(key: str, transport=None) -> list[str]:
    return image_models(list_models(key, transport))


# Modèles texte + image en entrée (API Responses) : familles « gpt-<n> » hors modèles spécialisés
_TEXT_EXCLUDE = ("image", "audio", "realtime", "tts", "transcribe", "search", "embedding", "moderation", "codex", "dall-e",
                 "whisper", "computer-use", "instruct", "chat-latest", "oss")
# Documentation officielle (octobre 2026) : Astra (le plus capable), Sol (équilibre coût / performance), Luna (le plus économique)
TEXT_PREFERENCE = ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra", "gpt-5.6-terra", "gpt-5.5", "gpt-5.4-mini", "gpt-5-mini",
                   "gpt-4.1-mini", "gpt-4o-mini"]


def text_models(ids: list[str]) -> list[str]:
    import re

    return [m for m in ids if re.match(r"^gpt-\d", m) and not any(x in m for x in _TEXT_EXCLUDE)]


def pick_text_default(models: list[str]) -> str:
    for m in TEXT_PREFERENCE:
        if m in models:
            return m
    undated = [m for m in models if not m[-10:].replace("-", "").isdigit()]
    return sorted(undated or models)[-1] if models else ""


def respond_json(key: str, *, model: str, prompt: str, schema: dict, name: str, images: list[pathlib.Path] | None = None,
                 detail: str = "high", transport=None) -> tuple[Any, dict]:
    """API Responses avec sortie structurée stricte (text.format json_schema) et images en data URL. Retourne (données, usage)."""
    import json as _json

    content: list[dict] = [{"type": "input_text", "text": prompt}]
    for pth in images or []:
        mime = "image/png" if pth.suffix.lower() == ".png" else "image/webp" if pth.suffix.lower() == ".webp" else "image/jpeg"
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(pth.read_bytes()).decode()}",
                        "detail": detail})
    body = {"model": model, "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}}}
    resp = _post(key, "/responses", transport, json=body)
    texts, refusal = [], None
    for item in resp.get("output") or []:
        for c in item.get("content") or []:
            if c.get("type") == "output_text":
                texts.append(c.get("text") or "")
            elif c.get("type") == "refusal":
                refusal = c.get("refusal") or "refus"
    if refusal and not texts:
        raise StepFailure(f"OpenAI a refusé de répondre : {refusal}", code="openai_refusal",
                          corrective="Vérifiez l'image et les consignes (contenu sensible ?) ou utilisez le moteur local")
    if resp.get("status") == "incomplete":
        raise StepFailure(f"Réponse OpenAI incomplète : {(resp.get('incomplete_details') or {}).get('reason')}", code="openai_incomplete")
    try:
        return _json.loads("".join(texts)), resp.get("usage") or {}
    except ValueError:
        raise StepFailure("Réponse OpenAI illisible (JSON attendu)", code="openai_output", details={"text": "".join(texts)[:2000]})


def pick_default(models: list[str], purpose: str) -> str:
    """Recommandation de la documentation officielle (2026) : « sunburst » pour la précision d'édition, « flare » pour la
    génération rapide ; sinon le plus récent des modèles GPT Image réellement disponibles pour cette clé."""
    pref = ["sunburst", "flare"] if purpose == "edit" else ["flare", "sunburst"]
    for tag in pref:
        cands = [m for m in models if tag in m and not m[-10:].replace("-", "").isdigit()]  # alias de préférence au snapshot daté
        if cands:
            return sorted(cands)[-1]
    undated = [m for m in models if not m[-10:].replace("-", "").isdigit()]
    return sorted(undated or models)[-1] if models else ""


def size_for(model: str, width: int, height: int) -> str:
    """gpt-image-1* : tailles fixes (la plus proche en proportions) ; modèles plus récents : taille libre si elle respecte
    les contraintes documentées (multiples de 16, ratio 1:3 à 3:1, côté ≤ 3840, 655 360 à 8 294 400 pixels)."""
    w, h = int(width), int(height)
    if not model.startswith("gpt-image-1"):
        w16, h16 = max(16, round(w / 16) * 16), max(16, round(h / 16) * 16)
        if max(w16, h16) <= 3840 and 1 / 3 <= w16 / h16 <= 3 and 655_360 <= w16 * h16 <= 8_294_400:
            return f"{w16}x{h16}"
    ratio = w / h
    fw, fh = min(FIXED_SIZES, key=lambda s: abs((s[0] / s[1]) - ratio))
    return f"{fw}x{fh}"


def _save(resp: dict, out: pathlib.Path) -> dict:
    data = resp.get("data") or []
    if not data or not data[0].get("b64_json"):
        raise StepFailure("OpenAI n'a renvoyé aucune image", code="openai_empty")
    out.write_bytes(base64.b64decode(data[0]["b64_json"]))
    return {"usage": resp.get("usage"), "revised_prompt": data[0].get("revised_prompt"), "created": resp.get("created")}


def generate(key: str, *, model: str, prompt: str, size: str, quality: str, background: str, moderation: str,
             out: pathlib.Path, transport=None) -> dict:
    body: dict[str, Any] = {"model": model, "prompt": prompt, "size": size, "quality": quality, "background": background,
                            "moderation": moderation, "n": 1, "output_format": "png"}
    return _save(_post(key, "/images/generations", transport, json=body), out)


def edit(key: str, *, model: str, prompt: str, images: list[pathlib.Path], size: str, quality: str, background: str,
         moderation: str, out: pathlib.Path, transport=None) -> dict:
    field = "image" if len(images) == 1 else "image[]"
    files = [(field, (p.name, p.read_bytes(), "image/png" if p.suffix.lower() == ".png" else
                      "image/webp" if p.suffix.lower() == ".webp" else "image/jpeg")) for p in images]
    data = {"model": model, "prompt": prompt, "size": size, "quality": quality, "background": background,
            "moderation": moderation, "n": "1", "output_format": "png"}
    return _save(_post(key, "/images/edits", transport, data=data, files=files), out)


