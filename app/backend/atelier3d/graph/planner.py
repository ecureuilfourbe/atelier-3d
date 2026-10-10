"""Planification et lancement : portées explicites, prérequis, expansion foreach bornée, snapshot immuable."""
from __future__ import annotations

import copy
import random
from typing import Any

from ..db import Database, dumps, new_id, now
from ..events import emit
from . import nodetypes as NT
from .model import (GraphError, GraphStore, active_items, ancestors, descendants, edges_from, edges_into, exec_mode,
                    materialize, node_map, topo_order, validate)
from .state import PREVIEW_TYPES, SOURCE_TYPES, StateStore, derive_item_seed, step_params

SCOPES = {
    "graph": "tout le graphe (seulement les étapes manquantes ou à recalculer)",
    "until": "jusqu'à cette node (ses prérequis manquants inclus)",
    "branch": "cette branche (la node et ses descendants, prérequis manquants inclus)",
    "node_only": "cette node seule (nouvelle variante)",
    "node_and_descendants": "cette node et ses descendants",
    "step_only": "cette sous-étape seule",
    "item_only": "cet item seul",
    "item_and_descendants": "cet item et ses descendants",
    "all_items": "tous les items de la node",
}

GRAPH_SCOPES = {"graph", "until", "branch"}


def executable(node: dict) -> bool:
    return node["type"] not in SOURCE_TYPES and node["type"] not in PREVIEW_TYPES


class Planner:
    def __init__(self, db: Database, graphs: GraphStore, state: StateStore, capabilities_fn=None, aliases_fn=None):
        self.db = db
        self.graphs = graphs
        self.state = state
        self.capabilities_fn = capabilities_fn or (lambda: {})
        self.aliases_fn = aliases_fn or (lambda: {})

    # ------------------------------------------------------------------ helpers
    def _engine_for(self, node: dict, step: NT.Step, caps: dict, aliases: dict) -> dict:
        p = NT.effective_params(node)
        cap = caps.get(step.capability) or {}
        info = {"capability": step.capability, "status": cap.get("status", "inconnu"), "engine": cap.get("engine"),
                "reason": cap.get("reason")}
        alias_param = None
        if step.capability in ("image.create", "image.edit", "video.minimax"):
            alias_param = p.get("workflow_id") or {"image.create": "image_create_default", "image.edit": "image_edit_default",
                                                   "video.minimax": "minimax_user"}[step.capability]
        elif step.capability in ("image.upscale.seedvr2", "image.upscale.esrgan", "text.generate", "vision.local"):
            alias_param = {"image.upscale.seedvr2": "upscale_seedvr2_default", "image.upscale.esrgan": "upscale_esrgan_default",
                           "text.generate": "text_generate_default", "vision.local": "vision_generate_default"}[step.capability]
        elif step.capability == "image.remove_background":
            alias_param = "remove_background_default"
        elif step.capability == "mesh.generate":
            alias_param = "image_to_mesh_default"
        if step.capability == "video.minimax" and info["status"] != "indisponible":
            # vidéo installée avec Atelier 3D : un mode dont le modèle n'a pas été installé (ex. Références) est refusé
            mode = p.get("mode") or "image_to_video"
            if (cap.get("modes") or {}).get(mode) == "indisponible":
                info["status"] = "indisponible"
                info["reason"] = (cap.get("mode_reasons") or {}).get(mode) or f"mode vidéo « {mode} » indisponible"
        if alias_param:
            target = aliases.get(alias_param)
            info["workflow_alias"] = alias_param
            info["workflow_id"] = target
            if not target:
                info["status"] = "indisponible"
                info["reason"] = f"alias de workflow « {alias_param} » non résolu (aucun template API validé)"
        return info

    def _target_nodes(self, doc: dict, scope: str, node_id: str | None) -> list[str]:
        order = topo_order(doc)
        nodes = node_map(doc)
        if scope == "graph":
            return [n for n in order if executable(nodes[n])]
        if not node_id or node_id not in nodes:
            raise GraphError(f"node_id requis pour la portée « {scope} »", code="scope")
        if scope == "until":
            return [n for n in ancestors(doc, node_id) + [node_id] if executable(nodes[n])]
        if scope in ("branch", "node_and_descendants", "item_and_descendants"):
            return [n for n in [node_id] + descendants(doc, node_id) if executable(nodes[n])]
        return [node_id] if executable(nodes[node_id]) else []

    # ------------------------------------------------------------------ plan
    def plan(self, graph_id: str, *, scope: str = "graph", node_id: str | None = None, item_id: str | None = None,
             step: str | None = None, force: bool = False, params_patch: dict | None = None, apply: bool = True,
             author: str = "utilisateur", expected_revision: int | None = None) -> dict:
        if scope not in SCOPES:
            raise GraphError(f"Portée inconnue : {scope}", code="scope", details=list(SCOPES))
        doc = self.graphs.get(graph_id)
        if expected_revision is not None and int(expected_revision) != int(doc["revision"]):
            from .model import RevisionConflict
            raise RevisionConflict(int(expected_revision), int(doc["revision"]))
        work = copy.deepcopy(doc)
        changes = materialize(work)
        if changes and apply:
            doc = self.graphs.save(work, author=author, reason="planification : " + "; ".join(changes), expected_revision=doc["revision"])
            work = copy.deepcopy(doc)
        caps = self.capabilities_fn()
        aliases = self.aliases_fn()
        v = validate(work, capabilities=caps, aliases=aliases)
        nodes = node_map(work)
        targets = self._target_nodes(work, scope, node_id)
        target_set = set(targets)
        if scope in ("item_only", "item_and_descendants") and not item_id:
            raise GraphError("item_id requis pour cette portée", code="scope")
        if item_id and node_id:
            n = nodes[node_id]
            if item_id not in [i["item_id"] for i in n.get("items", [])]:
                raise GraphError(f"item {item_id} inconnu pour {node_id}", code="not_found", status=404)
            if item_id not in [i["item_id"] for i in active_items(n)]:
                raise GraphError(f"item {item_id} archivé (prompt retiré)", code="archived_item")

        executions: list[dict] = []
        skipped: list[dict] = []
        missing_prereq: list[dict] = []
        planned: dict[tuple, dict] = {}  # (node, step, item) -> execution
        order = topo_order(work)
        rerun_scope = scope not in GRAPH_SCOPES
        with_descendants = scope in ("node_and_descendants", "item_and_descendants")

        if rerun_scope:
            consider = [node_id] + (descendants(work, node_id) if with_descendants else [])
            consider = [x for x in consider if executable(nodes[x])]
        else:
            consider = list(targets)
            for t in list(targets):
                for a in ancestors(work, t):
                    if a not in consider and executable(nodes[a]):
                        consider.append(a)
        consider = sorted(set(consider), key=order.index)

        # Erreurs de validation : seules celles des nodes concernées par CETTE exécution bloquent. Une node incomplète ailleurs
        # (ex. « Générer vidéo » posée sans noodle) n'empêche plus de lancer une autre branche ; en « tout exécuter », elle est
        # ignorée avec ses descendantes, avec un avertissement.
        edge_target = {e["id"]: e["to"][0] for e in work.get("edges", [])}

        def err_node(e):
            return e.get("node_id") or edge_target.get(e.get("edge_id"))

        node_errors = [e for e in v["errors"] if err_node(e) in nodes]
        blocking_validation = [e for e in v["errors"] if err_node(e) not in nodes]  # erreurs globales (cycle, identifiants…)
        scope_warnings: list[dict] = []
        if scope == "graph":
            # seulement les nodes INCOMPLÈTES (entrée manquante) ; les autres erreurs (max_items, types, modes…) refusent
            incomplete = {err_node(e) for e in node_errors if e.get("code") == "missing_input"}
            incomplete -= {err_node(e) for e in node_errors if e.get("code") != "missing_input"}
            drop = set(incomplete)
            for b in incomplete:
                drop |= set(descendants(work, b))
            consider = [x for x in consider if x not in drop]
            for e in node_errors:
                if err_node(e) in incomplete:
                    scope_warnings.append({"code": "skipped_invalid", "node_id": err_node(e),
                                           "message": f"{err_node(e)} non exécutée (ni ses descendantes) : {e['message']}"})
                else:
                    blocking_validation.append(e)
        else:
            relevant = set(consider) | ({node_id} if node_id else set())
            for x in list(relevant):
                relevant |= set(ancestors(work, x))
            for e in node_errors:
                if err_node(e) in relevant:
                    blocking_validation.append(e)
                else:
                    scope_warnings.append({"code": "outside_scope", "node_id": err_node(e),
                                           "message": f"{err_node(e)} incomplète, hors de cette exécution : {e['message']}"})

        for nid in consider:
            n = nodes[nid]
            steps = self.state.node_steps(n)
            items = self.state.item_ids(n)
            if nid == node_id and item_id is not None:
                items = [item_id]
            for it in items:
                for si, st in enumerate(steps):
                    key = (nid, st.id, it)
                    status = self.state.slot_status(work, n, si, it)
                    upstream_planned = self._depends_on_planned(work, n, si, it, planned)
                    reason = None
                    if rerun_scope:
                        if nid == node_id:
                            if scope == "step_only":
                                if st.id == step:
                                    reason = "relance de la sous-étape"
                                elif upstream_planned and with_descendants:
                                    reason = "étape suivante de la relance"
                            else:
                                reason = "relance demandée" if si == 0 or not upstream_planned else "étape suivante de la relance"
                        elif upstream_planned:
                            reason = "descendant de la relance"
                    else:
                        in_target = nid in target_set
                        if force and in_target:
                            reason = "recalcul forcé"
                        elif upstream_planned:
                            reason = "amont recalculé dans ce run"
                        elif status["status"] == "missing":
                            reason = "jamais calculé" if in_target else "prérequis manquant"
                        elif status["status"] == "stale" and (in_target or scope in ("graph", "until")):
                            reason = "à recalculer" if in_target else "prérequis à recalculer"
                    if reason is None:
                        if status["status"] == "ok" or not rerun_scope:
                            skipped.append({"node_id": nid, "step": st.id, "item_id": it or None, "reason": status["status"],
                                            "active_variant_id": status["active_variant_id"]})
                        continue
                    ex = {
                        "node_id": nid, "node_type": n["type"], "label": n.get("label") or nid, "step": st.id, "step_label": st.label,
                        "step_index": si, "item_id": it or None, "item_key": self._item_key(n, it), "reason": reason,
                        "resource": st.resource, "engine": self._engine_for(n, st, caps, aliases), "capability": st.capability,
                    }
                    executions.append(ex)
                    planned[key] = ex
        # Relance ciblée : prérequis absents => refus explicite
        if rerun_scope:
            for ex in executions:
                n = nodes[ex["node_id"]]
                ins = self.state.slot_inputs(work, n, ex["step_index"], ex["item_id"] or "",
                                             run_overrides={k: "planned" for k in planned})
                for port, ref in ins.items():
                    for r in (ref if isinstance(ref, list) else [ref]):
                        if isinstance(r, dict) and r.get("kind") == "missing":
                            missing_prereq.append({"node_id": r.get("node_id"), "step": r.get("step"), "item_id": r.get("item_id"),
                                                   "for": ex["node_id"], "port": port, "reason": r.get("reason")})

        # Extraction de props : la liste des parties vient de l'analyse. Si l'analyse est (re)calculée dans ce run, les étapes qui
        # en dépendent sont reportées : le worker les lance automatiquement après avoir écrit les parties détectées.
        deferred: list[dict] = []
        continue_after = False
        analyses = [e["node_id"] for e in executions if e["node_type"] == "image.props"]
        if analyses:
            dep: set[str] = set()
            stack = [e["to"][0] for a in analyses for e in edges_from(work, a) if e["from"][1] != "image"]
            while stack:
                cur = stack.pop()
                if cur not in dep:
                    dep.add(cur)
                    stack += [e["to"][0] for e in edges_from(work, cur)]
            deferred = [e for e in executions if e["node_id"] in dep]
            executions = [e for e in executions if e["node_id"] not in dep]
            planned = {k: v for k, v in planned.items() if k[0] not in dep}
            # suite automatique : étapes reportées, ou cible qui dépend de l'analyse (liste encore vide avant la 1re analyse)
            continue_after = bool(deferred) or (node_id in dep and scope in GRAPH_SCOPES) or (scope == "graph" and bool(dep))

        foreach_summary = {}
        for nid in consider:
            n = nodes[nid]
            if n["type"] in NT.IMAGE_GEN_TYPES and exec_mode(n) == "foreach_prompts":
                its = active_items(n)
                n_img = len(edges_into(work, nid, "image"))
                previews = [x["id"] for x in work["nodes"] if (x.get("auto") or {}).get("owner_node_id") == nid and not x.get("archived")]
                foreach_summary[nid] = {
                    "items": [{"item_id": i["item_id"], "item_key": i["item_key"], "label": i.get("label"), "order": i.get("order"),
                               "seed": self._item_seed(n, i["item_key"], None)} for i in its],
                    "previews": previews,
                    "summary": f"{len(its)} prompt{'s' if len(its) > 1 else ''}"
                               + (f" × {n_img} référence{'s' if n_img > 1 else ''} commune{'s' if n_img > 1 else ''}" if n_img else "")
                               + f" → {len(its)} résultat{'s' if len(its) > 1 else ''}",
                }

        blocking = list(blocking_validation)
        unavailable = [e for e in executions if e["engine"]["status"] == "indisponible"]
        for e in unavailable:
            blocking.append({"code": "engine_unavailable", "node_id": e["node_id"],
                             "message": f"{e['node_id']} / {e['step_label']} : moteur indisponible — {e['engine'].get('reason')}"})
        if missing_prereq:
            uniq = {(m.get("node_id"), m.get("step"), m.get("item_id")): m for m in missing_prereq}
            blocking.append({"code": "missing_prerequisites",
                             "message": "Prérequis à exécuter d'abord : " + ", ".join(sorted({str(m.get('node_id')) for m in uniq.values()})) +
                                        " (portée « jusqu'à » ou « branche » pour les inclure).",
                             "details": list(uniq.values())})
        return {
            "graph_id": graph_id, "revision": work["revision"], "scope": scope, "scope_label": SCOPES[scope],
            "node_id": node_id, "item_id": item_id, "step": step, "force": force,
            "executions": executions, "skipped": skipped, "foreach": foreach_summary,
            "deferred": [{"node_id": e["node_id"], "step": e["step"], "item_id": e["item_id"]} for e in deferred],
            "continue_after_analysis": continue_after,
            "counts": {"executions": len(executions), "gpu": sum(1 for e in executions if e["resource"] == "gpu"),
                       "skipped": len(skipped)},
            "errors": blocking, "warnings": v["warnings"] + scope_warnings, "valid": not blocking,
            "materialized": changes, "doc": work,
        }

    def _depends_on_planned(self, doc, node, step_index, item_id, planned) -> bool:
        if step_index > 0:
            prev = self.state.node_steps(node)[step_index - 1].id
            return (node["id"], prev, item_id) in planned
        ins = self.state.slot_inputs(doc, node, 0, item_id, run_overrides={k: "x" for k in planned})
        return _contains_job(ins)

    @staticmethod
    def _item_key(node: dict, item_id: str) -> str | None:
        if not item_id:
            return None
        it = next((i for i in node.get("items", []) if i["item_id"] == item_id), None)
        return it["item_key"] if it else None

    @staticmethod
    def _item_seed(node: dict, item_key: str | None, patch_seed: int | None) -> int | None:
        if patch_seed is not None:
            return int(patch_seed)
        base = NT.effective_params(node).get("seed")
        if base is None:
            return None
        return derive_item_seed(int(base), item_key) if item_key else int(base)

    # ------------------------------------------------------------------ actualisation
    ACTUALIZE = {"jamais calculé", "prérequis manquant", "à recalculer", "prérequis à recalculer"}

    def adopt_up_to_date(self, graph_id: str, scope: str, node_id: str | None, author: str) -> list[dict]:
        """Avant d'actualiser : une sortie périmée est remplacée, sans recalcul, par la variante la plus récente déjà
        calculée avec EXACTEMENT les entrées et réglages actuels (empreinte identique), s'il en existe une. Parcours
        dans l'ordre du graphe : une adoption en amont peut rendre une variante aval à nouveau conforme."""
        doc = self.graphs.get(graph_id)
        nodes = node_map(doc)
        targets = self._target_nodes(doc, scope, node_id)
        consider = set(targets)
        for t in targets:
            consider.update(a for a in ancestors(doc, t) if executable(nodes[a]))
        adopted = []
        for nid in topo_order(doc):
            if nid not in consider:
                continue
            n = nodes[nid]
            for it in self.state.item_ids(n):
                for si, st in enumerate(self.state.node_steps(n)):
                    s = self.state.slot_status(doc, n, si, it)
                    if s["status"] != "stale":
                        continue
                    for v in reversed(self.state.list_variants(graph_id, nid, step=st.id, item_id=it)):
                        if v["input_fingerprint"] == s["expected_fingerprint"] and v.get("status", "succeeded") == "succeeded":
                            self.state.set_active(graph_id, nid, st.id, it, v["id"], author=author,
                                                  reason="actualisation : variante existante à jour réutilisée")
                            adopted.append({"node_id": nid, "step": st.id, "item_id": it or None, "variant_id": v["id"]})
                            break
        return adopted

    # ------------------------------------------------------------------ run
    def create_run(self, graph_id: str, *, scope: str = "graph", node_id: str | None = None, item_id: str | None = None,
                   step: str | None = None, force: bool = False, params_patch: dict | None = None,
                   activate: str | None = None, author: str = "utilisateur", expected_revision: int | None = None,
                   jobs_service=None) -> dict:
        adopted = []
        if scope in GRAPH_SCOPES and not force:
            adopted = self.adopt_up_to_date(graph_id, scope, node_id, author)
        plan = self.plan(graph_id, scope=scope, node_id=node_id, item_id=item_id, step=step, force=force,
                         params_patch=params_patch, apply=True, author=author, expected_revision=expected_revision)
        plan["adopted"] = adopted
        if not plan["valid"]:
            raise GraphError("Lancement refusé : la planification signale des erreurs", code="plan_invalid", details=plan["errors"])
        if not plan["executions"]:
            return {**{k: v for k, v in plan.items() if k != "doc"}, "run_id": None, "jobs": [],
                    "message": "Rien à exécuter : tout est à jour (utilisez force_recompute pour recalculer)."}
        doc = plan["doc"]
        nodes = node_map(doc)
        run_id = new_id("run")
        rerun_scope = scope not in GRAPH_SCOPES
        job_ids: dict[tuple, str] = {(e["node_id"], e["step"], e["item_id"] or ""): new_id("job") for e in plan["executions"]}
        jobs = []
        pol_of: dict[str, str] = {}  # job_id -> politique d'activation (pour propager l'actualisation vers l'aval)
        for e in plan["executions"]:
            n = nodes[e["node_id"]]
            st = self.state.node_steps(n)[e["step_index"]]
            it = e["item_id"] or ""
            key = (e["node_id"], e["step"], it)
            inputs = self.state.slot_inputs(doc, n, e["step_index"], it, run_overrides=job_ids)
            params = NT.effective_params(n)
            is_target = e["node_id"] == node_id
            patch = dict(params_patch or {}) if is_target else {}
            patch_seed = patch.pop("seed", None)
            params.update(patch)
            seed = None
            if st.generative:
                seed = self._item_seed(n, e["item_key"], patch_seed)
                if seed is None:
                    seed = random.randint(0, 2**32 - 1)
                params["seed"] = seed
            deps = sorted({j for j in _job_refs(inputs) if j in job_ids.values()})
            pol = activate or (n.get("execution") or {}).get("output_policy") or "activate_first_success_only"
            if rerun_scope and is_target and activate is None:
                pol = "never_if_exists"  # une relance ne remplace jamais la sortie active existante
            elif activate is None and (e["reason"] in self.ACTUALIZE or (
                    e["reason"] == "amont recalculé dans ce run" and deps and all(pol_of.get(d) == "always" for d in deps))):
                # ACTUALISATION (sortie absente ou périmée, ou aval d'une actualisation) : le nouveau résultat devient la sortie
                # utilisée — sinon l'étape resterait « à recalculer » et serait recalculée à chaque exécution
                pol = "always"
            pol_of[job_ids[key]] = pol
            jobs.append({
                "id": job_ids[key], "kind": "graph_step", "run_id": run_id, "graph_id": graph_id, "node_id": e["node_id"],
                "node_type": n["type"], "step": e["step"], "item_id": it, "item_key": e["item_key"], "resource": st.resource,
                "depends_on": deps,
                "payload": {
                    "node": {"id": n["id"], "type": n["type"], "label": n.get("label"), "execution": n.get("execution")},
                    "step": e["step"], "step_index": e["step_index"], "capability": st.capability, "generative": st.generative,
                    "params": params, "step_params": {**step_params(n, st), **{k: v for k, v in patch.items() if st.params is None or k in st.params}},
                    "seed": seed, "inputs": inputs, "engine": e["engine"], "force_recompute": bool(force),
                    "activate": pol, "item_key": e["item_key"], "reason": e["reason"], "graph_revision": doc["revision"],
                    "on_item_error": (n.get("execution") or {}).get("on_item_error", "continue"),
                    **({"continue_run": {"scope": scope, "node_id": node_id}} if plan["continue_after_analysis"] and n["type"] == "image.props" else {}),
                },
            })
        ts = now()
        with self.db.tx() as c:
            c.execute("INSERT INTO runs(id, graph_id, revision, snapshot, scope, status, author, options, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (run_id, graph_id, doc["revision"], dumps(doc), dumps({"scope": scope, "node_id": node_id, "item_id": item_id, "step": step}),
                       "queued", author, dumps({"force": force, "params_patch": params_patch, "activate": activate}), ts))
            emit(self.db, "run_created", run_id=run_id, graph_id=graph_id, conn=c, scope=scope, node_id=node_id, item_id=item_id,
                 revision=doc["revision"], jobs=[j["id"] for j in jobs], author=author)
            for j in jobs:
                jobs_service.insert_job(c, j, author=author)
        return {**{k: v for k, v in plan.items() if k != "doc"}, "run_id": run_id, "jobs": [
            {"job_id": j["id"], "node_id": j["node_id"], "step": j["step"], "item_id": j["item_id"] or None, "item_key": j["item_key"],
             "depends_on": j["depends_on"], "seed": j["payload"]["seed"]} for j in jobs]}


def _contains_job(v) -> bool:
    if isinstance(v, list):
        return any(_contains_job(x) for x in v)
    if isinstance(v, dict):
        if v.get("kind") == "job":
            return True
        if v.get("kind") == "list":
            return any(_contains_job(x) for x in v["items"])
        if v.get("kind") == "references":
            return any(_contains_job(s.get("ref")) for s in v["slots"])
        return any(_contains_job(x) for x in v.values() if isinstance(x, (dict, list)))
    return False


def _job_refs(v) -> list[str]:
    out: list[str] = []
    if isinstance(v, list):
        for x in v:
            out += _job_refs(x)
    elif isinstance(v, dict):
        if v.get("kind") == "job":
            out.append(v["job_id"])
        else:
            for x in v.values():
                if isinstance(x, (dict, list)):
                    out += _job_refs(x)
    return out
