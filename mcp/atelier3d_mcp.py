"""Serveur MCP stdio d'Atelier 3D (SDK Python « mcp » 2.3.0 verrouillé, classe MCPServer).

Chaque outil appelle l'API locale /api/v1 : même logique, même historique que l'interface (origine « Claude Code »).
Les générations retournent un job_id immédiatement. Les binaires (.blend, .mp4…) ne transitent jamais en base64 :
download_artifact les copie par HTTP dans un dossier autorisé et renvoie chemin, taille et SHA-256.

Lancement (Claude Code) : <runtime>\\venv-app\\Scripts\\python.exe <dist>\\mcp\\atelier3d_mcp.py
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "python"))

from mcp.server.mcpserver import Image, MCPServer  # noqa: E402

from atelier3d_client import Atelier3D, Atelier3DError  # noqa: E402

import logging  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

server = MCPServer(
    name="atelier3d",
    title="Atelier 3D",
    version="1.0.0",
    instructions=(
        "Studio local image → 3D riggé → Blender, image Krea 2 et vidéo MiniMax H3. Procédure : get_capabilities → "
        "create_graph (JSON du studio, schéma 1.1) → plan_graph → run_graph → get_job_status / list_node_items → get_preview → "
        "rerun_item ou rerun_node avec une portée explicite → select_item_variant / select_variant. Les identifiants (graph_id, "
        "révision, item_id, variant_id) viennent toujours des réponses de l'API, jamais d'une position dans un tableau. "
        "Limitez les itérations, conservez les variantes, arrêtez-vous sur erreur systémique."),
)

_client: Atelier3D | None = None


def C() -> Atelier3D:
    global _client
    if _client is None:
        _client = Atelier3D(author="Claude Code")
    return _client


def _safe(fn, *a, **k) -> Any:
    try:
        return fn(*a, **k)
    except Atelier3DError as e:
        return {"error": e.payload.get("error", e.payload) if isinstance(e.payload, dict) else str(e.payload), "http_status": e.status}


def _allowed_dir(dest: str) -> pathlib.Path:
    home = pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])
    cfg = json.loads((home / "config" / "atelier3d.json").read_text(encoding="utf-8"))
    roots = [home / "data" / "exports"] + [pathlib.Path(p) for p in (cfg.get("security") or {}).get("allowed_dirs", [])]
    p = pathlib.Path(dest).resolve()
    for r in roots:
        try:
            p.relative_to(r.resolve())
            return p
        except ValueError:
            continue
    raise PermissionError(f"Dossier non autorisé. Autorisés : {', '.join(str(r) for r in roots)}")


# --------------------------------------------------------------------------- découverte
@server.tool()
def get_capabilities(refresh: bool = False) -> dict:
    """États réels des moteurs (disponible / à tester / indisponible + raison), alias de workflows et modes foreach."""
    return _safe(C().capabilities, refresh)


@server.tool()
def list_node_types() -> dict:
    """Catalogue des nodes du studio : ports typés, JSON Schema des paramètres, sous-étapes et capacités."""
    return _safe(C().node_types)


@server.tool()
def upload_asset(path: str) -> dict:
    """Importe une image, une vidéo ou un maillage depuis un dossier autorisé de la machine ; retourne upload_id."""
    return _safe(C()._req, "POST", "/api/v1/uploads", data={"path": path})


# --------------------------------------------------------------------------- tâches simples
@server.tool()
def generate_image(prompt: str, seed: int = 42001, width: int = 1024, height: int = 1024, remove_background: bool = False,
                   idempotency_key: str | None = None) -> dict:
    """Image Krea 2 Turbo locale (texte seul). Retourne job_id et graph_id immédiatement."""
    return _safe(C().create_job, "image_generate", {"prompt": prompt, "seed": seed, "width": width, "height": height,
                                                     "remove_background": remove_background}, idempotency_key=idempotency_key)


@server.tool()
def generate_asset(upload_id: str | None = None, prompt: str | None = None, category: str = "humanoid", name: str = "Personnage",
                   triangle_budget: int = 40000, texture_size: int = 2048, target_height_m: float = 1.8, full_pipeline: bool = True,
                   idempotency_key: str | None = None) -> dict:
    """Image (upload_id) ou prompt → 3D texturée ; full_pipeline=True ajoute rig (humanoïde), tests de déformation et export Blender."""
    params = {"category": category, "name": name, "triangle_budget": triangle_budget, "texture_size": texture_size,
              "target_height_m": target_height_m}
    if prompt:
        params["prompt"] = prompt
    kind = "full_pipeline" if full_pipeline else "image_to_mesh"
    return _safe(C().create_job, kind, params, upload_id=upload_id, idempotency_key=idempotency_key)


@server.tool()
def rig_asset(upload_id: str, profile: str = "humanoid", engine: str = "mia", name: str = "Rig", prepare: bool = False,
              idempotency_key: str | None = None) -> dict:
    """Maillage existant → rig + skin (MIA) → tests → export .blend/.glb/.fbx vérifié."""
    return _safe(C().create_job, "rig_existing_mesh", {"profile": profile, "rig_engine": engine, "name": name, "prepare": prepare},
                 upload_id=upload_id, idempotency_key=idempotency_key)


@server.tool()
def generate_video(prompt: str, mode: str = "image_to_video", upload_id: str | None = None, duration_s: float = 5, width: int = 480,
                   height: int = 864, seed: int = 20261005, slots: list[dict] | None = None, idempotency_key: str | None = None) -> dict:
    """Vidéo via VOTRE accès MiniMax H3 existant. mode : text_to_video | image_to_video | reference_to_video (slots avec upload_id)."""
    p = {"prompt": prompt, "mode": mode, "duration_s": duration_s, "width": width, "height": height, "seed": seed}
    if slots:
        p["slots"] = slots
    return _safe(C().create_job, "video_generate", p, upload_id=upload_id, idempotency_key=idempotency_key)


@server.tool()
def get_job_status(job_id: str) -> dict:
    """État, étape, progrès (réel ou « traitement en cours »), erreurs structurées, enfants et statut final."""
    return _safe(C().job, job_id)


@server.tool()
def cancel_job(job_id: str) -> dict:
    """Demande l'annulation (l'arrêt effectif est confirmé par l'événement job_cancelled)."""
    return _safe(C().cancel, job_id)


@server.tool()
def list_media(kind: str = "all", q: str = "", graph_id: str | None = None, limit: int = 30, offset: int = 0) -> dict:
    """Médiathèque : images, vidéos et modèles 3D générés (kind = all | image | video | mesh), filtrables par texte (prompt,
    node, graphe, item) ou par graphe. Chaque entrée donne artifact_id, graphe/node/item d'origine et « active » (sortie
    transmise en aval). Utilisez get_preview(artifact_id) pour voir une image, download_artifact pour récupérer un fichier."""
    r = _safe(C().media, kind, q, graph_id, limit, offset)
    if isinstance(r, dict) and "items" in r:
        keep = ("artifact_id", "kind", "name", "created_at", "graph_id", "graph_name", "node_id", "node_label", "step", "item_key",
                "author", "prompt", "active", "variant_id", "job_id")
        r["items"] = [{k: it.get(k) for k in keep} for it in r["items"]]
    return r


@server.tool()
def list_artifacts(job_id: str) -> dict:
    """Artefacts d'une tâche (et de ses étapes) avec empreintes SHA-256."""
    arts = _safe(C().artifacts, job_id)
    if isinstance(arts, list):
        return {"artifacts": [{k: a[k] for k in ("id", "job_id", "variant_id", "kind", "role", "path", "size", "sha256", "mime")} for a in arts]}
    return arts


@server.tool()
def get_validation_report(job_id: str) -> dict:
    """Rapports validation.json (structure, poses de test, réouverture .blend, réimport .glb/.fbx)."""
    return _safe(C().validation, job_id)


@server.tool()
def download_artifact(artifact_id: str, dest_dir: str) -> dict:
    """Copie un artefact par HTTP dans un dossier autorisé ; renvoie chemin, taille et SHA-256 (jamais de base64)."""
    try:
        d = _allowed_dir(dest_dir)
    except PermissionError as e:
        return {"error": str(e)}
    d.mkdir(parents=True, exist_ok=True)
    return _safe(C().download, artifact_id, d)


@server.tool()
def open_in_blender(job_id: str, artifact_id: str | None = None) -> dict:
    """Ouvre le .blend validé de la tâche dans le Blender local (appel backend sur un fichier autorisé)."""
    return _safe(C().open_in_blender, job_id, artifact_id)


@server.tool()
def get_preview(artifact_id: str | None = None, job_id: str | None = None, max_size: int = 768) -> list:
    """Image de contrôle exploitable en vision (taille limitée) + références des artefacts originaux.
    Pour une vidéo : frames horodatées, métadonnées et lien du clip original."""
    from PIL import Image as PILImage

    c = C()
    out: list = []
    targets = []
    if artifact_id:
        targets = [c.get(f"/api/v1/artifacts/{artifact_id}")]
    elif job_id:
        arts = c.artifacts(job_id)
        prefer = [a for a in arts if a["role"] in ("image", "preview", "pose_preview", "video_frame", "thumbnail", "comparison")]
        targets = prefer[:4] or arts[:1]
    else:
        return [{"error": "artifact_id ou job_id requis"}]
    for a in targets:
        if a["kind"] == "video":
            out.append(json.dumps({"video": a["id"], "url": a["url"], "note": "Utilisez job_id pour obtenir les frames horodatées."}, ensure_ascii=False))
            continue
        if a["kind"] not in ("image", "animation_preview"):
            out.append(json.dumps({"artifact": a["id"], "kind": a["kind"], "path": a["path"], "note": "pas d'aperçu image"}, ensure_ascii=False))
            continue
        r = c.http.get(f"/api/v1/artifacts/{a['id']}/download")
        im = PILImage.open(io.BytesIO(r.content))
        if im.mode == "RGBA":
            bg = PILImage.new("RGB", im.size, (128, 128, 128))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        im = im.convert("RGB")
        im.thumbnail((max_size, max_size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        out.append(Image(data=buf.getvalue(), format="jpeg"))
        out.append(json.dumps({"artifact_id": a["id"], "role": a["role"], "original": a["path"], "sha256": a["sha256"],
                               "time_s": (a.get("meta") or {}).get("time_s")}, ensure_ascii=False))
    return out


# --------------------------------------------------------------------------- graphes
@server.tool()
def create_graph(graph: dict) -> dict:
    """Crée un graphe du studio (graph.json, schéma 1.0 migré ou 1.1). Retourne le graphe avec ids, items et previews."""
    r = _safe(C().create_graph, graph)
    return {"graph": r.get("graph"), "validation": r.get("validation")} if isinstance(r, dict) and "graph" in r else r


@server.tool()
def delete_graph(graph_id: str) -> dict:
    """Retire un graphe de la liste (corbeille, restaurable avec restore_graph). Ses résultats restent dans la médiathèque.
    Refusé si des étapes de ce graphe sont en attente ou en cours. À n'utiliser que sur demande explicite de l'utilisateur."""
    return _safe(C().delete_graph, graph_id)


@server.tool()
def restore_graph(graph_id: str) -> dict:
    """Remet dans la liste un graphe supprimé (corbeille), avec ses variantes et résultats."""
    return _safe(C().restore_graph, graph_id)


@server.tool()
def get_graph(graph_id: str, include_status: bool = True) -> dict:
    """Graphe courant (révision, nodes, noodles, items) + états (À jour / À recalculer…) et validations."""
    r = _safe(C().graph, graph_id, include_status)
    if isinstance(r, dict) and "slots" in r:
        r["slots"] = {k: {"active_variant_id": v["active_variant_id"], "variants": [x["id"] for x in v["variants"]],
                          "last_job": (v["jobs"][-1] if v["jobs"] else None)} for k, v in r["slots"].items()}
    return r


@server.tool()
def update_graph(graph_id: str, ops: list[dict], expected_revision: int | None = None) -> dict:
    """Modifie le graphe par opérations (add_node, update_node, remove_node, add_edge, update_edge, remove_edge, set_name,
    set_groups, replace) avec contrôle de révision."""
    r = _safe(C().patch_graph, graph_id, ops, expected_revision)
    return {"graph": r.get("graph"), "changes": r.get("changes"), "validation": r.get("validation")} if isinstance(r, dict) and "graph" in r else r


@server.tool()
def plan_graph(graph_id: str, scope: str = "graph", node_id: str | None = None, item_id: str | None = None) -> dict:
    """Valide et planifie sans générer : dépendances, items foreach (« 5 prompts × 1 référence → 5 résultats »), moteurs choisis."""
    return _safe(C().plan, graph_id, scope=scope, node_id=node_id, item_id=item_id)


@server.tool()
def run_graph(graph_id: str, scope: str = "graph", node_id: str | None = None, force_recompute: bool = False,
              expected_graph_revision: int | None = None, idempotency_key: str | None = None) -> dict:
    """Lance le graphe (scope graph | until | branch) avec snapshot immuable. Ne recalcule que le nécessaire sauf force_recompute."""
    return _safe(C().run_graph, graph_id, scope=scope, node_id=node_id, force_recompute=force_recompute,
                 expected_graph_revision=expected_graph_revision, idempotency_key=idempotency_key)


@server.tool()
def rerun_node(graph_id: str, node_id: str, scope: str = "node_only", params_patch: dict | None = None, force_recompute: bool = True,
               activate_output: bool = False, step: str | None = None, expected_graph_revision: int | None = None,
               idempotency_key: str | None = None) -> dict:
    """Nouvelle variante d'une node (scope node_only | node_and_descendants | step_only | all_items). Candidate non activée par défaut."""
    return _safe(C().rerun_node, graph_id, node_id, scope=scope, params_patch=params_patch, force_recompute=force_recompute,
                 activate_output=activate_output, step=step, expected_graph_revision=expected_graph_revision, idempotency_key=idempotency_key)


@server.tool()
def list_variants(graph_id: str, node_id: str) -> dict:
    """Variantes (sorties, provenance, auteur, seed, cache) et sorties actives d'une node."""
    r = _safe(C().variants, graph_id, node_id)
    if isinstance(r, dict) and "variants" in r:
        for v in r["variants"]:
            v["artifacts"] = {k: [{"id": a["id"], "role": a["role"], "path": a["path"]} for a in arts] for k, arts in v.get("artifacts", {}).items()}
            v.pop("inputs", None)
    return r


@server.tool()
def select_variant(graph_id: str, node_id: str, variant_id: str, cascade: bool = False, pin_edges: list[str] | None = None) -> dict:
    """Change la sortie active (et l'épinglage éventuel). Retourne les nodes devenues « À recalculer »."""
    return _safe(C().select_variant, graph_id, node_id, variant_id, cascade, pin_edges)


@server.tool()
def list_node_items(graph_id: str, node_id: str) -> dict:
    """Items foreach stables (item_id, item_key, ports, seeds, preview, variantes, état). Utilisez ces item_id, jamais un index."""
    r = _safe(C().items, graph_id, node_id)
    if isinstance(r, list):
        return {"items": [{k: it.get(k) for k in ("item_id", "item_key", "label", "order", "status", "status_slot", "active_variant_id",
                                                     "latest_variant_id", "candidate_not_selected", "variants_count", "seed", "preview_node_id", "ports")}
                          | {"image_artifact": ((it.get("outputs") or {}).get("image") or [{}])[0].get("id"),
                             "last_job": (it.get("jobs") or [None])[0]} for it in r]}
    return r


@server.tool()
def rerun_item(graph_id: str, node_id: str, item_id: str, scope: str = "item_only", params_patch: dict | None = None,
               force_recompute: bool = True, activate_output: bool = False, expected_graph_revision: int | None = None,
               idempotency_key: str | None = None) -> dict:
    """Relance UNIQUEMENT cet item (ou item + descendants). Les autres items et l'amont ne sont pas recalculés."""
    return _safe(C().rerun_item, graph_id, node_id, item_id, scope=scope, params_patch=params_patch, force_recompute=force_recompute,
                 activate_output=activate_output, expected_graph_revision=expected_graph_revision, idempotency_key=idempotency_key)


@server.tool()
def cancel_item(graph_id: str, node_id: str, item_id: str) -> dict:
    """Annule les tâches en attente ou en cours de cet item seulement."""
    return _safe(C().cancel_item, graph_id, node_id, item_id)


@server.tool()
def resume_item(graph_id: str, node_id: str, item_id: str) -> dict:
    """Reprend cet item après échec/interruption (les autres résultats sont conservés)."""
    return _safe(C().resume_item, graph_id, node_id, item_id)


@server.tool()
def select_item_variant(graph_id: str, node_id: str, item_id: str, variant_id: str) -> dict:
    """Sélectionne la variante active de cet item ; seuls ses descendants qui suivent sa sortie deviennent à recalculer."""
    return _safe(C().select_item_variant, graph_id, node_id, item_id, variant_id)


if __name__ == "__main__":
    server.run("stdio")
