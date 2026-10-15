"""Catalogue des nodes métier du studio (indépendant des classes ComfyUI internes).

Chaque type déclare : ports typés, paramètres (JSON Schema), sous-étapes relançables séparément,
capacité moteur requise par étape, nature (générative / déterministe / import / preview / export).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------- types de ports
PORT_TYPES = {
    "Prompt": "Texte / prompt",
    "PromptList": "Collection de prompts",
    "Image": "Image",
    "ImageList": "Liste d'images",
    "Mask": "Masque",
    "ReferenceCollection": "Collection de références",
    "Mesh": "Maillage (avec matériaux/textures)",
    "Materials": "Matériaux",
    "RiggedMesh": "Maillage riggé",
    "Animation": "Mouvement (squelette animé)",
    "Video": "Vidéo",
    "Report": "Rapport",
    "Files": "Fichiers exportés",
    "Media": "Image, vidéo ou 3D",
}

# Conversions autorisées (source -> cibles acceptées), sans transformation de données.
COMPATIBLE = {
    "Prompt": {"Prompt", "PromptList"},
    "Image": {"Image", "ImageList", "Media"},
    "ImageList": {"ImageList"},
    "Mask": {"Mask"},
    "ReferenceCollection": {"ReferenceCollection"},
    "Mesh": {"Mesh", "Media"},
    "RiggedMesh": {"RiggedMesh", "Mesh", "Media"},
    "Video": {"Video", "Media"},
    "Report": {"Report"},
    "Files": {"Files"},
    "Materials": {"Materials"},
    "Animation": {"Animation"},
}


def compatible(src_type: str, dst_type: str) -> bool:
    return dst_type in COMPATIBLE.get(src_type, {src_type})


@dataclass
class Port:
    name: str
    type: str
    label: str
    multi: bool = False          # plusieurs noodles (identité + ordre persistants)
    required: bool = False
    dynamic_prefix: str | None = None  # ex. "image:" pour image:<slot>
    description: str = ""

    def to_json(self) -> dict:
        d = {"name": self.name, "type": self.type, "type_label": PORT_TYPES.get(self.type, self.type),
             "label": self.label, "multi": self.multi, "required": self.required}
        if self.dynamic_prefix:
            d["dynamic_prefix"] = self.dynamic_prefix
        if self.description:
            d["description"] = self.description
        return d


@dataclass
class Step:
    id: str
    label: str
    capability: str           # capacité moteur requise
    resource: str             # "gpu" | "cpu" | "none"
    generative: bool = False  # accepte une seed
    params: tuple | None = None  # paramètres qui influencent cette étape (None = tous sauf la seed)
    # entrées de la node utilisées par cette étape (None = toutes pour la 1re étape, aucune ensuite : sortie précédente seule)
    inputs: tuple | None = None


@dataclass
class NodeType:
    type: str
    label: str
    category: str
    kind: str  # source | generative | deterministic | import | preview | export | collection
    description: str
    inputs: list[Port]
    outputs: list[Port]
    params_schema: dict
    steps: Callable[[dict], list[Step]] = field(default=lambda node: [])
    actions: list[str] = field(default_factory=list)
    execution_modes: dict[str, dict] = field(default_factory=dict)

    def step_list(self, node: dict) -> list[Step]:
        return self.steps(node)

    def to_json(self) -> dict:
        return {
            "type": self.type, "label": self.label, "category": self.category, "kind": self.kind,
            "description": self.description,
            "inputs": [p.to_json() for p in self.inputs], "outputs": [p.to_json() for p in self.outputs],
            "params_schema": self.params_schema, "actions": self.actions,
            "execution_modes": self.execution_modes,
        }


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": True}


SEED = {"type": "integer", "minimum": 0, "maximum": 2**53 - 1, "title": "Seed"}

# ---------------------------------------------------------------------------- étapes dynamiques


IMAGE_GEN_TYPES = ("image.generate", "image.edit")
OPENAI_STEP_PARAMS = ("operation", "engine", "openai_model", "quality", "background", "moderation", "width", "height", "num_variants")
# Qualité / modération d'une node OpenAI : vide = valeur par défaut de Réglages → OpenAI (résolue à l'exécution).
OPENAI_QUALITY = {"type": "string", "enum": ["", "auto", "low", "medium", "high", "xhigh", "max"], "default": "", "title": "Qualité",
                  "x-engine": "openai", "description": "xhigh / max : modèles GPT Image 2.5 seulement ; coût plus élevé.",
                  "enumLabels": {"": "par défaut des Réglages", "auto": "automatique", "low": "basse (rapide, économique)",
                                 "medium": "moyenne", "high": "haute", "xhigh": "très haute (GPT Image 2.5)", "max": "maximale (GPT Image 2.5)"}}
OPENAI_MODERATION = {"type": "string", "enum": ["", "auto", "low"], "default": "", "title": "Modération OpenAI", "x-engine": "openai",
                     "x-advanced": True, "enumLabels": {"": "par défaut des Réglages", "auto": "standard (auto)", "low": "moins stricte (low)"}}


def _image_generate_steps(node: dict) -> list[Step]:
    """Générer (texte → image) ou Éditer (image + consigne). Les listes de réglages des étapes Krea sont inchangées depuis
    la séparation des deux nodes : les empreintes, donc les résultats déjà calculés, restent valides."""
    p = node.get("params", {})
    op = "edit" if node.get("type") == "image.edit" else p.get("operation", "create")
    steps = []
    if p.get("engine") == "openai":
        # service externe : file « réseau » du worker (ne bloque ni le GPU ni Blender)
        steps.append(Step(op, "Édition (OpenAI, service externe)" if op == "edit" else "Génération (OpenAI, service externe)",
                          "image.openai", "net", generative=True, params=OPENAI_STEP_PARAMS))
    elif op == "edit":
        steps.append(Step("edit", "Édition avec référence", "image.edit", "gpu", generative=True,
                          params=("operation", "workflow_id", "width", "height", "steps", "ref_boost", "grounding_px", "num_variants")))
    else:
        steps.append(Step("create", "Génération", "image.create", "gpu", generative=True,
                          params=("operation", "workflow_id", "width", "height", "refine_prompt", "num_variants")))
    if (p.get("postprocess") or {}).get("remove_background"):
        steps.append(Step("remove_background", "Détourage (masque alpha)", "image.remove_background", "gpu", params=("postprocess",)))
    return steps


POSE_STEP_PARAMS = ("pose", "engine", "pose_details", "ref_boost", "openai_model", "quality", "moderation")


def _image_prepare_steps(node: dict) -> list[Step]:
    """Pose A/T (édition, facultative), détourage BiRefNet (facultatif), cadrage. Sans pose et avec détourage (défauts),
    les deux étapes historiques sont inchangées (mêmes réglages) : les résultats déjà calculés restent valides."""
    p = node.get("params") or {}
    steps = []
    if p.get("pose", "none") in ("a_pose", "t_pose"):
        if p.get("engine") == "openai":
            steps.append(Step("pose", "Pose A/T (OpenAI, service externe)", "image.openai", "net", generative=True, params=POSE_STEP_PARAMS))
        else:
            steps.append(Step("pose", "Pose A/T (édition Krea 2)", "image.edit", "gpu", generative=True, params=POSE_STEP_PARAMS))
    if p.get("remove_background", True):
        steps.append(Step("remove_background", "Détourage (masque alpha)", "image.remove_background", "gpu", params=()))
    steps.append(Step("crop", "Cadrage et contrôle", "local", "cpu", params=("crop_to_subject", "padding")))
    return steps


def _image_upscale_steps(node: dict) -> list[Step]:
    seedvr2 = (node.get("params") or {}).get("engine", "seedvr2") == "seedvr2"
    # une capacité par moteur : statut, workflow et dernière réussite propres à chacun
    return [Step("upscale", "Upscale (SeedVR2, détails)" if seedvr2 else "Upscale (Real-ESRGAN, rapide)",
                 "image.upscale.seedvr2" if seedvr2 else "image.upscale.esrgan", "gpu", generative=seedvr2,
                 params=("engine", "resolution", "color_correction"))]


RANDOMIZE_PARAMS = ("context", "count", "language", "subject", "extra", "creativity")


def _randomize_steps(node: dict) -> list[Step]:
    if (node.get("params") or {}).get("engine") == "claude":
        return [Step("generate", "Écriture des prompts (Claude, service externe)", "text.claude", "net", generative=True,
                     params=RANDOMIZE_PARAMS + ("engine", "claude_model"))]
    if (node.get("params") or {}).get("engine") == "openai":
        return [Step("generate", "Écriture des prompts (OpenAI, service externe)", "text.openai", "net", generative=True,
                     params=RANDOMIZE_PARAMS + ("engine", "openai_model"))]
    return [Step("generate", "Écriture des prompts (Qwen3-VL 4B, local)", "text.generate", "gpu", generative=True, params=RANDOMIZE_PARAMS)]


PROPS_ANALYSIS_PARAMS = ("include", "detail", "merge_symmetric", "engine", "openai_model")


def _props_steps(node: dict) -> list[Step]:
    if (node.get("params") or {}).get("engine") == "claude":  # paramètres distincts : empreintes OpenAI / local inchangées
        return [Step("analyze", "Analyse de l'image (Claude, service externe)", "vision.claude", "net",
                     params=PROPS_ANALYSIS_PARAMS + ("claude_model",))]
    if (node.get("params") or {}).get("engine", "openai") == "openai":
        return [Step("analyze", "Analyse de l'image (OpenAI, service externe)", "vision.openai", "net", params=PROPS_ANALYSIS_PARAMS)]
    return [Step("analyze", "Analyse de l'image (Qwen3-VL 4B, local)", "vision.local", "gpu", generative=True, params=PROPS_ANALYSIS_PARAMS)]


def _mesh_rig_steps(node: dict) -> list[Step]:
    p = node.get("params", {})
    profile = p.get("profile", "humanoid")
    if profile == "articulated":
        return [Step("articulated_rig", "Armature à pivots", "blender", "cpu", params=("profile", "pivots"))]
    engine = p.get("engine", "mia")
    if engine == "blender_auto":
        return [Step("skeleton", "Squelette (MIA, articulations seules)", "rig.mia", "gpu", params=("engine", "profile", "no_fingers")),
                Step("bind", "Poids automatiques Blender (à vérifier)", "blender", "cpu", params=("engine", "joint_overrides", "no_fingers"))]
    return [Step("skeleton_skin", "Squelette + poids (Make-It-Animatable)", "rig.mia", "gpu",
                 params=("engine", "profile", "no_fingers", "input_normal")),
            Step("bind", "Assemblage Blender (armature, groupes, modificateur)", "blender", "cpu", params=("engine", "joint_overrides"))]


EXECUTION_MODES = {
    "single": {"label": "Unique", "implemented": True,
               "description": "Une consigne et les références autorisées pour une génération."},
    "foreach_prompts": {"label": "Pour chaque prompt", "implemented": True,
                        "description": "Un traitement indépendant par prompt connecté, avec la même image/référence partagée."},
    "foreach_images": {"label": "Pour chaque image", "implemented": False,
                       "reason": "Non intégré en V1 : prévu avec une entrée ImageList multi-connexion."},
    "zip": {"label": "Appariement i ↔ i", "implemented": False,
            "reason": "Non intégré en V1 (le contrôle de longueurs est déjà refusé à la planification)."},
    "cartesian": {"label": "Toutes les paires", "implemented": False,
                  "reason": "Non intégré en V1 : nécessite un affichage du nombre total et une limite explicite."},
}

REGISTRY: dict[str, NodeType] = {}


def register(nt: NodeType) -> None:
    REGISTRY[nt.type] = nt


register(NodeType(
    type="text.prompt", label="Prompt", category="Texte", kind="source",
    description="Texte réutilisable ; plusieurs générateurs peuvent utiliser le même prompt.",
    inputs=[], outputs=[Port("text", "Prompt", "Texte")],
    params_schema=_schema({"text": {"type": "string", "title": "Texte", "default": "", "format": "textarea"}}, ["text"]),
    actions=["edit"],
))

register(NodeType(
    type="text.compose", label="Composer prompt", category="Texte", kind="deterministic",
    description="Assemble plusieurs fragments (ordre des connexions) en UN prompt affiché en entier.",
    inputs=[Port("fragments", "PromptList", "Fragments", multi=True, required=True)],
    outputs=[Port("text", "Prompt", "Texte final")],
    params_schema=_schema({
        "separator": {"type": "string", "title": "Séparateur", "default": ", "},
        "prefix": {"type": "string", "title": "Préfixe", "default": ""},
        "suffix": {"type": "string", "title": "Suffixe", "default": ""},
    }),
))

register(NodeType(
    type="text.randomize", label="Prompts aléatoires", category="Texte", kind="generative",
    description="Écrit N prompts variés à partir d'un contexte (« village sur une planète désertique, Star Wars »), avec "
                "Qwen3-VL 4B en local. Sortie « Prompts » : un item par prompt sur un générateur « pour chaque prompt » ; "
                "sorties « Prompt n » : un prompt à la fois.",
    inputs=[Port("context", "Prompt", "Contexte", description="Facultatif : remplace le contexte écrit dans la node.")],
    outputs=[Port("prompts", "PromptList", "Prompts (liste)", description="Un item par prompt généré (générateur en mode « pour chaque prompt »)."),
             Port("prompt:", "Prompt", "Prompt n", dynamic_prefix="prompt:", description="Le n-ième prompt généré (prompt:1 … prompt:N).")],
    params_schema=_schema({
        "context": {"type": "string", "default": "", "title": "Contexte", "format": "textarea",
                    "description": "Thème, univers, ambiance : en français ou en anglais."},
        "count": {"type": "integer", "minimum": 1, "maximum": 16, "default": 5, "title": "Nombre de prompts"},
        "language": {"type": "string", "enum": ["en", "fr"], "default": "en", "title": "Langue des prompts",
                     "enumLabels": {"en": "anglais (conseillé pour Krea 2)", "fr": "français"}},
        "subject": {"type": "string", "enum": ["free", "scene", "character", "object", "creature"], "default": "free",
                    "title": "Type de sujet", "enumLabels": {"free": "libre", "scene": "lieux / scènes", "character": "personnages",
                                                            "object": "objets / accessoires", "creature": "créatures"}},
        "extra": {"type": "string", "default": "", "title": "Consignes supplémentaires", "format": "textarea", "x-advanced": True,
                  "description": "Ex. « un seul personnage, de face, sans texte, fond neutre »."},
        "creativity": {"type": "number", "minimum": 0.2, "maximum": 1.5, "default": 0.9, "title": "Créativité (température)",
                       "x-advanced": True, "x-engine": "local"},
        "engine": {"type": "string", "enum": ["local", "openai", "claude"], "default": "local", "title": "Moteur",
                   "enumLabels": {"local": "Qwen3-VL 4B (local, sur ce PC)", "openai": "OpenAI / ChatGPT (service externe)",
                                  "claude": "Claude / Anthropic (service externe)"}},
        "openai_model": {"type": "string", "default": "", "title": "Modèle OpenAI (texte)", "x-engine": "openai", "x-openai-text-models": True,
                         "description": "Vide : modèle texte par défaut des Réglages → OpenAI."},
        "claude_model": {"type": "string", "default": "", "title": "Modèle Claude", "x-engine": "claude", "x-claude-models": True,
                         "description": "Vide : modèle par défaut des Réglages → Claude."},
        "seed": SEED,
    }),
    steps=_randomize_steps,
    actions=["regenerate", "select_variant"],
))

register(NodeType(
    type="image.import", label="Importer une image", category="Image", kind="import",
    description="Fichier PNG/JPEG/WebP ; l'original est conservé. Plusieurs instances possibles.",
    inputs=[], outputs=[Port("image", "Image", "Image")],
    params_schema=_schema({"upload_id": {"type": "string", "title": "Fichier importé"}}),
    actions=["replace"],
))

IMAGE_OUTPUTS = [
    Port("image", "Image", "Image", description="Mode unique : variante sélectionnée."),
    Port("images", "ImageList", "Images (collection)", description="Mode foreach : sorties de chaque item, identité conservée."),
    Port("image:", "Image", "Image d'un item", dynamic_prefix="image:", description="Mode foreach : port stable image:<item_key>."),
]
PROMPT_INPUTS = [
    Port("prompt", "Prompt", "Prompt", description="Mode unique : une seule consigne."),
    Port("prompts", "PromptList", "Prompts", multi=True,
         description="Mode foreach : une tâche indépendante par noodle (item_key et ordre persistants)."),
]

register(NodeType(
    type="image.generate", label="Générer une image", category="Image", kind="generative",
    description="Texte → image : Krea 2 Turbo (local) ou OpenAI / ChatGPT Images (service externe). "
                "Modes unique ou pour chaque prompt (foreach).",
    inputs=list(PROMPT_INPUTS),
    outputs=list(IMAGE_OUTPUTS),
    params_schema=_schema({
        "operation": {"type": "string", "enum": ["create", "edit"], "default": "create", "title": "Opération", "x-hidden": True,
                      "description": "Toujours « create » ; « edit » (anciens graphes) est converti en node « Éditer une image »."},
        "engine": {"type": "string", "enum": ["krea2", "openai"], "default": "krea2", "title": "Moteur",
                   "enumLabels": {"krea2": "Krea 2 (local, sur ce PC)", "openai": "OpenAI / ChatGPT Images (service externe)"}},
        "openai_model": {"type": "string", "default": "", "title": "Modèle OpenAI", "x-engine": "openai", "x-openai-models": True,
                         "description": "Vide : modèle par défaut choisi dans Réglages → OpenAI."},
        "quality": OPENAI_QUALITY,
        "background": {"type": "string", "enum": ["auto", "opaque", "transparent"], "default": "auto", "title": "Fond",
                       "x-engine": "openai",
                       "enumLabels": {"auto": "automatique", "opaque": "opaque", "transparent": "transparent (PNG avec alpha)"}},
        "moderation": OPENAI_MODERATION,
        "provider": {"type": "string", "title": "Moteur (informatif)", "default": "krea2_turbo", "x-hidden": True},
        "workflow_id": {"type": "string", "title": "Workflow (alias)", "default": "", "x-engine": "krea2", "x-advanced": True},
        "seed": SEED,
        "width": {"type": "integer", "minimum": 256, "maximum": 3840, "multipleOf": 16, "default": 1024, "title": "Largeur"},
        "height": {"type": "integer", "minimum": 256, "maximum": 3840, "multipleOf": 16, "default": 1024, "title": "Hauteur"},
        "refine_prompt": {"type": "boolean", "default": False, "title": "Affiner le prompt (Qwen3-VL du template officiel)",
                          "x-advanced": True, "x-engine": "krea2"},
        # anciens graphes : node combinée en opération « edit » (convertie en « Éditer une image » à la prochaine modification).
        # Ces défauts doivent rester identiques à l'ancien schéma, sinon leurs résultats d'édition passeraient « à recalculer ».
        "steps": {"type": "integer", "minimum": 4, "maximum": 30, "default": 10, "title": "Steps (édition)", "x-hidden": True},
        "ref_boost": {"type": "number", "minimum": 0, "maximum": 10, "default": 4.0, "title": "ref_boost", "x-hidden": True},
        "grounding_px": {"type": "integer", "minimum": 384, "maximum": 1024, "default": 768, "title": "grounding_px", "x-hidden": True},
        "num_variants": {"type": "integer", "minimum": 1, "maximum": 1, "default": 1, "title": "Variantes par item",
                         "description": "V1 : une variante par item (5 prompts → 5 résultats)."},
        "postprocess": {"type": "object", "title": "Post-traitement", "properties": {
            "remove_background": {"type": "boolean", "default": False, "title": "Retirer le fond (vrai canal alpha)"}}},
    }),
    steps=_image_generate_steps,
    actions=["regenerate", "rerun_item", "select_variant"],
    execution_modes=EXECUTION_MODES,
))

register(NodeType(
    type="image.edit", label="Éditer une image", category="Image", kind="generative",
    description="Image + consigne → image modifiée : Krea 2 Identity Edit (local) ou OpenAI / ChatGPT Images (service externe : "
                "l'image est envoyée à OpenAI). Modes unique ou pour chaque prompt (même image, une consigne par item).",
    inputs=list(PROMPT_INPUTS) + [
        Port("image", "Image", "Image à éditer", required=True, description="Image source (scène / sujet principal)."),
        Port("image_b", "Image", "Seconde référence",
             description="OpenAI : image supplémentaire jointe à la demande. Non exposée avec Krea 2 en V1."),
    ],
    outputs=list(IMAGE_OUTPUTS),
    params_schema=_schema({
        "operation": {"type": "string", "enum": ["edit"], "default": "edit", "title": "Opération", "x-hidden": True},
        "engine": {"type": "string", "enum": ["krea2", "openai"], "default": "krea2", "title": "Moteur",
                   "enumLabels": {"krea2": "Krea 2 (local, sur ce PC)", "openai": "OpenAI / ChatGPT Images (service externe)"}},
        "openai_model": {"type": "string", "default": "", "title": "Modèle OpenAI", "x-engine": "openai", "x-openai-models": True,
                         "description": "Vide : modèle par défaut choisi dans Réglages → OpenAI."},
        "quality": OPENAI_QUALITY,
        "background": {"type": "string", "enum": ["auto", "opaque", "transparent"], "default": "auto", "title": "Fond",
                       "x-engine": "openai",
                       "enumLabels": {"auto": "automatique", "opaque": "opaque", "transparent": "transparent (PNG avec alpha)"}},
        "moderation": OPENAI_MODERATION,
        "workflow_id": {"type": "string", "title": "Workflow (alias)", "default": "", "x-engine": "krea2", "x-advanced": True},
        "seed": SEED,
        "width": {"type": "integer", "minimum": 256, "maximum": 3840, "multipleOf": 16, "default": 1024, "title": "Largeur"},
        "height": {"type": "integer", "minimum": 256, "maximum": 3840, "multipleOf": 16, "default": 1024, "title": "Hauteur"},
        "steps": {"type": "integer", "minimum": 4, "maximum": 30, "title": "Steps (édition)", "default": 10, "x-advanced": True,
                  "x-engine": "krea2"},
        "ref_boost": {"type": "number", "minimum": 0, "maximum": 10, "default": 4.0, "title": "Fidélité à la référence (ref_boost)",
                      "x-advanced": True, "x-engine": "krea2"},
        "grounding_px": {"type": "integer", "minimum": 384, "maximum": 1024, "default": 768, "title": "grounding_px", "x-advanced": True,
                         "x-engine": "krea2"},
        "num_variants": {"type": "integer", "minimum": 1, "maximum": 1, "default": 1, "title": "Variantes par item",
                         "description": "V1 : une variante par item (5 prompts → 5 résultats)."},
        "postprocess": {"type": "object", "title": "Post-traitement", "properties": {
            "remove_background": {"type": "boolean", "default": False, "title": "Retirer le fond (vrai canal alpha)"}}},
    }),
    steps=_image_generate_steps,
    actions=["regenerate", "rerun_item", "select_variant"],
    execution_modes=EXECUTION_MODES,
))

register(NodeType(
    type="image.prepare", label="Préparer l'image", category="Image", kind="deterministic",
    description="Pose A ou T (édition IA, facultative), détourage BiRefNet recalculable (vrai PNG RGBA) et cadrage sur le sujet, "
                "avec avertissements.",
    inputs=[Port("image", "Image", "Image", required=True)],
    outputs=[Port("image", "Image", "Image préparée"), Port("mask", "Mask", "Masque")],
    params_schema=_schema({
        "pose": {"type": "string", "enum": ["none", "a_pose", "t_pose"], "default": "none", "title": "Pose",
                 "enumLabels": {"none": "inchangée", "a_pose": "A-pose (bras à 45°)", "t_pose": "T-pose (bras à l'horizontale)"},
                 "description": "Redessine le personnage dans la pose choisie (édition IA : nouvelle image, à vérifier visuellement)."},
        "engine": {"type": "string", "enum": ["krea2", "openai"], "default": "krea2", "title": "Moteur de la pose",
                   "enumLabels": {"krea2": "Krea 2 (local, sur ce PC)", "openai": "OpenAI / ChatGPT Images (service externe)"},
                   "x-show-if": {"pose": ["a_pose", "t_pose"]}},
        "pose_details": {"type": "string", "default": "", "title": "Précisions pour la pose", "format": "textarea",
                         "description": "Ajoutées à la consigne (ex. « mains ouvertes, regard face caméra »).",
                         "x-show-if": {"pose": ["a_pose", "t_pose"]}},
        "remove_background": {"type": "boolean", "default": True, "title": "Retirer le fond (vrai canal alpha)"},
        "crop_to_subject": {"type": "boolean", "default": True, "title": "Recadrer sur le sujet",
                            "description": "Utilise le canal alpha : sans détourage, l'image est conservée telle quelle."},
        "padding": {"type": "number", "minimum": 0, "maximum": 0.5, "default": 0.08, "title": "Marge"},
        "seed": {**SEED, "x-show-if": {"pose": ["a_pose", "t_pose"]}},
        "ref_boost": {"type": "number", "minimum": 0, "maximum": 10, "default": 2.0, "title": "Fidélité au personnage (ref_boost)",
                      "x-advanced": True, "x-engine": "krea2", "x-show-if": {"pose": ["a_pose", "t_pose"]},
                      "description": "Plus haut : identité mieux conservée, mais la pose change moins."},
        "openai_model": {"type": "string", "default": "", "title": "Modèle OpenAI", "x-engine": "openai", "x-openai-models": True,
                         "x-show-if": {"pose": ["a_pose", "t_pose"]}},
        "quality": {**OPENAI_QUALITY, "x-show-if": {"pose": ["a_pose", "t_pose"]}},
        "moderation": {**OPENAI_MODERATION, "x-show-if": {"pose": ["a_pose", "t_pose"]}},
    }),
    steps=_image_prepare_steps,
    actions=["regenerate"],
))

register(NodeType(
    type="image.props", label="Extraction de props", category="Image", kind="deterministic",
    description="Envoie l'image à ChatGPT (ou Qwen3-VL en local) qui décrit le personnage et liste ce qu'on peut en extraire "
                "(tête, corps, masque, cape, ceinture, bottes…). Chaque partie cochée devient une image isolée dans le groupe « Props » "
                "(une édition par partie, avec sa preview).",
    inputs=[Port("image", "Image", "Image du personnage", required=True)],
    outputs=[Port("prompts", "PromptList", "Consignes d'extraction", description="Une consigne par partie cochée (un item par partie)."),
             Port("image", "Image", "Image source", description="L'image d'entrée, transmise telle quelle.")],
    params_schema=_schema({
        "include": {"type": "string", "enum": ["props", "props_body"], "default": "props_body", "title": "Inclure",
                    "enumLabels": {"props": "Props seuls", "props_body": "Props + corps (tête, corps)"}},
        "detail": {"type": "string", "enum": ["minimal", "standard", "detailed"], "default": "standard", "title": "Détail",
                   "enumLabels": {"minimal": "Minimal (3-6)", "standard": "Standard (6-15)", "detailed": "Détaillé (15+)"}},
        "merge_symmetric": {"type": "boolean", "default": True, "title": "Fusionner les pièces symétriques",
                            "description": "Une seule botte, un seul gant… (l'autre côté est son miroir)."},
        "engine": {"type": "string", "enum": ["openai", "local", "claude"], "default": "openai", "title": "Analyse par",
                   "enumLabels": {"openai": "OpenAI / ChatGPT (service externe)", "local": "Qwen3-VL 4B (local, sur ce PC)",
                                  "claude": "Claude / Anthropic (service externe)"}},
        "openai_model": {"type": "string", "default": "", "title": "Modèle OpenAI (vision)", "x-engine": "openai", "x-openai-text-models": True,
                         "description": "Vide : modèle texte / vision par défaut des Réglages → OpenAI."},
        "claude_model": {"type": "string", "default": "", "title": "Modèle Claude (vision)", "x-engine": "claude", "x-claude-models": True,
                         "description": "Vide : modèle par défaut des Réglages → Claude."},
        "character": {"type": "string", "default": "", "title": "Personnage (description)", "format": "textarea", "x-advanced": True,
                      "description": "Écrite par l'analyse ; modifiable. Reprise dans chaque consigne d'extraction."},
        "extra": {"type": "string", "default": "", "title": "Consigne ajoutée à chaque extraction", "format": "textarea", "x-advanced": True},
        "parts": {"type": "array", "default": [], "title": "Parties", "x-hidden": True, "items": {"type": "object"},
                  "description": "Écrites par l'analyse puis modifiables dans la node : key, name, kind (body|prop), symmetric, description, "
                                 "enabled, manual."},
        "seed": {**SEED, "x-show-if": {"engine": ["local"]}},
    }),
    steps=_props_steps,
    actions=["analyze", "extract"],
))

register(NodeType(
    type="image.upscale", label="Upscale image (2K / 4K)", category="Image", kind="deterministic",
    description="Agrandit une image de référence : SeedVR2 3B (restauration IA, détails nets) ou Real-ESRGAN x4 (rapide). "
                "Le canal alpha d'une image détourée est conservé.",
    inputs=[Port("image", "Image", "Image", required=True)],
    outputs=[Port("image", "Image", "Image agrandie")],
    params_schema=_schema({
        "resolution": {"type": "string", "enum": ["2k", "4k"], "default": "2k", "title": "Taille cible",
                       "enumLabels": {"2k": "2K — 2048 px (côté le plus long)", "4k": "4K — 4096 px (côté le plus long)"}},
        "engine": {"type": "string", "enum": ["seedvr2", "esrgan"], "default": "seedvr2", "title": "Moteur",
                   "enumLabels": {"seedvr2": "Détaillé — SeedVR2 3B (IA, local)", "esrgan": "Rapide — Real-ESRGAN x4 (local)"}},
        "color_correction": {"type": "string", "enum": ["lab", "wavelet", "adain", "none"], "default": "lab",
                             "title": "Correction des couleurs (SeedVR2)", "x-advanced": True, "x-show-if": {"engine": ["seedvr2"]},
                             "enumLabels": {"lab": "lab — la plus fidèle", "wavelet": "wavelet", "adain": "adain (rapide)",
                                            "none": "aucune (valeur du template)"}},
        "seed": {**SEED, "x-show-if": {"engine": ["seedvr2"]}},
    }),
    steps=_image_upscale_steps,
    actions=["regenerate"],
))

register(NodeType(
    type="image.preview", label="Preview image", category="Image", kind="preview",
    description="Zoom, comparaison, sélection de variante. La sortie transmet la variante sélectionnée, sans calcul.",
    inputs=[Port("image", "Image", "Image", required=True)],
    outputs=[Port("image", "Image", "Image sélectionnée")],
    params_schema=_schema({}),
    actions=["refresh", "regenerate_item", "select_variant", "download"],
))

register(NodeType(
    type="mesh.generate", label="Image → 3D", category="3D", kind="generative",
    description="Génère un vrai maillage texturé (Pixal3D natif ComfyUI, template officiel) ; seed et versions tracées.",
    inputs=[Port("image", "Image", "Image", required=True)],
    outputs=[Port("mesh", "Mesh", "Maillage")],
    params_schema=_schema({
        "category": {"type": "string", "enum": ["humanoid", "static", "articulated"], "default": "humanoid", "title": "Catégorie",
                     "enumLabels": {"humanoid": "Personnage humanoïde", "static": "Objet statique", "articulated": "Objet articulé"}},
        "engine": {"type": "string", "enum": ["pixal3d", "trellis2"], "default": "pixal3d", "title": "Moteur 3D",
                   "enumLabels": {"pixal3d": "Pixal3D (recommandé : ≈ 4 min sur 8 Go)",
                                  "trellis2": "TRELLIS.2 (très lent sur 8 Go : plus de 3 h constatées)"}},
        "seed": SEED,
        "quality": {"type": "string", "enum": ["preview", "standard", "detailed"], "default": "standard", "title": "Profil",
                    "enumLabels": {"preview": "Aperçu", "standard": "Standard", "detailed": "Détaillé"}},
        "texture_size": {"type": "integer", "enum": [1024, 2048, 4096], "default": 2048, "title": "Taille des textures"},
        "decimate_target": {"type": "integer", "minimum": 5000, "maximum": 2000000, "default": 300000, "title": "Faces après décimation (moteur)",
                            "x-advanced": True},
        "remove_background": {"type": "boolean", "default": True, "title": "Détourer l'image (BiRefNet du template)", "x-advanced": True},
    }),
    steps=lambda node: [Step("generate", "Génération 3D + textures + aperçus", "mesh.generate", "gpu", generative=True)],
    actions=["regenerate"],
))

register(NodeType(
    type="mesh.import", label="Importer un maillage", category="3D", kind="import",
    description="GLB/GLTF/OBJ/FBX pour riggage sans nouvelle génération.",
    inputs=[], outputs=[Port("mesh", "Mesh", "Maillage")],
    params_schema=_schema({"upload_id": {"type": "string", "title": "Fichier importé"}}),
    actions=["replace"],
))

register(NodeType(
    type="mesh.prepare", label="Préparer le maillage", category="3D", kind="deterministic",
    description="Macro dépliable : diagnostic/réparation, simplification, UV et baking (chaque sous-étape relançable).",
    inputs=[Port("mesh", "Mesh", "Maillage", required=True)],
    outputs=[Port("mesh", "Mesh", "Maillage préparé"), Port("report", "Report", "Rapport")],
    params_schema=_schema({
        "profile": {"type": "string", "enum": ["preview", "standard", "detailed"], "default": "standard", "title": "Profil"},
        "triangle_budget": {"type": "integer", "minimum": 1000, "maximum": 1000000, "default": 40000, "title": "Budget triangles"},
        "simplify_mode": {"type": "string", "enum": ["decimate", "quadriflow"], "default": "decimate", "title": "Simplification",
                          "enumLabels": {"decimate": "Décimation (triangles)", "quadriflow": "Retopologie quads QuadriFlow (Blender)"}},
        "texture_size": {"type": "integer", "enum": [1024, 2048, 4096], "default": 2048, "title": "Taille des textures"},
        "repair": {"type": "boolean", "default": True, "title": "Réparations limitées (doublons, normales)"},
        "keep_parts_separate": {"type": "boolean", "default": True, "title": "Conserver les pièces séparées"},
    }),
    steps=lambda node: [Step("repair", "Diagnostic et réparation", "blender", "cpu", params=("repair", "keep_parts_separate")),
                        Step("simplify", "Simplification / retopologie", "blender", "cpu", params=("triangle_budget", "simplify_mode", "profile")),
                        Step("bake", "UV et baking haute → basse (Cycles GPU)", "blender", "gpu", params=("texture_size", "profile"))],
))

register(NodeType(
    type="mesh.rig", label="Rig + skin", category="3D", kind="deterministic",
    description="Macro : placement du squelette et poids de skinning, puis assemblage Blender vérifié.",
    inputs=[Port("mesh", "Mesh", "Maillage", required=True)],
    outputs=[Port("rigged_mesh", "RiggedMesh", "Maillage riggé"), Port("report", "Report", "Rapport")],
    params_schema=_schema({
        "profile": {"type": "string", "enum": ["humanoid", "articulated"], "default": "humanoid", "title": "Profil",
                    "enumLabels": {"humanoid": "Humanoïde", "articulated": "Objet articulé (pivots)"}},
        "engine": {"type": "string", "enum": ["mia", "blender_auto"], "default": "mia", "title": "Moteur",
                   "enumLabels": {"mia": "Make-It-Animatable (squelette + poids)",
                                  "blender_auto": "Squelette MIA + poids automatiques Blender (« rig automatique à vérifier »)"}},
        "no_fingers": {"type": "boolean", "default": False, "title": "Sans doigts (mains en un bloc)"},
        "input_normal": {"type": "boolean", "default": True, "title": "Affiner les poids avec les normales"},
        "joint_overrides": {"type": "object", "title": "Corrections d'articulations (mode guidé)", "x-advanced": True,
                            "description": "{nom_os: [x,y,z]} en mètres, repère Blender ; recalcul des poids."},
        "pivots": {"type": "array", "title": "Pivots (objet articulé)", "x-advanced": True,
                   "items": {"type": "object"}},
    }),
    steps=_mesh_rig_steps,
))

register(NodeType(
    type="mesh.validate", label="Vérifier la déformation", category="3D", kind="deterministic",
    description="Poses de test procédurales, mesures de déformation, images et aperçu animé ; rest pose restaurée.",
    inputs=[Port("rigged_mesh", "RiggedMesh", "Maillage riggé", required=True)],
    outputs=[Port("report", "Report", "Rapport"), Port("rigged_mesh", "RiggedMesh", "Maillage riggé (inchangé)")],
    params_schema=_schema({"amplitude": {"type": "number", "minimum": 0.2, "maximum": 1.5, "default": 1.0, "title": "Amplitude des poses"}}),
    steps=lambda node: [Step("deform_tests", "Tests de déformation", "blender", "cpu")],
))

def _animate_steps(node: dict) -> list[Step]:
    # le mouvement ne dépend que du prompt / de la vidéo : changer de modèle ne relance que le transfert
    return [Step("motion", "Mouvement (Kimodo : prompt / MediaPipe : vidéo)", "motion.generate", "gpu", generative=True,
                 params=("source", "duration_s", "diffusion_steps", "smoothing", "start_s", "end_s"), inputs=("prompt", "video")),
            Step("retarget", "Transfert sur le modèle (Blender)", "blender", "cpu", params=("in_place",), inputs=("rigged_mesh",))]


register(NodeType(
    type="mesh.animate", label="Animer le modèle 3D", category="Animation", kind="generative",
    description="Anime un modèle riggé (squelette Mixamo du rig MIA) à partir d'un prompt de mouvement (Kimodo, local) ou d'une "
                "vidéo de référence (MediaPipe Pose, local). Sorties : modèle animé (.glb, .blend, .fbx) et aperçu vidéo.",
    inputs=[Port("rigged_mesh", "Mesh", "Modèle riggé", required=True,
                 description="Sortie de « Rigging » / « Vérifier la déformation », ou modèle importé (.glb, .fbx) avec un squelette Mixamo."),
            Port("prompt", "Prompt", "Prompt de mouvement", description="En anglais de préférence ; une ligne par mouvement pour les enchaîner."),
            Port("video", "Video", "Vidéo de référence", description="Une personne entière, caméra fixe de préférence.")],
    outputs=[Port("rigged_mesh", "RiggedMesh", "Modèle animé"), Port("video", "Video", "Aperçu animé"),
             Port("motion", "Animation", "Mouvement")],
    params_schema=_schema({
        "source": {"type": "string", "enum": ["auto", "prompt", "video"], "default": "auto", "title": "Source du mouvement",
                   "enumLabels": {"auto": "automatique (l'entrée branchée)", "prompt": "prompt (Kimodo)", "video": "vidéo (MediaPipe)"}},
        "duration_s": {"type": "number", "minimum": 1, "maximum": 20, "default": 4, "title": "Durée (s, prompt)",
                       "description": "Kimodo : 30 images/s ; jusqu'à 10 s par ligne de prompt."},
        "in_place": {"type": "boolean", "default": False, "title": "Sur place (sans déplacement au sol)"},
        "smoothing": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5, "title": "Lissage (vidéo)",
                      "description": "Réduit les tremblements de la capture ; trop élevé = mouvements amortis."},
        "diffusion_steps": {"type": "integer", "minimum": 20, "maximum": 200, "default": 100, "title": "Étapes de diffusion (Kimodo)", "x-advanced": True},
        "start_s": {"type": "number", "minimum": 0, "default": 0, "title": "Début de la vidéo (s)", "x-advanced": True},
        "end_s": {"type": "number", "minimum": 0, "default": 0, "title": "Fin de la vidéo (s, 0 = jusqu'au bout)", "x-advanced": True},
        "seed": SEED,
    }),
    steps=_animate_steps,
    actions=["regenerate", "select_variant"],
))

register(NodeType(
    type="mesh.preview", label="Preview 3D", category="3D", kind="preview",
    description="Maillage, textures, filaire, os et poses ; inspecte tout artefact intermédiaire.",
    inputs=[Port("mesh", "Mesh", "Maillage", required=True)],
    outputs=[Port("mesh", "Mesh", "Maillage sélectionné")],
    params_schema=_schema({"show_skeleton": {"type": "boolean", "default": True, "title": "Afficher les os"}}),
    actions=["refresh", "select_variant"],
))

register(NodeType(
    type="asset.export", label="Export Blender", category="3D", kind="export",
    description=".blend autonome (textures intégrées), .glb, .fbx, textures et metadata.json ; réouverture vérifiée.",
    inputs=[Port("asset", "Mesh", "Asset (maillage ou maillage riggé)", required=True)],
    outputs=[Port("files", "Files", "Fichiers"), Port("report", "Report", "Rapport d'export")],
    params_schema=_schema({
        "name": {"type": "string", "default": "Asset", "title": "Nom"},
        "formats": {"type": "array", "items": {"type": "string", "enum": ["blend", "glb", "fbx"]}, "default": ["blend", "glb", "fbx"],
                    "title": "Formats"},
        "target_height_m": {"type": "number", "minimum": 0, "maximum": 100, "default": 1.8, "title": "Hauteur souhaitée (m, 0 = inchangée)"},
    }),
    steps=lambda node: [Step("export", "Export et vérification", "blender", "cpu")],
    actions=["reexport", "open_in_blender", "open_folder", "download"],
))

register(NodeType(
    type="reference.collection", label="Références", category="Vidéo", kind="collection",
    description="Slots nommés (rôle, sujet, description) ; l'ordre est conservé jusqu'au prompt réel.",
    inputs=[Port("image:", "Image", "Image d'un slot", dynamic_prefix="image:")],
    outputs=[Port("references", "ReferenceCollection", "Références")],
    params_schema=_schema({"slots": {"type": "array", "title": "Slots", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "role": {"type": "string", "enum": ["subject", "identity", "decor", "style", "movement", "camera"]},
        "label": {"type": "string"}, "description": {"type": "string"}}}}}),
))

register(NodeType(
    type="video.generate", label="Générer vidéo · MiniMax H3 Ultra", category="Vidéo", kind="generative",
    description="Réutilise votre accès MiniMax H3 existant (workflow importé). Modes selon les capacités réelles du workflow.",
    inputs=[
        Port("prompt", "Prompt", "Prompt", required=True),
        Port("image", "Image", "Première image"),
        Port("last_image", "Image", "Dernière image"),
        Port("references", "ReferenceCollection", "Références"),
    ],
    outputs=[Port("video", "Video", "Vidéo")],
    params_schema=_schema({
        "workflow_id": {"type": "string", "default": "minimax_user", "title": "Workflow (alias)"},
        "mode": {"type": "string", "enum": ["text_to_video", "image_to_video", "first_last_to_video", "reference_to_video"],
                 "default": "image_to_video", "title": "Mode",
                 "enumLabels": {"text_to_video": "Texte → vidéo", "image_to_video": "Image → vidéo",
                                "first_last_to_video": "Début/fin → vidéo", "reference_to_video": "Références → vidéo"}},
        "duration_s": {"type": "number", "minimum": 1, "maximum": 15, "default": 5, "title": "Durée (s, arrondie à la grille 17k+5 à 24 i/s)"},
        "width": {"type": "integer", "minimum": 256, "maximum": 1920, "multipleOf": 32, "default": 480, "title": "Largeur"},
        "height": {"type": "integer", "minimum": 256, "maximum": 1920, "multipleOf": 32, "default": 864, "title": "Hauteur"},
        "seed": SEED,
        "low_memory": {"type": "boolean", "default": True, "title": "Profil mémoire réduite (lancement par Atelier 3D)", "x-advanced": True},
        "apply_prompt_guide": {"type": "boolean", "default": True, "title": "Appliquer le guide de prompt de votre workflow (ancre <Picture 1>)", "x-advanced": True},
        "text_encoder": {"type": "string", "enum": ["", "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"],
                         "default": "", "title": "Encodeur texte (vide = celui de votre workflow)", "x-advanced": True},
    }),
    steps=lambda node: [Step("generate", "Génération vidéo + miniature et frames", "video.minimax", "gpu", generative=True)],
    actions=["regenerate"],
))

register(NodeType(
    type="compare.before_after", label="Avant / après", category="Comparer", kind="preview",
    description="Compare deux résultats : images, vidéos (lecture synchronisée) ou modèles 3D (rendus, et vue 3D côte à côte à "
                "caméras liées). Curseur, côte à côte ou différence. Aucun calcul.",
    inputs=[Port("before", "Media", "Avant", description="Image, vidéo, maillage ou maillage riggé."),
            Port("after", "Media", "Après", description="Image, vidéo, maillage ou maillage riggé.")],
    outputs=[],
    params_schema=_schema({
        "mode": {"type": "string", "enum": ["slider", "side_by_side", "difference"], "default": "slider", "title": "Affichage",
                 "enumLabels": {"slider": "Curseur", "side_by_side": "Côte à côte", "difference": "Différence"}},
        "label_before": {"type": "string", "default": "Avant", "title": "Libellé de gauche"},
        "label_after": {"type": "string", "default": "Après", "title": "Libellé de droite"},
    }),
    actions=["compare"],
))

register(NodeType(
    type="video.import", label="Importer une vidéo", category="Vidéo", kind="import",
    description="Fichier MP4 / WebM / MOV (disque ou médiathèque) ; l'original est conservé. Pour comparer ou prévisualiser.",
    inputs=[], outputs=[Port("video", "Video", "Vidéo")],
    params_schema=_schema({"upload_id": {"type": "string", "title": "Fichier importé"}}),
    actions=["replace"],
))

register(NodeType(
    type="video.preview", label="Preview vidéo", category="Vidéo", kind="preview",
    description="Lecture réelle, timeline, frames horodatées et comparaison de clips.",
    inputs=[Port("video", "Video", "Vidéo", required=True)],
    outputs=[Port("video", "Video", "Vidéo sélectionnée")],
    params_schema=_schema({}),
    actions=["refresh", "select_variant"],
))


def get(type_: str) -> NodeType:
    if type_ not in REGISTRY:
        raise KeyError(f"type de node inconnu : {type_}")
    return REGISTRY[type_]


def input_port(nt: NodeType, name: str) -> Port | None:
    for p in nt.inputs:
        if p.name == name:
            return p
        if p.dynamic_prefix and name.startswith(p.dynamic_prefix):
            return Port(name, p.type, p.label, p.multi, p.required)
    return None


def output_port(nt: NodeType, name: str) -> Port | None:
    for p in nt.outputs:
        if p.name == name:
            return p
        if p.dynamic_prefix and name.startswith(p.dynamic_prefix):
            return Port(name, p.type, p.label)
    return None


def defaults_for(nt: NodeType) -> dict:
    out = {}
    for k, v in nt.params_schema.get("properties", {}).items():
        if "default" in v:
            out[k] = copy.deepcopy(v["default"])
        elif v.get("type") == "object" and "properties" in v:
            sub = {kk: vv["default"] for kk, vv in v["properties"].items() if "default" in vv}
            if sub:
                out[k] = sub
    return out


def effective_params(node: dict) -> dict:
    nt = get(node["type"])
    p = defaults_for(nt)
    for k, v in (node.get("params") or {}).items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


def catalog() -> list[dict]:
    return [nt.to_json() for nt in REGISTRY.values()]
