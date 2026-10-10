"""Documents de graphe du studio (schéma 1.1) : validation, révisions, items foreach, previews automatiques.

Le graph.json est le contrat du studio ; il n'est JAMAIS envoyé tel quel à ComfyUI.
"""
from __future__ import annotations

import copy
import random
import hashlib
import re
from typing import Any

import jsonschema

from ..db import Database, dumps, loads, new_id, now
from ..events import emit
from . import nodetypes as NT

SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMAS = ("1.0", "1.1")


class GraphError(Exception):
    def __init__(self, message: str, *, code: str = "invalid_graph", details: Any = None, status: int = 422):
        super().__init__(message)
        self.code = code
        self.details = details
        self.status = status

    def to_json(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class RevisionConflict(GraphError):
    def __init__(self, expected: int, actual: int):
        super().__init__(f"Révision attendue {expected}, révision actuelle {actual}", code="revision_conflict",
                         details={"expected": expected, "actual": actual}, status=409)


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_").lower()
    return s or "x"


# ---------------------------------------------------------------------------- migration

def migrate(doc: dict) -> tuple[dict, list[str]]:
    """Migre un document 1.0 vers 1.1 (compatible : aucune node ni noodle supprimée)."""
    doc = copy.deepcopy(doc)
    notes: list[str] = []
    ver = str(doc.get("schema_version", "1.0"))
    if ver not in SUPPORTED_SCHEMAS:
        raise GraphError(f"schema_version {ver} non pris en charge (attendu : {', '.join(SUPPORTED_SCHEMAS)})", code="schema_version")
    if ver == "1.0":
        for e in doc.get("edges", []):
            if "id" not in e:
                e["id"] = f"e_{_slug(e['from'][0])}_{_slug(e['from'][1])}__{_slug(e['to'][0])}_{_slug(e['to'][1])}"
                notes.append(f"identifiant ajouté à la noodle {e['id']}")
        for n in doc.get("nodes", []):
            if n.get("type") == "image.generate":
                n.setdefault("params", {}).setdefault("operation", "create")
        doc["schema_version"] = SCHEMA_VERSION
        notes.append("schéma 1.0 → 1.1")
    return doc, notes


# ---------------------------------------------------------------------------- utilitaires

def node_map(doc: dict) -> dict[str, dict]:
    return {n["id"]: n for n in doc.get("nodes", [])}


def edges_into(doc: dict, node_id: str, port: str | None = None) -> list[dict]:
    es = [e for e in doc.get("edges", []) if e["to"][0] == node_id and (port is None or e["to"][1] == port)]
    return sorted(es, key=lambda e: (e.get("order", 0), e["id"]))


def edges_from(doc: dict, node_id: str) -> list[dict]:
    return [e for e in doc.get("edges", []) if e["from"][0] == node_id]


def exec_mode(node: dict) -> str:
    return (node.get("execution") or {}).get("mode", "single")


def item_key_for_edge(edge: dict) -> str:
    return edge.get("item_key") or _slug(edge["from"][0])


LIST_SOURCES = {"text.randomize", "image.props"}  # nodes dont la sortie « prompts » est une LISTE de prompts


def is_list_edge(doc_nodes: dict, edge: dict) -> bool:
    src = doc_nodes.get(edge["from"][0]) or {}
    return src.get("type") in LIST_SOURCES and edge["from"][1] == "prompts"


def prompt_entries(doc: dict, node: dict, nodes: dict | None = None) -> list[dict]:
    """Prompts d'un générateur « pour chaque prompt » : une entrée par noodle « Prompts », et pour une noodle venant d'une
    LISTE (« Prompts aléatoires »), une entrée par prompt de la liste (clé stable <clé de la noodle>_<n>)."""
    nodes = nodes if nodes is not None else node_map(doc)
    out = []
    for e in edges_into(doc, node["id"], "prompts"):
        key = item_key_for_edge(e)
        src = nodes.get(e["from"][0]) or {}
        if is_list_edge(nodes, e) and src.get("type") == "image.props":
            # une entrée par partie cochée ; clé d'item = clé de la partie (stable quand on coche, renomme ou réordonne)
            from .. import props as P

            for i, part in enumerate(P.enabled_parts(src.get("params") or {})):
                out.append({"edge": e, "key": part["key"], "label": part.get("name") or part["key"],
                            "order": e.get("order", 0) + i / 1000, "index": None, "list_key": part["key"]})
        elif is_list_edge(nodes, e):
            try:
                n = int(NT.effective_params(src).get("count", 1))
            except (KeyError, TypeError, ValueError):
                n = 1
            base = src.get("label") or "Prompt"
            for i in range(max(0, n)):
                out.append({"edge": e, "key": f"{key}_{i + 1}", "label": f"{base} {i + 1}",
                            "order": e.get("order", 0) + i / 100, "index": i})
        else:
            out.append({"edge": e, "key": key, "label": src.get("label") or key, "order": e.get("order", 0), "index": None})
    return out


def active_items(node: dict) -> list[dict]:
    return sorted([i for i in node.get("items", []) if i.get("status", "active") == "active"],
                  key=lambda i: (i.get("order", 0), i["item_key"]))


def topo_order(doc: dict) -> list[str]:
    nodes = node_map(doc)
    indeg = {nid: 0 for nid in nodes}
    adj: dict[str, set[str]] = {nid: set() for nid in nodes}
    for e in doc.get("edges", []):
        a, b = e["from"][0], e["to"][0]
        if a in nodes and b in nodes and b not in adj[a]:
            adj[a].add(b)
            indeg[b] += 1
    queue = sorted([n for n, d in indeg.items() if d == 0])
    out = []
    while queue:
        n = queue.pop(0)
        out.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if len(out) != len(nodes):
        cyc = sorted(n for n, d in indeg.items() if d > 0)
        raise GraphError("Cycle d'exécution détecté (un foreach est une expansion bornée, pas une boucle).",
                         code="cycle", details={"nodes": cyc})
    return out


def descendants(doc: dict, node_id: str) -> list[str]:
    out, stack = [], [node_id]
    seen = {node_id}
    while stack:
        cur = stack.pop()
        for e in edges_from(doc, cur):
            nxt = e["to"][0]
            if nxt not in seen:
                seen.add(nxt)
                out.append(nxt)
                stack.append(nxt)
    order = topo_order(doc)
    return sorted(out, key=order.index)


def ancestors(doc: dict, node_id: str) -> list[str]:
    out, stack = [], [node_id]
    seen = {node_id}
    while stack:
        cur = stack.pop()
        for e in edges_into(doc, cur):
            prv = e["from"][0]
            if prv not in seen:
                seen.add(prv)
                out.append(prv)
                stack.append(prv)
    order = topo_order(doc)
    return sorted(out, key=order.index)


def output_type(doc: dict, node_id: str, port: str) -> str | None:
    n = node_map(doc).get(node_id)
    if not n:
        return None
    try:
        nt = NT.get(n["type"])
    except KeyError:
        return None
    p = NT.output_port(nt, port)
    return p.type if p else None


# ---------------------------------------------------------------------------- matérialisation foreach

def convert_legacy(doc: dict) -> list[str]:
    """Séparation Générer / Éditer : l'ancienne node combinée en opération « edit » devient « Éditer une image » (même
    identifiant, mêmes entrées, étapes et réglages : variantes et résultats restent valides). Appliquée DÈS LA LECTURE
    d'un graphe, pas seulement à sa prochaine modification : sinon l'entrée « image » disparaîtrait de l'ancienne node
    et ses résultats passeraient « à recalculer » (constaté sur 18 résultats réels avant ce correctif)."""
    changes = []
    for node in doc.get("nodes", []):
        if node.get("type") == "image.generate" and (node.get("params") or {}).get("operation") == "edit":
            node["type"] = "image.edit"
            changes.append(f"{node['id']} : « Générer une image » (opération Éditer) devient « Éditer une image »")
    return changes


def _normalize_prompt_edges(doc: dict, node: dict, nodes: dict) -> list[str]:
    """Après un changement de mode, une noodle peut rester sur l'entrée que l'interface n'affiche plus (constaté : prompt
    branché sur « prompt » avant le passage en « pour chaque prompt », invisible et bloquant). On la déplace :
    pour chaque prompt → entrée « Prompts » (doublon d'un prompt déjà branché : retiré) ; mode unique avec une seule
    noodle « Prompts » → entrée « prompt ». Plusieurs prompts en mode unique restent une erreur explicite."""
    nid = node["id"]
    changes: list[str] = []
    single = [e for e in doc["edges"] if e["to"][0] == nid and e["to"][1] == "prompt"]
    multi = [e for e in doc["edges"] if e["to"][0] == nid and e["to"][1] == "prompts"]
    if exec_mode(node) == "foreach_prompts":
        used = {e.get("item_key") for e in multi}
        for e in single:
            if any(m["from"] == e["from"] for m in multi):
                doc["edges"].remove(e)
                changes.append(f"noodle {e['id']} retirée (même prompt déjà branché sur « Prompts » de {nid})")
                continue
            src = nodes.get(e["from"][0]) or {}
            base = _slug(src.get("label") or e["from"][0].removeprefix("p_"))
            key, k = base, 2
            while key in used:
                key, k = f"{base}_{k}", k + 1
            used.add(key)
            e["to"], e["item_key"], e["order"] = [nid, "prompts"], key, len(multi)
            multi.append(e)
            changes.append(f"noodle {e['id']} déplacée vers « Prompts » de {nid} (mode pour chaque prompt)")
    elif not single and len(multi) == 1 and not is_list_edge(nodes, multi[0]):
        e = multi[0]
        e["to"] = [nid, "prompt"]
        e.pop("item_key", None)
        e.pop("order", None)
        changes.append(f"noodle {e['id']} déplacée vers « prompt » de {nid} (mode unique)")
    return changes


def materialize(doc: dict) -> list[str]:
    """Attribue des item_id stables aux connexions de prompts foreach et crée/archive les previews automatiques.

    Retourne la liste des changements (pour l'historique). Idempotent.
    """
    changes: list[str] = []
    nodes = node_map(doc)
    changes += convert_legacy(doc)
    for node in doc.get("nodes", []):
        # node générative sans seed : seed fixée une fois pour toutes (Exécuter reproductible, cache utile) ;
        # « Régénérer » passe une nouvelle seed à la relance sans modifier ce réglage
        try:
            props = (NT.get(node["type"]).params_schema or {}).get("properties", {})
        except Exception:  # noqa: BLE001 — type inconnu : signalé par la validation
            props = {}
        if "seed" in props and (node.get("params") or {}).get("seed") is None:
            node.setdefault("params", {})["seed"] = random.randint(0, 2**31 - 1)
            changes.append(f"seed fixée pour {node['id']}")
    for node in list(doc.get("nodes", [])):
        if node.get("type") == "image.props":
            changes += _ensure_props_group(doc, node)
            nodes = node_map(doc)
    for node in list(doc.get("nodes", [])):
        if node.get("type") not in NT.IMAGE_GEN_TYPES:
            continue
        changes += _normalize_prompt_edges(doc, node, nodes)
        items = node.setdefault("items", [])
        if exec_mode(node) != "foreach_prompts":
            continue
        by_key = {i["item_key"]: i for i in items}
        live_keys = set()
        for ent in prompt_entries(doc, node, nodes):
            e, key, label = ent["edge"], ent["key"], ent["label"]
            e["item_key"] = item_key_for_edge(e)
            live_keys.add(key)
            it = by_key.get(key)
            if it is None:
                it = {"item_id": new_id("itm"), "item_key": key, "label": label, "order": ent["order"],
                      "edge_id": e["id"], "status": "active", "created_at": now()}
                if ent["index"] is not None:
                    it["list_index"] = ent["index"]
                if ent.get("list_key"):
                    it["list_key"] = ent["list_key"]
                items.append(it)
                by_key[key] = it
                changes.append(f"item {key} créé ({it['item_id']})")
            else:
                if it.get("status") != "active":
                    it["status"] = "active"
                    changes.append(f"item {key} réactivé")
                it["order"] = ent["order"]
                it["edge_id"] = e["id"]
                if ent["index"] is not None:
                    it["list_index"] = ent["index"]
                else:
                    it.pop("list_index", None)
                if ent.get("list_key"):
                    it["list_key"] = ent["list_key"]
                else:
                    it.pop("list_key", None)
                if it.get("label") != label:
                    it["label"] = label
        for it in items:
            if it["item_key"] not in live_keys and it.get("status") == "active":
                it["status"] = "archived"
                it["archived_at"] = now()
                changes.append(f"item {it['item_key']} archivé (prompt retiré)")

        if (node.get("presentation") or {}).get("auto_previews") == "per_item":
            changes += _ensure_auto_previews(doc, node)
    for node in doc.get("nodes", []):
        if node.get("type") == "image.props":
            _sync_props_group(doc, node)
    return changes


def _ensure_props_group(doc: dict, owner: dict) -> list[str]:
    """Groupe « Props » d'une node d'extraction : une node « Éditer une image » pour chaque prompt (une par partie cochée),
    previews automatiques par item. Créé UNE fois : supprimé par l'utilisateur, il n'est pas recréé."""
    info = owner.get("props_group") or {}
    if info.get("created"):
        return []
    nodes = node_map(doc)
    base = f"props_{_slug(owner['id'])}"
    gid, k = base, 2
    while gid in nodes:
        gid, k = f"{base}_{k}", k + 1
    pos = owner.get("position") or {"x": 0, "y": 0}
    engine = "openai" if (owner.get("params") or {}).get("engine", "openai") == "openai" else "krea2"
    doc["nodes"].append({"id": gid, "type": "image.edit", "label": "Props", "params": {"engine": engine, "ref_boost": 1.5},
                         "execution": {"mode": "foreach_prompts", "max_items": 32, "on_item_error": "continue",
                                       "output_policy": "activate_first_success_only"},
                         "presentation": {"auto_previews": "per_item"},
                         "position": {"x": pos.get("x", 0) + 420, "y": pos.get("y", 0)}})
    doc["edges"] += [
        {"id": f"e_{_slug(owner['id'])}_prompts_{gid}", "from": [owner["id"], "prompts"], "to": [gid, "prompts"],
         "item_key": _slug(owner["id"]), "order": 0},
        {"id": f"e_{_slug(owner['id'])}_image_{gid}", "from": [owner["id"], "image"], "to": [gid, "image"]},
    ]
    doc.setdefault("groups", []).append({"id": f"grp_{gid}", "label": f"Props · {owner.get('label') or owner['id']}",
                                         "node_ids": [gid], "collapsed": False, "owner_node_id": owner["id"]})
    owner["props_group"] = {"created": True, "node_id": gid, "engine": engine}
    return [f"groupe Props créé ({gid}, moteur {engine})"]


def _sync_props_group(doc: dict, owner: dict) -> None:
    """Le groupe suit ses previews (ajoutées, archivées) et le moteur d'analyse (OpenAI → extraction OpenAI, local → Krea 2)
    tant que l'utilisateur n'a pas choisi lui-même un autre moteur pour le groupe."""
    info = owner.get("props_group") or {}
    gid = info.get("node_id")
    gnode = node_map(doc).get(gid) if gid else None
    if gnode is not None:
        want = "openai" if (owner.get("params") or {}).get("engine", "openai") == "openai" else "krea2"
        cur = (gnode.get("params") or {}).get("engine", "krea2")
        if cur == info.get("engine") and cur != want:
            gnode.setdefault("params", {})["engine"] = want
            info["engine"] = want
    grp = next((g for g in doc.get("groups", []) if g.get("owner_node_id") == owner["id"]), None)
    if not grp or not gid or gid not in node_map(doc):
        return
    pvs = [n["id"] for n in doc["nodes"] if (n.get("auto") or {}).get("owner_node_id") == gid and not n.get("archived")]
    grp["node_ids"] = [gid] + pvs


def _ensure_auto_previews(doc: dict, owner: dict) -> list[str]:
    changes = []
    nodes = node_map(doc)
    existing = {(n.get("auto") or {}).get("item_id"): n for n in doc["nodes"]
                if (n.get("auto") or {}).get("owner_node_id") == owner["id"]}
    pos = owner.get("position") or {"x": 0, "y": 0}
    for idx, it in enumerate(sorted(owner.get("items", []), key=lambda i: (i.get("order", 0), i["item_key"]))):
        pv = existing.get(it["item_id"])
        if pv is None and it.get("status") == "active":
            pv_id = f"pv_{_slug(owner['id'])}_{_slug(it['item_key'])}"
            base, k = pv_id, 2
            while pv_id in nodes:
                pv_id = f"{base}_{k}"
                k += 1
            pv = {"id": pv_id, "type": "image.preview", "label": it.get("label") or it["item_key"], "params": {},
                  "position": {"x": pos.get("x", 0) + 380, "y": pos.get("y", 0) + idx * 300 - 300},
                  "auto": {"owner_node_id": owner["id"], "item_id": it["item_id"], "item_key": it["item_key"]}}
            doc["nodes"].append(pv)
            nodes[pv_id] = pv
            changes.append(f"preview {pv_id} créée pour l'item {it['item_key']}")
        if pv is None:
            continue
        pv["archived"] = it.get("status") != "active"
        if not pv["archived"] and pv.get("label") != it.get("label"):
            pv["label"] = it.get("label") or it["item_key"]
        want_port = f"image:{it['item_key']}"
        has = [e for e in edges_into(doc, pv["id"], "image")]
        if not has and not pv["archived"]:
            doc["edges"].append({"id": f"e_{pv['id']}", "from": [owner["id"], want_port], "to": [pv["id"], "image"], "auto": True})
            changes.append(f"noodle {owner['id']}.{want_port} → {pv['id']} créée")
    return changes


# ---------------------------------------------------------------------------- validation

def validate(doc: dict, *, capabilities: dict | None = None, aliases: dict | None = None) -> dict:
    """Validation structurelle + règles métier. Retourne {errors:[...], warnings:[...]}."""
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(code, msg, **kw):
        errors.append({"code": code, "message": msg, **kw})

    def warn(code, msg, **kw):
        warnings.append({"code": code, "message": msg, **kw})

    nodes = node_map(doc)
    if len(nodes) != len(doc.get("nodes", [])):
        err("duplicate_node", "Identifiants de nodes dupliqués")
    eids = [e.get("id") for e in doc.get("edges", [])]
    if len(set(eids)) != len(eids) or None in eids:
        err("edge_id", "Chaque noodle doit avoir un identifiant unique")

    for n in doc.get("nodes", []):
        try:
            nt = NT.get(n["type"])
        except KeyError:
            err("unknown_type", f"Type de node inconnu : {n.get('type')}", node_id=n.get("id"))
            continue
        try:
            jsonschema.validate(NT.effective_params(n), nt.params_schema)
        except jsonschema.ValidationError as ve:
            err("params", f"Paramètre invalide ({'/'.join(map(str, ve.path)) or 'params'}) : {ve.message}", node_id=n["id"])

    for e in doc.get("edges", []):
        (sn, sp), (dn, dp) = e["from"], e["to"]
        if sn not in nodes or dn not in nodes:
            err("edge_node", f"Noodle {e.get('id')} : node absente", edge_id=e.get("id"))
            continue
        try:
            snt, dnt = NT.get(nodes[sn]["type"]), NT.get(nodes[dn]["type"])
        except KeyError:
            continue
        op, ip = NT.output_port(snt, sp), NT.input_port(dnt, dp)
        if op is None:
            err("port", f"La node {sn} n'a pas de sortie « {sp} »", edge_id=e["id"])
            continue
        if ip is None:
            err("port", f"La node {dn} n'a pas d'entrée « {dp} »", edge_id=e["id"])
            continue
        if not NT.compatible(op.type, ip.type):
            err("type_mismatch", f"Branchement refusé : {sn}.{sp} ({NT.PORT_TYPES.get(op.type, op.type)}) → "
                                 f"{dn}.{dp} ({NT.PORT_TYPES.get(ip.type, ip.type)}) ; types incompatibles.", edge_id=e["id"])
        if sp.startswith("image:"):
            key = sp.split(":", 1)[1]
            src_items = {i["item_key"]: i for i in nodes[sn].get("items", [])}
            if key not in src_items and exec_mode(nodes[sn]) == "foreach_prompts":
                keys_now = {x["key"] for x in prompt_entries(doc, nodes[sn], nodes)}
                if key not in keys_now:
                    err("item_port", f"Port {sp} inexistant sur {sn} (aucun item « {key} »)", edge_id=e["id"])
            elif key in src_items and src_items[key].get("status") == "archived":
                warn("archived_item", f"{dn} dépend de l'item archivé « {key} » de {sn}", edge_id=e["id"], node_id=dn)
        if sp.startswith("prompt:") and nodes[sn].get("type") in LIST_SOURCES:
            try:
                k, cnt = int(sp.split(":", 1)[1]), int(NT.effective_params(nodes[sn]).get("count", 1))
            except (TypeError, ValueError):
                k, cnt = 0, 0
            if not 1 <= k <= cnt:
                err("item_port", f"Sortie {sp} inexistante sur {sn} : la node écrit {cnt} prompt(s)", edge_id=e["id"])
        link = e.get("link") or {}
        if link.get("mode") == "pinned" and not link.get("variant_id"):
            err("pin", f"Noodle {e['id']} épinglée sans variant_id", edge_id=e["id"])

    # multiplicité des entrées
    for n in doc.get("nodes", []):
        try:
            nt = NT.get(n["type"])
        except KeyError:
            continue
        counts: dict[str, int] = {}
        for e in edges_into(doc, n["id"]):
            counts[e["to"][1]] = counts.get(e["to"][1], 0) + 1
        for port, c in counts.items():
            ip = NT.input_port(nt, port)
            if ip and not ip.multi and c > 1:
                err("multi", f"L'entrée « {ip.label} » de {n['id']} n'accepte qu'une noodle ({c} connectées).", node_id=n["id"])

    try:
        topo_order(doc)
    except GraphError as ge:
        err(ge.code, str(ge), details=ge.details)

    for n in doc.get("nodes", []):
        _validate_business(doc, n, err, warn, capabilities, aliases)

    return {"errors": errors, "warnings": warnings, "valid": not errors}


def _validate_business(doc, n, err, warn, capabilities, aliases):
    t = n.get("type")
    nid = n.get("id")
    p = NT.effective_params(n) if t in NT.REGISTRY else {}
    has = lambda port: bool(edges_into(doc, nid, port))  # noqa: E731
    if t in NT.IMAGE_GEN_TYPES:
        mode = exec_mode(n)
        mdef = NT.EXECUTION_MODES.get(mode)
        if mdef is None:
            err("exec_mode", f"Mode d'exécution inconnu : {mode}", node_id=nid)
        elif not mdef["implemented"]:
            err("exec_mode", f"Mode « {mdef['label']} » non intégré : {mdef['reason']}", node_id=nid)
        n_prompts = len(prompt_entries(doc, n))
        if mode == "single":
            if n_prompts and not has("prompt"):
                err("single_multi", f"{nid} : {n_prompts} prompt(s) sur l'entrée collection en mode unique ; "
                                    "choisissez explicitement le mode « pour chaque prompt » ou utilisez « Composer prompt ».", node_id=nid)
            if not has("prompt") and not n_prompts:
                err("missing_input", f"{nid} : aucun prompt connecté", node_id=nid)
        if mode == "foreach_prompts":
            if has("prompt"):
                err("foreach_prompt", f"{nid} : en mode foreach, les prompts se connectent à l'entrée « Prompts » (collection).", node_id=nid)
            pe = edges_into(doc, nid, "prompts")
            if n_prompts == 0 and pe and all((node_map(doc).get(e["from"][0]) or {}).get("type") == "image.props" for e in pe):
                warn("props_pending", f"{nid} : aucune partie cochée pour l'instant (analysez l'image ou cochez des parties dans "
                                      "« Extraction de props »)", node_id=nid)
            elif n_prompts == 0:
                err("missing_input", f"{nid} : mode foreach sans aucun prompt connecté", node_id=nid)
            mx = int((n.get("execution") or {}).get("max_items", 16))
            if n_prompts > mx:
                err("max_items", f"{nid} : {n_prompts} prompts connectés mais max_items = {mx}. "
                                 "Augmentez volontairement la limite ou retirez un prompt (aucune génération lancée).", node_id=nid,
                    details={"connected": n_prompts, "max_items": mx})
        if t == "image.generate" and (has("image") or has("image_b")):
            err("text_only_engine", f"{nid} : « Générer une image » part du texte seul et n'exploite pas d'image ; "
                                    "utilisez une node « Éditer une image » pour partir d'une image.", node_id=nid)
        if t == "image.edit" and not has("image"):
            err("missing_input", f"{nid} : « Éditer une image » exige une image à éditer", node_id=nid)
        if t == "image.edit" and has("image_b") and p.get("engine") != "openai":
            err("unsupported_input", f"{nid} : la seconde référence n'est utilisée qu'avec le moteur OpenAI "
                                     "(non exposée avec Krea 2 en V1) ; retirez-la ou choisissez OpenAI.", node_id=nid)
        if p.get("engine") == "openai":
            warn("external_service", f"{nid} : moteur OpenAI — {'l’image et ' if t == 'image.edit' else ''}la consigne sont envoyées "
                                     "à OpenAI (service externe, facturé sur votre compte).", node_id=nid)
    elif t == "image.props":
        if not has("image"):
            err("missing_input", f"{nid} : « Extraction de props » exige une image", node_id=nid)
        if p.get("engine", "openai") == "openai":
            warn("external_service", f"{nid} : analyse par OpenAI — l'image est envoyée à OpenAI (service externe, facturé).", node_id=nid)
        elif p.get("engine") == "claude":
            warn("external_service", f"{nid} : analyse par Claude — l'image est envoyée à Anthropic (service externe, facturé).", node_id=nid)
    elif t == "text.randomize" and p.get("engine") == "openai":
        warn("external_service", f"{nid} : écriture par OpenAI — le contexte est envoyé à OpenAI (service externe, facturé).", node_id=nid)
    elif t == "text.randomize" and p.get("engine") == "claude":
        warn("external_service", f"{nid} : écriture par Claude — le contexte est envoyé à Anthropic (service externe, facturé).", node_id=nid)
    elif t == "video.generate":
        mode = p.get("mode")
        refs, first, last = has("references"), has("image"), has("last_image")
        if mode == "reference_to_video" and (first or last):
            err("video_mix", f"{nid} : références et première/dernière image appartiennent à des modes incompatibles.", node_id=nid)
        if mode == "reference_to_video" and not refs:
            err("missing_input", f"{nid} : mode Références → vidéo sans collection de références", node_id=nid)
        if mode == "image_to_video" and not first:
            err("missing_input", f"{nid} : mode Image → vidéo sans image de départ", node_id=nid)
        if mode == "first_last_to_video" and not (first and last):
            err("missing_input", f"{nid} : mode Début/fin → vidéo exige la première ET la dernière image", node_id=nid)
        if mode == "text_to_video" and (first or last or refs):
            err("video_mix", f"{nid} : mode Texte → vidéo avec des images connectées (elles seraient ignorées)", node_id=nid)
    else:
        try:
            nt = NT.get(t)
        except KeyError:
            return
        for ip in nt.inputs:
            if ip.required and not ip.dynamic_prefix and not has(ip.name):
                if t in ("image.preview", "mesh.preview", "video.preview") and n.get("archived"):
                    continue
                err("missing_input", f"{nid} : entrée obligatoire « {ip.label} » non connectée", node_id=nid)
        if t in ("image.import", "mesh.import", "video.import") and not p.get("upload_id"):
            warn("no_file", f"{nid} : aucun fichier importé", node_id=nid)
        if t == "text.prompt" and not str(p.get("text", "")).strip():
            warn("empty_prompt", f"{nid} : prompt vide", node_id=nid)
        if t == "compare.before_after" and not (has("before") and has("after")):
            warn("compare_incomplete", f"{nid} : branchez un résultat sur « Avant » et sur « Après »", node_id=nid)


# ---------------------------------------------------------------------------- store

class GraphStore:
    def __init__(self, db: Database):
        self.db = db

    def _row(self, graph_id: str):
        r = self.db.one("SELECT * FROM graphs WHERE id = ?", (graph_id,))
        if not r:
            raise GraphError(f"Graphe {graph_id} introuvable", code="not_found", status=404)
        return r

    def get(self, graph_id: str) -> dict:
        r = self._row(graph_id)
        doc = loads(r["doc"])
        doc["id"], doc["revision"] = r["id"], r["revision"]
        convert_legacy(doc)  # enregistré à la prochaine modification ; vu converti dès maintenant par tout le code
        return doc

    def get_revision(self, graph_id: str, revision: int) -> dict:
        r = self.db.one("SELECT doc FROM graph_revisions WHERE graph_id = ? AND revision = ?", (graph_id, revision))
        if not r:
            raise GraphError(f"Révision {revision} introuvable", code="not_found", status=404)
        doc = loads(r["doc"])
        convert_legacy(doc)
        return doc

    def list(self, archived: bool = False) -> list[dict]:
        rows = self.db.all("SELECT id, name, revision, schema_version, author, created_at, updated_at FROM graphs WHERE archived = ? "
                           "ORDER BY updated_at DESC", (1 if archived else 0,))
        return [dict(r) for r in rows]

    def set_archived(self, graph_id: str, archived: bool, *, author: str) -> dict:
        """Suppression réversible : le graphe sort de la liste (corbeille) ; révisions, variantes et fichiers sont conservés."""
        r = self._row(graph_id)
        with self.db.tx() as c:
            c.execute("UPDATE graphs SET archived = ?, updated_at = ? WHERE id = ?", (1 if archived else 0, now(), graph_id))
            emit(self.db, "graph_deleted" if archived else "graph_restored", graph_id=graph_id, conn=c, author=author, name=r["name"])
        return {"id": graph_id, "name": r["name"], "archived": archived}

    def create(self, doc: dict, *, author: str) -> dict:
        doc, notes = migrate(doc)
        doc = copy.deepcopy(doc)
        gid = doc.get("id") or new_id("gr")
        if self.db.one("SELECT 1 FROM graphs WHERE id = ?", (gid,)):
            gid = new_id("gr")
        doc["id"] = gid
        doc.setdefault("name", "Graphe sans nom")
        doc.setdefault("nodes", [])
        doc.setdefault("edges", [])
        doc.setdefault("groups", [])
        self._autolayout(doc)
        changes = materialize(doc)
        v = validate(doc)
        structural = [e for e in v["errors"] if e["code"] in ("duplicate_node", "edge_id", "unknown_type", "edge_node", "port", "type_mismatch", "cycle")]
        if structural:
            raise GraphError("Graphe invalide", details=structural)
        doc["revision"] = 1
        doc["schema_version"] = SCHEMA_VERSION
        ts = now()
        with self.db.tx() as c:
            c.execute("INSERT INTO graphs(id, name, schema_version, revision, doc, author, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (gid, doc["name"], SCHEMA_VERSION, 1, dumps(doc), author, ts, ts))
            c.execute("INSERT INTO graph_revisions(graph_id, revision, doc, author, reason, created_at) VALUES(?,?,?,?,?,?)",
                      (gid, 1, dumps(doc), author, "; ".join(notes + changes) or "création", ts))
            emit(self.db, "graph_created", graph_id=gid, conn=c, revision=1, author=author)
        return doc

    def save(self, doc: dict, *, author: str, reason: str, expected_revision: int | None = None) -> dict:
        gid = doc["id"]
        with self.db.tx() as c:
            r = c.execute("SELECT revision FROM graphs WHERE id = ?", (gid,)).fetchone()
            if not r:
                raise GraphError(f"Graphe {gid} introuvable", code="not_found", status=404)
            if expected_revision is not None and int(expected_revision) != int(r["revision"]):
                raise RevisionConflict(int(expected_revision), int(r["revision"]))
            rev = int(r["revision"]) + 1
            doc["revision"] = rev
            doc["schema_version"] = SCHEMA_VERSION
            ts = now()
            c.execute("UPDATE graphs SET doc = ?, name = ?, revision = ?, updated_at = ? WHERE id = ?", (dumps(doc), doc.get("name", ""), rev, ts, gid))
            c.execute("INSERT INTO graph_revisions(graph_id, revision, doc, author, reason, created_at) VALUES(?,?,?,?,?,?)",
                      (gid, rev, dumps(doc), author, reason, ts))
            emit(self.db, "graph_updated", graph_id=gid, conn=c, revision=rev, author=author, reason=reason)
        return doc

    def patch(self, graph_id: str, ops: list[dict], *, author: str, expected_revision: int | None = None) -> tuple[dict, list[str]]:
        doc = self.get(graph_id)
        if expected_revision is not None and int(expected_revision) != int(doc["revision"]):
            raise RevisionConflict(int(expected_revision), int(doc["revision"]))
        log: list[str] = []
        for op in ops:
            log.append(apply_op(doc, op))
        self._autolayout(doc)
        log += materialize(doc)
        v = validate(doc)
        structural = [e for e in v["errors"] if e["code"] in ("duplicate_node", "edge_id", "unknown_type", "edge_node", "port", "type_mismatch", "cycle", "multi")]
        if structural:
            raise GraphError("Modification refusée", details=structural)
        doc = self.save(doc, author=author, reason="; ".join(x for x in log if x)[:2000], expected_revision=doc["revision"])
        return doc, log

    @staticmethod
    def _autolayout(doc: dict) -> None:
        """Positions par défaut (colonnes topologiques) pour les nodes sans position ; positions existantes conservées."""
        try:
            order = topo_order(doc)
        except GraphError:
            order = [n["id"] for n in doc["nodes"]]
        depth: dict[str, int] = {}
        for nid in order:
            ins = [e["from"][0] for e in edges_into(doc, nid)]
            depth[nid] = 0 if not ins else 1 + max(depth.get(i, 0) for i in ins)
        per_col: dict[int, int] = {}
        nodes = node_map(doc)
        for nid in order:
            n = nodes[nid]
            col = depth.get(nid, 0)
            row = per_col.get(col, 0)
            per_col[col] = row + 1
            if not n.get("position"):
                n["position"] = {"x": 40 + col * 360, "y": 40 + row * 240}


def apply_op(doc: dict, op: dict) -> str:
    kind = op.get("op")
    nodes = node_map(doc)
    if kind == "add_node":
        n = copy.deepcopy(op["node"])
        n.setdefault("id", new_id("n"))
        if n["id"] in nodes:
            raise GraphError(f"Node {n['id']} existe déjà", code="duplicate_node")
        NT.get(n["type"])
        n.setdefault("params", {})
        doc["nodes"].append(n)
        return f"node {n['id']} ajoutée"
    if kind == "update_node":
        n = nodes.get(op["node_id"])
        if not n:
            raise GraphError(f"Node {op['node_id']} introuvable", code="not_found", status=404)
        for k in ("label", "position", "execution", "presentation", "size", "collapsed"):
            if k in op:
                n[k] = op[k]
        if "params" in op:
            merged = dict(n.get("params") or {})
            for k, v in op["params"].items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            n["params"] = merged
        return f"node {n['id']} modifiée"
    if kind == "remove_node":
        nid = op["node_id"]
        if nid not in nodes:
            raise GraphError(f"Node {nid} introuvable", code="not_found", status=404)
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] != nid]
        doc["edges"] = [e for e in doc["edges"] if e["from"][0] != nid and e["to"][0] != nid]
        return f"node {nid} supprimée"
    if kind == "add_edge":
        e = copy.deepcopy(op["edge"])
        e.setdefault("id", new_id("e"))
        if any(x["id"] == e["id"] for x in doc["edges"]):
            raise GraphError(f"Noodle {e['id']} existe déjà", code="edge_id")
        doc["edges"].append(e)
        return f"noodle {e['id']} ajoutée"
    if kind == "update_edge":
        for e in doc["edges"]:
            if e["id"] == op["edge_id"]:
                if "order" in op:
                    e["order"] = op["order"]
                if "link" in op:
                    e["link"] = op["link"]
                if "item_key" in op and op["item_key"] != e.get("item_key"):
                    raise GraphError("item_key est immuable (identité de l'item) ; créez une nouvelle connexion.", code="item_key")
                return f"noodle {e['id']} modifiée"
        raise GraphError(f"Noodle {op['edge_id']} introuvable", code="not_found", status=404)
    if kind == "remove_edge":
        before = len(doc["edges"])
        doc["edges"] = [e for e in doc["edges"] if e["id"] != op["edge_id"]]
        if len(doc["edges"]) == before:
            raise GraphError(f"Noodle {op['edge_id']} introuvable", code="not_found", status=404)
        return f"noodle {op['edge_id']} supprimée"
    if kind == "set_name":
        doc["name"] = op["name"]
        return "nom modifié"
    if kind == "set_groups":
        doc["groups"] = op["groups"]
        return "groupes modifiés"
    if kind == "replace":
        new, _ = migrate(op["doc"])
        old_items = {n["id"]: n.get("items") for n in doc["nodes"]}
        for n in new.get("nodes", []):
            if n.get("type") in NT.IMAGE_GEN_TYPES and not n.get("items") and old_items.get(n["id"]):
                n["items"] = old_items[n["id"]]  # les identités d'items ne se perdent pas lors d'un enregistrement UI
        doc["nodes"], doc["edges"] = new.get("nodes", []), new.get("edges", [])
        doc["groups"] = new.get("groups", doc.get("groups", []))
        doc["name"] = new.get("name", doc.get("name"))
        return "document remplacé"
    raise GraphError(f"Opération inconnue : {kind}", code="op")


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_schema() -> dict:
    """JSON Schema 1.1 du document de graphe (types et ports issus du catalogue réel)."""
    types = sorted(NT.REGISTRY)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://atelier3d.local/schemas/graph-1.1.schema.json",
        "title": "Atelier 3D — graphe du studio (graph.json) 1.1",
        "type": "object",
        "required": ["schema_version", "nodes", "edges"],
        "properties": {
            "schema_version": {"enum": ["1.0", "1.1"]},
            "id": {"type": "string"},
            "name": {"type": "string"},
            "revision": {"type": "integer"},
            "nodes": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            "edges": {"type": "array", "items": {"$ref": "#/$defs/edge"}},
            "groups": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "label": {"type": "string"}, "node_ids": {"type": "array", "items": {"type": "string"}},
                "collapsed": {"type": "boolean"}}}},
        },
        "$defs": {
            "node": {
                "type": "object", "required": ["id", "type"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"},
                    "type": {"enum": types},
                    "label": {"type": "string"},
                    "params": {"type": "object"},
                    "position": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}},
                    "execution": {"type": "object", "properties": {
                        "mode": {"enum": list(NT.EXECUTION_MODES)},
                        "shared_inputs": {"type": "array", "items": {"type": "string"}},
                        "max_items": {"type": "integer", "minimum": 1, "maximum": 64},
                        "on_item_error": {"enum": ["continue", "stop"]},
                        "output_policy": {"enum": ["activate_first_success_only", "never", "always"]}}},
                    "presentation": {"type": "object", "properties": {"auto_previews": {"enum": ["none", "per_item"]}}},
                    "items": {"type": "array", "items": {"type": "object", "required": ["item_id", "item_key"], "properties": {
                        "item_id": {"type": "string"}, "item_key": {"type": "string"}, "label": {"type": "string"},
                        "order": {"type": "integer"}, "edge_id": {"type": "string"}, "status": {"enum": ["active", "archived"]}}}},
                    "auto": {"type": "object", "properties": {"owner_node_id": {"type": "string"}, "item_id": {"type": "string"},
                                                              "item_key": {"type": "string"}}},
                    "archived": {"type": "boolean"},
                },
            },
            "edge": {
                "type": "object", "required": ["from", "to"],
                "properties": {
                    "id": {"type": "string"},
                    "from": {"type": "array", "prefixItems": [{"type": "string"}, {"type": "string"}], "minItems": 2, "maxItems": 2},
                    "to": {"type": "array", "prefixItems": [{"type": "string"}, {"type": "string"}], "minItems": 2, "maxItems": 2},
                    "item_key": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                    "order": {"type": "integer"},
                    "link": {"type": "object", "properties": {"mode": {"enum": ["follow_active", "pinned"]},
                                                              "variant_id": {"type": "string"}}},
                },
            },
        },
        "x-node-types": {t: NT.REGISTRY[t].to_json() for t in types},
    }
