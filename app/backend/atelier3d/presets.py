"""Préréglages de graphes. Les tâches simples de l'API (POST /api/v1/jobs) sont traduites en graphes du studio :
même planificateur, mêmes variantes, même historique que l'interface."""
from __future__ import annotations

from typing import Any

PRESETS = {
    "image_to_character": "Image → personnage Blender",
    "prompt_to_character": "Prompt → concept → Blender",
    "image_to_video": "Image → vidéo MiniMax",
    "references_to_video": "Références de personnages → scène vidéo",
    "mesh_to_rig": "Mesh existant → rig",
    "pnj_concept_apose_parts": "PNJ · concept, A-pose et parties (foreach)",
    "image_to_object": "Image → objet statique Blender",
    "random_prompts_to_images": "Idées · prompts aléatoires → images (foreach)",
}

# Prompts du cahier des charges (section 18), conservés tels quels dans examples/pnj_concept_apose_parts.graph.json.
PART_PROMPTS_SPEC = [
    ("tete", "Tête", "Isole la tête de ce personnage en gros plan de face, conserve exactement les traits visibles, sans modifier son identité. Fond simple."),
    ("cheveux", "Cheveux", "Isole la coiffure de ce personnage pour une référence de modélisation, conserve forme, longueur, couleur et disposition des mèches. Fond simple."),
    ("tenue", "Tenue", "Présente la tenue de ce personnage sur un mannequin neutre en A-pose. Conserve coupe, couches, coutures, couleurs et matières visibles. Fond simple."),
    ("accessoires", "Accessoires", "Isole les accessoires visibles portés par ce personnage, conserve leurs formes et détails et dispose-les séparément sur un fond simple."),
    ("bottes", "Bottes", "Isole les bottes de ce personnage en gros plan lisible, conserve forme, couleurs, attaches et semelles visibles. Fond simple."),
]
# Préréglage : consignes explicites de cadrage (« Close-up… », « no person ») qui ont donné de meilleurs résultats réels
# avec Krea 2 Identity Edit et ref_boost 1,5 (voir TEST_REPORT.md § 3). Les libellés restent en français.
PART_PROMPTS = [
    ("tete", "Tête", "Close-up portrait of this same character: only the head and the top of the shoulders, filling the frame, front view. "
                     "Keep exactly the same face, skin tone and hairstyle. Plain light grey background."),
    ("cheveux", "Cheveux", "Close-up of this same character's head, three-quarter view, showing the hairstyle clearly. Keep exactly the same "
                           "hairstyle: length, volume, colour and shape. Plain light grey background."),
    ("tenue", "Tenue", "Show this same outfit on a neutral blank mannequin in A-pose, full body, front view. Keep exactly the same cut, "
                       "layers, seams, colours and materials. Plain light grey background."),
    ("accessoires", "Accessoires", "Lay out only the accessories this character is actually wearing, each one separately on a plain light "
                                   "grey background, product sheet, no person. Do not add any item that is not visible on the character."),
    ("bottes", "Bottes", "Product photo of this character's boots only: the pair of boots alone, no legs, no person, front three-quarter view, "
                         "filling the frame. Keep exactly the same shape, colour, straps and soles. Plain light grey background."),
]


def _n(id_, type_, label=None, **params) -> dict:
    d = {"id": id_, "type": type_, "params": params}
    if label:
        d["label"] = label
    return d


def _e(a, ap, b, bp, **kw) -> dict:
    return {"id": f"e_{a}_{ap}_{b}_{bp}".replace(":", "_"), "from": [a, ap], "to": [b, bp], **kw}


def character_chain(src: str, src_port: str, opts: dict, rig: bool = True) -> tuple[list, list, list]:
    category = opts.get("category", "humanoid")
    nodes = [
        _n("mesh", "mesh.generate", "Image → 3D", category=category, seed=int(opts.get("mesh_seed", 42)), quality=opts.get("quality", "standard"),
           texture_size=int(opts.get("texture_size", 2048))),
        _n("mesh_view", "mesh.preview", "Aperçu 3D"),
        _n("prepare", "mesh.prepare", "Préparer le maillage", triangle_budget=int(opts.get("triangle_budget", 40000)),
           texture_size=int(opts.get("texture_size", 2048)), simplify_mode=opts.get("simplify_mode", "decimate")),
    ]
    edges = [_e(src, src_port, "mesh", "image"), _e("mesh", "mesh", "mesh_view", "mesh"), _e("mesh", "mesh", "prepare", "mesh")]
    group = ["mesh", "mesh_view", "prepare"]
    if rig and category in ("humanoid", "articulated"):
        nodes += [
            _n("rig", "mesh.rig", "Rig + skin", profile="humanoid" if category == "humanoid" else "articulated",
               engine=opts.get("rig_engine", "mia"), no_fingers=bool(opts.get("no_fingers", False))),
            _n("validate", "mesh.validate", "Vérifier la déformation"),
            _n("rig_view", "mesh.preview", "Aperçu riggé", show_skeleton=True),
            _n("export", "asset.export", "Export Blender", name=opts.get("name", "Personnage"), formats=opts.get("formats", ["blend", "glb", "fbx"]),
               target_height_m=float(opts.get("target_height_m", 1.8))),
        ]
        edges += [_e("prepare", "mesh", "rig", "mesh"), _e("rig", "rigged_mesh", "validate", "rigged_mesh"),
                  _e("validate", "rigged_mesh", "rig_view", "mesh"), _e("validate", "rigged_mesh", "export", "asset")]
        group += ["rig", "validate", "rig_view", "export"]
    else:
        nodes.append(_n("export", "asset.export", "Export Blender", name=opts.get("name", "Objet"), formats=opts.get("formats", ["blend", "glb", "fbx"]),
                        target_height_m=float(opts.get("target_height_m", 0))))
        edges.append(_e("prepare", "mesh", "export", "asset"))
        group.append("export")
    return nodes, edges, group


def build(preset: str, opts: dict[str, Any]) -> dict:
    name = opts.get("graph_name") or PRESETS.get(preset, preset)
    if preset in ("image_to_character", "image_to_object"):
        if preset == "image_to_object":
            opts = {**opts, "category": opts.get("category", "static")}
        nodes = [_n("source", "image.import", "Image source", upload_id=opts.get("upload_id")),
                 _n("source_view", "image.preview", "Aperçu source")]
        edges = [_e("source", "image", "source_view", "image")]
        n2, e2, g = character_chain("source", "image", opts)
        return {"schema_version": "1.1", "name": name, "nodes": nodes + n2, "edges": edges + e2,
                "groups": [{"id": "grp_chain", "label": "Image → personnage Blender" if preset == "image_to_character" else "Image → objet Blender",
                            "node_ids": g, "collapsed": False}]}
    if preset == "prompt_to_character":
        nodes = [_n("p_image", "text.prompt", "Prompt image", text=opts.get("prompt", "Personnage entier en A-pose, mains visibles, fond simple")),
                 _n("image", "image.generate", "Générer une image", operation="create", seed=int(opts.get("seed", 42001)), width=1024, height=1024,
                    postprocess={"remove_background": True}),
                 _n("image_view", "image.preview", "Preview image")]
        edges = [_e("p_image", "text", "image", "prompt"), _e("image", "image", "image_view", "image")]
        n2, e2, g = character_chain("image_view", "image", opts)
        return {"schema_version": "1.1", "name": name, "nodes": nodes + n2, "edges": edges + e2,
                "groups": [{"id": "grp_chain", "label": "Image → personnage Blender", "node_ids": g, "collapsed": False}]}
    if preset == "mesh_to_rig":
        nodes = [_n("source", "mesh.import", "Maillage existant", upload_id=opts.get("upload_id")),
                 _n("rig", "mesh.rig", "Rig + skin", profile=opts.get("profile", "humanoid"), engine=opts.get("rig_engine", "mia")),
                 _n("validate", "mesh.validate", "Vérifier la déformation"),
                 _n("rig_view", "mesh.preview", "Aperçu riggé"),
                 _n("export", "asset.export", "Export Blender", name=opts.get("name", "Rig"), target_height_m=float(opts.get("target_height_m", 0)))]
        edges = [_e("source", "mesh", "rig", "mesh"), _e("rig", "rigged_mesh", "validate", "rigged_mesh"),
                 _e("validate", "rigged_mesh", "rig_view", "mesh"), _e("validate", "rigged_mesh", "export", "asset")]
        if opts.get("prepare", False):
            nodes.insert(1, _n("prepare", "mesh.prepare", "Préparer le maillage", triangle_budget=int(opts.get("triangle_budget", 40000))))
            edges[0] = _e("source", "mesh", "prepare", "mesh")
            edges.insert(1, _e("prepare", "mesh", "rig", "mesh"))
        return {"schema_version": "1.1", "name": name, "nodes": nodes, "edges": edges}
    if preset == "image_to_video":
        nodes = [_n("source", "image.import", "Image de départ", upload_id=opts.get("upload_id")),
                 _n("p_video", "text.prompt", "Prompt vidéo", text=opts.get("prompt", "")),
                 _n("video", "video.generate", "Générer vidéo · MiniMax H3 Ultra", mode="image_to_video", seed=int(opts.get("seed", 20261005)),
                    duration_s=float(opts.get("duration_s", 5)), width=int(opts.get("width", 480)), height=int(opts.get("height", 864))),
                 _n("video_view", "video.preview", "Preview vidéo")]
        edges = [_e("source", "image", "video", "image"), _e("p_video", "text", "video", "prompt"), _e("video", "video", "video_view", "video")]
        return {"schema_version": "1.1", "name": name, "nodes": nodes, "edges": edges}
    if preset == "references_to_video":
        slots = opts.get("slots") or [{"id": "sujet", "role": "subject", "label": "Sujet"}]
        nodes = [_n("refs", "reference.collection", "Références", slots=slots),
                 _n("p_video", "text.prompt", "Prompt vidéo", text=opts.get("prompt", "")),
                 _n("video", "video.generate", "Générer vidéo · MiniMax H3 Ultra", mode="reference_to_video", seed=int(opts.get("seed", 20261005))),
                 _n("video_view", "video.preview", "Preview vidéo")]
        edges = [_e("p_video", "text", "video", "prompt"), _e("refs", "references", "video", "references"), _e("video", "video", "video_view", "video")]
        for s in slots:
            if s.get("upload_id"):
                nid = f"ref_{s['id']}"
                nodes.append(_n(nid, "image.import", s.get("label", s["id"]), upload_id=s["upload_id"]))
                edges.append(_e(nid, "image", "refs", f"image:{s['id']}"))
        return {"schema_version": "1.1", "name": name, "nodes": nodes, "edges": edges}
    if preset == "pnj_concept_apose_parts":
        nodes = [
            # Le prompt de l'exemple du cahier des charges (examples/pnj_concept_apose_parts.graph.json) donne souvent une
            # planche multi-vues légendée avec Krea 2 ; le préréglage demande explicitement UN personnage, de face, sans texte.
            _n("p_concept", "text.prompt", "Prompt concept", text=opts.get("concept_prompt",
               "Concept art d'un seul PNJ original de science-fiction : un unique personnage en pied, vu de face, tenue détaillée, "
               "visage et membres lisibles, fond gris clair uni. Une seule vue, sans texte, sans légende, sans planche de personnage.")),
            _n("concept", "image.generate", "Concept art", operation="create", provider="krea2_turbo", seed=int(opts.get("seed", 42001)), width=1024, height=1024),
            _n("p_apose", "text.prompt", "Prompt A-pose", text=opts.get("apose_prompt",
               "Isole ce même personnage en pied en A-pose, bras légèrement écartés et mains ouvertes. Conserve visage, coiffure, proportions, tenue et accessoires. Aucun élément du corps ne doit être coupé. Retire le décor.")),
            _n("apose", "image.edit", "A-pose détourée", operation="edit", workflow_id="image_edit_default", seed=42002,
               postprocess={"remove_background": True}),
        ]
        edges = [_e("p_concept", "text", "concept", "prompt"), _e("concept", "image", "apose", "image"), _e("p_apose", "text", "apose", "prompt")]
        parts = {"id": "parts", "type": "image.edit", "label": "Parties",
                 "params": {"operation": "edit", "workflow_id": "image_edit_default", "seed": 43000, "num_variants": 1,
                            "ref_boost": float(opts.get("parts_ref_boost", 1.5))},
                 "execution": {"mode": "foreach_prompts", "shared_inputs": ["image"], "max_items": 5, "on_item_error": "continue",
                               "output_policy": "activate_first_success_only"},
                 "presentation": {"auto_previews": "per_item"}}
        nodes.append(parts)
        edges.append(_e("apose", "image", "parts", "image"))
        for order, (key, label, text) in enumerate(PART_PROMPTS_SPEC if opts.get("spec_prompts") else PART_PROMPTS):
            nodes.append(_n(f"p_{key}", "text.prompt", label, text=text))
            edges.append({"id": f"e_{key}", "from": [f"p_{key}", "text"], "to": ["parts", "prompts"], "item_key": key, "order": order})
        return {"schema_version": "1.1", "name": name, "nodes": nodes, "edges": edges}
    if preset == "random_prompts_to_images":
        # contexte → N prompts écrits en local (Qwen3-VL) → une image par prompt (Krea 2), une preview par item
        rnd = _n("idees", "text.randomize", "Idées", context=opts.get("context", "village sur une planète désertique, style Star Wars"),
                 count=int(opts.get("count", 4)), subject=opts.get("subject", "free"))
        gen = {"id": "images", "type": "image.generate", "label": "Images", "params": {"width": 1216, "height": 832},
               "execution": {"mode": "foreach_prompts", "max_items": 16, "on_item_error": "continue"},
               "presentation": {"auto_previews": "per_item"}}
        return {"schema_version": "1.1", "name": name, "nodes": [rnd, gen], "edges": [_e("idees", "prompts", "images", "prompts")]}
    raise KeyError(f"préréglage inconnu : {preset}")


JOB_KIND_TO_PRESET = {
    "image_to_mesh": ("image_to_character", {"rig": False}),
    "full_pipeline": ("image_to_character", {}),
    "rig_existing_mesh": ("mesh_to_rig", {}),
    "prepare_mesh": ("mesh_to_rig", {}),
    "video_generate": ("image_to_video", {}),
}


def build_for_job(kind: str, params: dict) -> tuple[dict, dict]:
    """Retourne (graphe, options de lancement) pour une tâche simple de l'API."""
    p = dict(params)
    if kind == "image_generate":
        doc = {"schema_version": "1.1", "name": p.get("graph_name", "Génération d'image"),
               "nodes": [_n("p_image", "text.prompt", "Prompt", text=p.get("prompt", "")),
                         _n("image", "image.generate", "Générer une image", operation="create", seed=int(p.get("seed", 42001)),
                            width=int(p.get("width", 1024)), height=int(p.get("height", 1024)), refine_prompt=bool(p.get("refine_prompt", False)),
                            postprocess={"remove_background": bool(p.get("remove_background", False))}),
                         _n("image_view", "image.preview", "Preview image")],
               "edges": [_e("p_image", "text", "image", "prompt"), _e("image", "image", "image_view", "image")]}
        return doc, {"scope": "graph"}
    if kind == "image_to_mesh":
        cat = p.get("category", "humanoid")
        doc = build("image_to_character", {**p, "category": cat})
        # génération + préparation seulement
        keep = {"source", "source_view", "mesh", "mesh_view", "prepare"}
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] in keep]
        doc["edges"] = [e for e in doc["edges"] if e["from"][0] in keep and e["to"][0] in keep]
        doc["groups"] = []
        return doc, {"scope": "graph"}
    if kind == "prepare_mesh":
        doc = {"schema_version": "1.1", "name": p.get("graph_name", "Préparation de maillage"),
               "nodes": [_n("source", "mesh.import", "Maillage", upload_id=p.get("upload_id")),
                         _n("prepare", "mesh.prepare", "Préparer le maillage", triangle_budget=int(p.get("triangle_budget", 40000))),
                         _n("export", "asset.export", "Export Blender", name=p.get("name", "Maillage"), target_height_m=float(p.get("target_height_m", 0)))],
               "edges": [_e("source", "mesh", "prepare", "mesh"), _e("prepare", "mesh", "export", "asset")]}
        return doc, {"scope": "graph"}
    if kind == "full_pipeline":
        if p.get("upload_id"):
            return build("image_to_character", p), {"scope": "graph"}
        return build("prompt_to_character", p), {"scope": "graph"}
    if kind == "rig_existing_mesh":
        return build("mesh_to_rig", p), {"scope": "graph"}
    if kind == "video_generate":
        if p.get("mode") == "reference_to_video":
            return build("references_to_video", p), {"scope": "graph"}
        doc = build("image_to_video", p)
        if p.get("mode") == "text_to_video" or not p.get("upload_id"):
            doc["nodes"] = [n for n in doc["nodes"] if n["id"] != "source"]
            doc["edges"] = [e for e in doc["edges"] if e["from"][0] != "source"]
            for n in doc["nodes"]:
                if n["id"] == "video":
                    n["params"]["mode"] = "text_to_video"
        return doc, {"scope": "graph"}
    raise KeyError(f"type de tâche inconnu : {kind}")
