"""Claude (Anthropic) : écriture de prompts et analyse d'image par l'API Messages officielle.

Service EXTERNE et facturé sur le compte de l'utilisateur : utilisé seulement par les nodes dont le moteur est réglé
explicitement sur Claude (« Prompts aléatoires », analyse d'« Extraction de props »). Claude ne génère pas d'images.
Endpoints (documentation officielle) : GET /v1/models (liste des modèles accessibles avec la clé) et POST /v1/messages ;
en-têtes x-api-key et anthropic-version. Sortie structurée : un outil unique imposé (tool_choice) dont le schéma d'entrée
est le schéma JSON attendu. Les modèles ne sont pas figés ici : ils sont lus avec la clé. La clé n'est jamais journalisée.
"""
from __future__ import annotations

import base64
import pathlib
import time
from typing import Any

import httpx

from .base import StepFailure
from .openai_images import _verify

API = "https://api.anthropic.com"
VERSION = "2023-06-01"


def _client(key: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"x-api-key": key, "anthropic-version": VERSION, "content-type": "application/json"},
                        timeout=httpx.Timeout(300, connect=20), verify=_verify(), trust_env=True, transport=transport)


def _error(r: httpx.Response) -> StepFailure:
    try:
        e = r.json().get("error") or {}
    except ValueError:
        e = {}
    msg, typ = str(e.get("message") or r.text[:300]), e.get("type") or ""
    if r.status_code == 401:
        return StepFailure("Clé Claude refusée (401) : vérifiez-la dans Réglages → Claude.", code="anthropic_auth",
                           corrective="Réglages → Claude : saisissez une clé valide puis « Tester la clé »")
    if r.status_code == 403:
        return StepFailure(f"Accès refusé par Anthropic (403) : {msg}", code="anthropic_forbidden")
    if r.status_code == 429:
        return StepFailure(f"Trop de requêtes vers Claude (429) : {msg}", code="anthropic_rate_limit",
                           corrective="Patientez puis relancez ; limites selon votre compte Anthropic")
    if r.status_code == 529 or typ == "overloaded_error":
        return StepFailure("Claude est momentanément surchargé (529) : relancez dans quelques minutes.", code="anthropic_overloaded")
    if r.status_code == 400 and ("credit" in msg.lower() or "billing" in msg.lower()):
        return StepFailure(f"Crédit Anthropic insuffisant : {msg}", code="anthropic_quota",
                           corrective="Ajoutez du crédit sur console.anthropic.com (Billing)")
    if r.status_code == 404 and typ == "not_found_error":
        return StepFailure(f"Modèle Claude introuvable pour cette clé : {msg}", code="anthropic_model",
                           corrective="Réglages → Claude : « Tester la clé » puis choisissez un modèle de la liste")
    return StepFailure(f"Erreur Anthropic ({r.status_code}) : {msg}", code="anthropic_error")


def _send(key: str, method: str, path: str, transport=None, **kw) -> dict:
    """Une nouvelle tentative automatique en cas de surcharge (429 / 529 / 5xx), en respectant retry-after."""
    for attempt in (1, 2):
        with _client(key, transport) as c:
            r = c.request(method, path, **kw)
        if r.status_code < 400:
            return r.json()
        if attempt == 1 and (r.status_code in (429, 529) or r.status_code >= 500):
            try:
                wait = min(30.0, float(r.headers.get("retry-after") or 5))
            except ValueError:
                wait = 5.0
            time.sleep(wait)
            continue
        raise _error(r)
    raise StepFailure("Anthropic injoignable", code="anthropic_error")  # pragma: no cover


def list_models(key: str, transport=None) -> list[str]:
    """Identifiants des modèles accessibles avec cette clé, du plus récent au plus ancien (ordre de l'API)."""
    ids: list[str] = []
    after = None
    for _ in range(10):
        params = {"limit": 100, **({"after_id": after} if after else {})}
        d = _send(key, "GET", "/v1/models", transport, params=params)
        ids += [m["id"] for m in d.get("data") or [] if m.get("id")]
        if not d.get("has_more"):
            break
        after = d.get("last_id")
    return ids


def pick_default(models: list[str]) -> str:
    """Sonnet le plus récent (bon équilibre qualité / coût pour écrire des prompts et décrire une image), sinon le plus
    récent des modèles listés. L'API liste les modèles du plus récent au plus ancien."""
    for fam in ("sonnet", "opus", "haiku"):
        for m in models:
            if fam in m:
                return m
    return models[0] if models else ""


def _media_type(p: pathlib.Path) -> str:
    s = p.suffix.lower()
    return "image/png" if s == ".png" else "image/webp" if s == ".webp" else "image/gif" if s == ".gif" else "image/jpeg"


def respond_json(key: str, *, model: str, prompt: str, schema: dict, name: str, images: list[pathlib.Path] | None = None,
                 max_tokens: int = 8000, transport=None) -> tuple[Any, dict]:
    """Messages API, réponse imposée par un outil unique dont le schéma d'entrée est le schéma attendu.
    Retourne (données, usage)."""
    content: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": _media_type(p),
                                                         "data": base64.b64encode(p.read_bytes()).decode("ascii")}}
                           for p in images or []]
    content.append({"type": "text", "text": prompt})
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}],
            "tools": [{"name": name, "description": "Enregistre la réponse structurée demandée.", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": name}}
    resp = _send(key, "POST", "/v1/messages", transport, json=body)
    stop = resp.get("stop_reason")
    if stop == "refusal":
        raise StepFailure("Claude a refusé de répondre", code="anthropic_refusal",
                          corrective="Vérifiez l'image et les consignes (contenu sensible ?) ou utilisez un autre moteur")
    for block in resp.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == name and isinstance(block.get("input"), dict):
            return block["input"], resp.get("usage") or {}
    if stop == "max_tokens":
        raise StepFailure("Réponse Claude tronquée (limite de longueur atteinte)", code="anthropic_incomplete",
                          corrective="Réduisez le nombre de prompts ou le niveau de détail")
    text = "".join(b.get("text") or "" for b in resp.get("content") or [] if b.get("type") == "text")
    raise StepFailure("Réponse Claude illisible (réponse structurée attendue)", code="anthropic_output", details={"text": text[:2000]})
