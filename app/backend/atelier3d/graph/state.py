"""Variantes, sorties actives, résolution des entrées et calcul d'obsolescence.

Unité d'exécution (« slot ») : (graph_id, node_id, step, item_id) — item_id vide hors foreach.
Une variante est immuable ; la sortie active d'un slot ne change que par sélection explicite
(ou par la politique sérialisée « activer la première réussite » quand aucune sortie n'existe).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..db import Database, dumps, loads, now
from ..events import emit
from . import nodetypes as NT
from .model import (LIST_SOURCES, GraphError, active_items, edges_into, exec_mode, item_key_for_edge, node_map, text_sha,
                    topo_order)

SOURCE_TYPES = {"text.prompt", "text.compose", "image.import", "mesh.import", "video.import", "reference.collection"}
PREVIEW_TYPES = {"image.preview", "mesh.preview", "video.preview", "compare.before_after"}

# sortie de node -> (étape, clé de sortie dans la variante) ; défaut : dernière étape, même nom
OUTPUT_STEP = {
    ("image.prepare", "mask"): "crop",
    ("mesh.validate", "rigged_mesh"): "deform_tests",
}


def canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha(v: Any) -> str:
    return hashlib.sha256(canon(v).encode("utf-8")).hexdigest()


def derive_item_seed(base_seed: int, item_key: str) -> int:
    """Seed effective d'un item foreach : base + 32 bits de SHA-256("item:" + item_key), modulo 2^32.

    Dépend uniquement de l'identité stable de l'item : réordonner ou renommer un prompt ne la change pas.
    """
    off = int(hashlib.sha256(f"item:{item_key}".encode("utf-8")).hexdigest()[:8], 16)
    return (int(base_seed) + off) % (2**32)


def step_params(node: dict, step: NT.Step) -> dict:
    p = NT.effective_params(node)
    p.pop("seed", None)
    if step.params is not None:
        p = {k: v for k, v in p.items() if k in step.params}
    return p


def props_text(node: dict, part: dict) -> dict:
    """Consigne d'extraction d'une partie (texte déterministe : nom, description, personnage et consigne ajoutée)."""
    from .. import props as P

    p = NT.effective_params(node)
    text = P.extraction_prompt(part, p.get("character", ""), p.get("extra", ""))
    return {"kind": "text", "text": text, "sha": text_sha(text), "node_id": node["id"], "part": part.get("key")}


def compose_text(parts: list[dict], fmt: dict, node_id: str) -> dict:
    text = fmt.get("prefix", "") + fmt.get("separator", ", ").join(x["text"] for x in parts if x["text"].strip()) + fmt.get("suffix", "")
    return {"kind": "text", "text": text, "sha": text_sha(text), "node_id": node_id, "composed_from": [x.get("node_id") for x in parts]}


def variant_row(r) -> dict:
    d = dict(r)
    for k in ("params", "inputs", "outputs", "engine", "stats"):
        d[k] = loads(d.get(k), {} if k != "inputs" else {})
    return d


class StateStore:
    def __init__(self, db: Database):
        self.db = db

    # -------------------------------------------------------------------- variantes
    def get_variant(self, variant_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM variants WHERE id = ?", (variant_id,))
        return variant_row(r) if r else None

    def list_variants(self, graph_id: str, node_id: str, step: str | None = None, item_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM variants WHERE graph_id = ? AND node_id = ?", [graph_id, node_id]
        if step is not None:
            sql += " AND step = ?"
            args.append(step)
        if item_id is not None:
            sql += " AND item_id = ?"
            args.append(item_id)
        return [variant_row(r) for r in self.db.all(sql + " ORDER BY created_at", tuple(args))]

    def active_id(self, graph_id: str, node_id: str, step: str, item_id: str = "") -> str | None:
        r = self.db.one("SELECT active_variant_id FROM slots WHERE graph_id = ? AND node_id = ? AND step = ? AND item_id = ?",
                        (graph_id, node_id, step, item_id))
        return r["active_variant_id"] if r else None

    def set_active(self, graph_id: str, node_id: str, step: str, item_id: str, variant_id: str | None, *,
                   author: str, conn=None, reason: str = "selection") -> None:
        def _do(c):
            c.execute("INSERT INTO slots(graph_id, node_id, step, item_id, active_variant_id, selected_by, updated_at) VALUES(?,?,?,?,?,?,?) "
                      "ON CONFLICT(graph_id, node_id, step, item_id) DO UPDATE SET active_variant_id = excluded.active_variant_id, "
                      "selected_by = excluded.selected_by, updated_at = excluded.updated_at",
                      (graph_id, node_id, step, item_id, variant_id, author, now()))
            emit(self.db, "active_output_changed", graph_id=graph_id, node_id=node_id, item_id=item_id or None, conn=c,
                 step=step, variant_id=variant_id, author=author, reason=reason)
        if conn is not None:
            _do(conn)
        else:
            with self.db.tx() as c:
                _do(c)

    # -------------------------------------------------------------------- résolution
    def node_steps(self, node: dict) -> list[NT.Step]:
        return NT.get(node["type"]).step_list(node)

    def item_ids(self, node: dict) -> list[str]:
        if node.get("type") in NT.IMAGE_GEN_TYPES and exec_mode(node) == "foreach_prompts":
            return [i["item_id"] for i in active_items(node)]
        return [""]

    def resolve_output(self, doc: dict, node_id: str, port: str, *, run_overrides: dict | None = None,
                       _depth: int = 0) -> dict:
        """Référence de la sortie `port` de `node_id` (texte, import, variante active, collection…)."""
        if _depth > 64:
            raise GraphError("Résolution trop profonde (cycle ?)", code="cycle")
        nodes = node_map(doc)
        n = nodes.get(node_id)
        if n is None:
            return {"kind": "missing", "reason": f"node {node_id} absente"}
        t = n["type"]
        p = NT.effective_params(n)
        if t == "text.prompt":
            text = str(p.get("text", ""))
            return {"kind": "text", "text": text, "sha": text_sha(text), "node_id": node_id}
        if t == "text.compose":
            parts = []
            for e in edges_into(doc, node_id, "fragments"):
                r = self.resolve_input_edge(doc, e, run_overrides, _depth + 1)
                parts += r["items"] if r.get("kind") == "text_list" else [r]
            fmt = {"prefix": p.get("prefix", ""), "separator": p.get("separator", ", "), "suffix": p.get("suffix", "")}
            if all(x.get("kind") == "text" for x in parts):
                return compose_text(parts, fmt, node_id)
            if any(x.get("kind") == "job" for x in parts) and all(x.get("kind") in ("text", "job") for x in parts):
                # fragment écrit dans ce même run (ex. « Prompts aléatoires ») : composé à l'exécution par le worker
                return {"kind": "compose", "parts": parts, "format": fmt, "node_id": node_id}
            return {"kind": "missing", "reason": "fragment non textuel ou pas encore écrit"}
        if t == "image.props":
            if port == "image":  # image d'entrée transmise telle quelle
                es = edges_into(doc, node_id, "image")
                if not es:
                    return {"kind": "missing", "reason": f"{node_id} : aucune image"}
                return {**self.resolve_input_edge(doc, es[0], run_overrides, _depth + 1), "via_props": node_id}
            from .. import props as P

            return {"kind": "text_list", "node_id": node_id,
                    "items": [props_text(n, part) for part in P.enabled_parts(p)]}
        if t in LIST_SOURCES:
            ref = self._slot_ref(doc["id"], node_id, "generate", "", "prompts", run_overrides)
            if port.startswith("prompt:"):
                try:
                    idx = int(port.split(":", 1)[1]) - 1
                except ValueError:
                    return {"kind": "missing", "reason": f"sortie {port} inconnue"}
                return self.list_text(ref, idx)
            n = int(p.get("count", 1))
            return {"kind": "text_list", "items": [self.list_text(ref, i) for i in range(n)], "node_id": node_id}
        if t in ("image.import", "mesh.import", "video.import"):
            uid = p.get("upload_id")
            return {"kind": "upload", "upload_id": uid, "node_id": node_id} if uid else {"kind": "missing", "reason": f"{node_id} : aucun fichier"}
        if t in PREVIEW_TYPES:
            es = edges_into(doc, node_id)
            if not es:
                return {"kind": "missing", "reason": f"{node_id} : non connectée"}
            ref = self.resolve_input_edge(doc, es[0], run_overrides, _depth + 1)
            return {**ref, "via_preview": node_id}
        if t == "reference.collection":
            slots = []
            for s in p.get("slots") or []:
                es = edges_into(doc, node_id, f"image:{s['id']}")
                ref = self.resolve_input_edge(doc, es[0], run_overrides, _depth + 1) if es else {"kind": "missing", "reason": f"slot {s['id']} vide"}
                slots.append({**s, "ref": ref})
            return {"kind": "references", "slots": slots, "node_id": node_id}
        # nodes à étapes
        steps = self.node_steps(n)
        if not steps:
            return {"kind": "missing", "reason": f"{node_id} sans étape"}
        step_id = OUTPUT_STEP.get((t, port), steps[-1].id)
        out_key = port
        if port.startswith("image:"):
            key = port.split(":", 1)[1]
            it = next((i for i in n.get("items", []) if i["item_key"] == key), None)
            if it is None:
                return {"kind": "missing", "reason": f"item {key} inconnu"}
            return self._slot_ref(doc["id"], node_id, step_id, it["item_id"], "image", run_overrides, item_key=key)
        if port == "images":
            refs = [self._slot_ref(doc["id"], node_id, step_id, it["item_id"], "image", run_overrides, item_key=it["item_key"])
                    for it in active_items(n)]
            return {"kind": "list", "items": refs, "node_id": node_id}
        return self._slot_ref(doc["id"], node_id, step_id, "", out_key, run_overrides)

    def _slot_ref(self, graph_id, node_id, step, item_id, output, run_overrides, item_key=None) -> dict:
        key = (node_id, step, item_id)
        if run_overrides and key in run_overrides:
            return {"kind": "job", "job_id": run_overrides[key], "node_id": node_id, "step": step, "item_id": item_id,
                    "output": output, "item_key": item_key}
        vid = self.active_id(graph_id, node_id, step, item_id)
        if not vid:
            return {"kind": "missing", "reason": f"{node_id}{'[' + item_key + ']' if item_key else ''} : aucune sortie active",
                    "node_id": node_id, "step": step, "item_id": item_id, "item_key": item_key}
        return {"kind": "variant", "variant_id": vid, "node_id": node_id, "step": step, "item_id": item_id, "output": output,
                "item_key": item_key}

    def generated_prompts(self, variant_id: str) -> list[str] | None:
        """Prompts écrits par une variante de « Prompts aléatoires » (métadonnées de l'artefact, sans lecture de fichier)."""
        r = self.db.one("SELECT outputs FROM variants WHERE id = ?", (variant_id,))
        ids = (loads(r["outputs"], {}) or {}).get("prompts") if r else None
        if not ids:
            return None
        a = self.db.one("SELECT meta FROM artifacts WHERE id = ?", (ids[0],))
        return (loads(a["meta"], {}) or {}).get("prompts") if a else None

    def list_text(self, ref: dict, index: int) -> dict:
        """n-ième prompt d'une liste générée : texte (variante connue), tâche du run (résolu par le worker) ou manquant."""
        k = ref.get("kind")
        if k == "job":
            return {**ref, "text_index": index}
        if k != "variant":
            return ref
        prompts = self.generated_prompts(ref["variant_id"]) or []
        if not 0 <= index < len(prompts):
            return {"kind": "missing", "reason": f"{ref.get('node_id')} : prompt {index + 1} absent ({len(prompts)} écrit(s))",
                    "node_id": ref.get("node_id")}
        text = prompts[index]
        return {"kind": "text", "text": text, "sha": text_sha(text), "node_id": ref.get("node_id"), "from_variant": ref["variant_id"],
                "index": index}

    def resolve_list_item(self, doc: dict, edge: dict, index: int, run_overrides: dict | None = None) -> dict:
        link = edge.get("link") or {}
        if link.get("mode") == "pinned" and link.get("variant_id"):
            ref = {"kind": "variant", "variant_id": link["variant_id"], "pinned": True, "node_id": edge["from"][0], "output": "prompts"}
        else:
            ref = self._slot_ref(doc["id"], edge["from"][0], "generate", "", "prompts", run_overrides)
        return {**self.list_text(ref, index), "edge_id": edge["id"]}

    def resolve_input_edge(self, doc: dict, edge: dict, run_overrides: dict | None = None, _depth: int = 0) -> dict:
        link = edge.get("link") or {}
        if link.get("mode") == "pinned" and link.get("variant_id"):
            v = self.get_variant(link["variant_id"])
            out = edge["from"][1]
            if out.startswith("image:"):
                out = "image"
            return {"kind": "variant", "variant_id": link["variant_id"], "pinned": True, "node_id": edge["from"][0],
                    "output": out, "step": v["step"] if v else None, "item_id": v["item_id"] if v else None}
        ref = self.resolve_output(doc, edge["from"][0], edge["from"][1], run_overrides=run_overrides, _depth=_depth)
        return {**ref, "edge_id": edge["id"]}

    def slot_inputs(self, doc: dict, node: dict, step_index: int, item_id: str = "", run_overrides: dict | None = None) -> dict:
        """Entrées effectives d'un slot. Étape > 0 : sortie active de l'étape précédente de la même node."""
        steps = self.node_steps(node)
        if step_index > 0:
            prev = steps[step_index - 1].id
            return {"previous": self._slot_ref(doc["id"], node["id"], prev, item_id, "*", run_overrides)}
        ins: dict[str, Any] = {}
        nt = NT.get(node["type"])
        foreach = node["type"] in NT.IMAGE_GEN_TYPES and exec_mode(node) == "foreach_prompts"
        item = next((i for i in node.get("items", []) if i["item_id"] == item_id), None) if foreach else None
        for port in nt.inputs:
            if port.dynamic_prefix:
                continue
            es = edges_into(doc, node["id"], port.name)
            if not es:
                continue
            if foreach and port.name == "prompts":
                if item and item.get("list_key"):  # partie d'une « Extraction de props » : consigne écrite depuis ses réglages
                    e = next((x for x in es if x["id"] == item.get("edge_id")), None)
                    src = node_map(doc).get(e["from"][0]) if e else None
                    part = next((x for x in ((src or {}).get("params") or {}).get("parts") or [] if x.get("key") == item["list_key"]), None)
                    ins["prompt"] = ({**props_text(src, part), "edge_id": e["id"]} if part and part.get("enabled", True)
                                     else {"kind": "missing", "reason": f"partie « {item['list_key']} » décochée ou supprimée"})
                    continue
                if item and item.get("list_index") is not None:  # prompt n d'une liste (« Prompts aléatoires »)
                    e = next((x for x in es if x["id"] == item.get("edge_id")), None)
                    ins["prompt"] = (self.resolve_list_item(doc, e, int(item["list_index"]), run_overrides) if e
                                     else {"kind": "missing", "reason": "liste de prompts débranchée"})
                    continue
                es = [e for e in es if item and item_key_for_edge(e) == item["item_key"]]
                ins["prompt"] = self.resolve_input_edge(doc, es[0], run_overrides) if es else {"kind": "missing", "reason": "item sans prompt"}
                continue
            if port.multi:
                ins[port.name] = [self.resolve_input_edge(doc, e, run_overrides) for e in es]
            else:
                ins[port.name] = self.resolve_input_edge(doc, es[0], run_overrides)
        return ins

    # -------------------------------------------------------------------- empreintes
    @staticmethod
    def ref_identity(ref: Any) -> Any:
        """Identité stable d'une référence d'entrée (pour l'obsolescence)."""
        if isinstance(ref, list):
            return [StateStore.ref_identity(r) for r in ref]
        if not isinstance(ref, dict):
            return ref
        k = ref.get("kind")
        if k == "text":
            return {"text": ref["sha"]}
        if k == "upload":
            return {"upload": ref["upload_id"]}
        if k == "variant":
            return {"variant": ref["variant_id"]}
        if k == "job":
            return {"job": ref["job_id"]}
        if k in ("list", "text_list"):
            return {"list": [StateStore.ref_identity(x) for x in ref["items"]]}
        if k == "compose":
            return {"compose": [StateStore.ref_identity(x) for x in ref["parts"]], "format": ref.get("format")}
        if k == "references":
            return {"refs": [{"id": s.get("id"), "role": s.get("role"), "label": s.get("label"), "description": s.get("description"),
                              "ref": StateStore.ref_identity(s.get("ref"))} for s in ref["slots"]]}
        if k == "missing":
            return {"missing": True}
        return {k: True}

    def fingerprint(self, inputs: dict, params: dict) -> str:
        return sha({"inputs": {k: self.ref_identity(v) for k, v in sorted(inputs.items())}, "params": params})

    # -------------------------------------------------------------------- états
    def slot_status(self, doc: dict, node: dict, step_index: int, item_id: str = "") -> dict:
        steps = self.node_steps(node)
        step = steps[step_index]
        inputs = self.slot_inputs(doc, node, step_index, item_id)
        params = step_params(node, step)
        fp = self.fingerprint(inputs, params)
        vid = self.active_id(doc["id"], node["id"], step.id, item_id)
        missing_inputs = [k for k, v in inputs.items() if _has_missing(v)]
        st: dict[str, Any] = {"node_id": node["id"], "step": step.id, "item_id": item_id, "active_variant_id": vid,
                              "expected_fingerprint": fp, "missing_inputs": missing_inputs}
        if not vid:
            st["status"] = "missing"
        else:
            v = self.get_variant(vid)
            if v is None:
                st["status"] = "missing"
            elif v["input_fingerprint"] == fp:
                st["status"] = "ok"
            else:
                reasons = []
                if self.fingerprint(v["inputs"] if isinstance(v["inputs"], dict) else {}, params) == fp:
                    reasons.append("paramètres modifiés")
                cur_ids = {k: self.ref_identity(x) for k, x in inputs.items()}
                old_ids = {k: self.ref_identity(x) for k, x in (v["inputs"] or {}).items()}
                changed = [k for k in set(cur_ids) | set(old_ids) if cur_ids.get(k) != old_ids.get(k)]
                if changed:
                    reasons.append("entrées modifiées : " + ", ".join(sorted(changed)))
                if v["params"] != params:
                    reasons.append("paramètres modifiés")
                st["status"] = "stale"
                st["reasons"] = sorted(set(reasons)) or ["contexte modifié"]
        return st

    def graph_status(self, doc: dict) -> dict:
        """État de chaque node/étape/item : ok, stale (À recalculer), missing, upstream (amont à recalculer)."""
        nodes = node_map(doc)
        out: dict[str, Any] = {}
        order = topo_order(doc)
        bad_nodes: set[str] = set()
        bad_items: dict[tuple[str, str], bool] = {}  # (node, item_key) → item d'un foreach pas à jour

        def edge_bad(e) -> bool:
            # une noodle branchée sur le port d'un item (« image:<clé> ») ne dépend que de cet item, pas des autres
            u, port = e["from"][0], e["from"][1]
            if ":" in port and (u, port.split(":", 1)[1]) in bad_items:
                return bad_items[(u, port.split(":", 1)[1])]
            return u in bad_nodes

        for nid in order:
            n = nodes[nid]
            t = n["type"]
            if t in SOURCE_TYPES or t in PREVIEW_TYPES:
                upstream_bad = any(edge_bad(e) for e in edges_into(doc, nid))
                ref = None
                if t in PREVIEW_TYPES and edges_into(doc, nid):
                    ref = self.resolve_input_edge(doc, edges_into(doc, nid)[0])
                status = "upstream" if upstream_bad else "ok"
                if t in PREVIEW_TYPES and (ref is None or ref.get("kind") == "missing"):
                    status = "missing" if not upstream_bad else "upstream"
                refs = None
                if t == "compare.before_after":  # « Avant / après » : les deux entrées
                    refs = {port: self.resolve_input_edge(doc, es[0]) for port in ("before", "after")
                            if (es := edges_into(doc, nid, port))}
                    complete = len(refs) == 2 and all(r.get("kind") != "missing" for r in refs.values())
                    status = "upstream" if upstream_bad else ("ok" if complete else "missing")
                    ref = refs.get("after") or refs.get("before")
                out[nid] = {"status": status, "ref": ref, **({"refs": refs} if refs is not None else {})}
                if status != "ok":
                    bad_nodes.add(nid)
                continue
            slots = []
            steps = self.node_steps(n)
            keys = {it["item_id"]: it["item_key"] for it in n.get("items", []) if it.get("item_id")}
            upstream_bad = any(edge_bad(e) for e in edges_into(doc, nid))
            for item_id in self.item_ids(n):
                item_slots = [self.slot_status(doc, n, i, item_id) for i in range(len(steps))]
                slots += item_slots
                if item_id in keys:
                    bad_items[(nid, keys[item_id])] = upstream_bad or any(s["status"] != "ok" for s in item_slots)
            statuses = [s["status"] for s in slots]
            if not slots:
                agg = "missing"
            elif all(s == "ok" for s in statuses):
                agg = "upstream" if upstream_bad else "ok"
            elif any(s == "stale" for s in statuses):
                agg = "stale"
            elif all(s == "missing" for s in statuses):
                agg = "missing"
            else:
                agg = "partial"
            out[nid] = {"status": agg, "slots": slots, "upstream_stale": upstream_bad}
            if agg != "ok":
                bad_nodes.add(nid)
        return out


def _has_missing(v) -> bool:
    if isinstance(v, list):
        return any(_has_missing(x) for x in v)
    if isinstance(v, dict):
        if v.get("kind") == "missing":
            return True
        if v.get("kind") in ("list", "text_list"):
            return any(_has_missing(x) for x in v["items"])
        if v.get("kind") == "compose":
            return any(_has_missing(x) for x in v["parts"])
        if v.get("kind") == "references":
            return any(_has_missing(s.get("ref")) for s in v["slots"])
    return False
