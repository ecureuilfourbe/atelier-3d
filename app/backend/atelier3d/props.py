"""Extraction de props : analyse d'une image de personnage (liste des parties extractibles) et consignes d'extraction.

L'analyse (OpenAI vision ou Qwen3-VL local) produit {character, parts:[{key, name, kind, symmetric, description}]}. Les
parties sont ensuite écrites dans les réglages de la node : l'utilisateur les active, renomme, supprime ou en ajoute ; chaque
partie active devient un item du groupe d'extraction (une image par partie, clé d'item = clé de la partie).
"""
from __future__ import annotations

import json
import re
import unicodedata

DETAIL_RANGES = {"minimal": (3, 6), "standard": (6, 15), "detailed": (15, 30)}

# Sortie structurée OpenAI (mode strict : tous les champs requis, aucun champ supplémentaire)
OPENAI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["character", "parts"],
    "properties": {
        "character": {"type": "string"},
        "parts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "kind", "symmetric", "description"],
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["body", "prop"]},
                "symmetric": {"type": "boolean"},
                "description": {"type": "string"},
            }}},
    },
}


def slug(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "partie"


def analysis_instruction(p: dict, *, structured: bool) -> str:
    lo, hi = DETAIL_RANGES.get(p.get("detail") or "standard", (6, 15))
    count = f"between {lo} and {hi}" if p.get("detail") != "detailed" else f"at least {lo}"
    body = (p.get("include") or "props_body") == "props_body"
    lines = [
        "You prepare a 3D modeling breakdown of the character shown in this image.",
        "First describe the character in one short sentence (who or what it is, style, outfit).",
        f"Then list {count} parts that a 3D artist could model separately from this image: clothing pieces, armor pieces, "
        "accessories, weapons, bags, jewelry, masks, hats, shoes"
        + (", plus the body parts \"head\" and \"body\" (the bare base body without the costume)." if body else
           ". Do not list body parts (no head, hands or body): only wearable or held items."),
        "Only list parts that are really visible in the image. Use short names in English (1 to 3 words), from head to feet.",
    ]
    if p.get("merge_symmetric", True):
        lines.append("Merge symmetrical pairs into ONE part named after a single side (for example \"right boot\" for a pair of boots) "
                     "and mark it symmetric.")
    else:
        lines.append("List left and right items separately (for example \"left boot\" and \"right boot\").")
    if structured:
        lines.append("For each part give: name, kind (\"body\" or \"prop\"), symmetric (true/false) and a description of 5 to 15 "
                     "words (shape, colors, materials).")
    else:
        lines += ["Answer in exactly this format, with no other text:",
                  "CHARACTER: <one sentence>",
                  "1. <name> | <body or prop> | <symmetric: yes or no> | <description of 5 to 15 words>",
                  "2. ..."]
    return "\n".join(lines)


def parse_local(text: str) -> dict:
    """Réponse de Qwen3-VL (format ligne par ligne). Tolère les variantes (puces, gras, champs manquants)."""
    s = (text or "").replace("**", "")
    character = ""
    m = re.search(r"CHARACTER\s*:\s*(.+)", s, re.I)
    if m:
        character = m.group(1).strip()
    parts = []
    for line in s.splitlines():
        mm = re.match(r"^\s*(?:\d{1,2}\s*[.)]|[-*•])\s*(.+)$", line)
        if not mm:
            continue
        fields = [f.strip().strip('"') for f in mm.group(1).split("|")]
        name = re.sub(r"\s*\(.*?\)\s*$", "", fields[0]).strip()
        if not name or len(name) > 40:
            continue
        kind = "body" if len(fields) > 1 and fields[1].lower().startswith("body") else "prop"
        sym = len(fields) > 2 and fields[2].lower().replace("symmetric:", "").strip().startswith(("yes", "true", "oui"))
        desc = fields[3] if len(fields) > 3 else (fields[-1] if len(fields) > 1 and len(fields[-1].split()) > 3 else "")
        parts.append({"name": name, "kind": kind, "symmetric": bool(sym), "description": desc})
    return {"character": character, "parts": parts}


BODY_KEYS = {"head", "body", "base_body", "bare_body", "torso"}
# parties anatomiques isolées : couvertes par « body » (constat : Qwen3-VL 4B liste bras et jambes séparément)
ANATOMY = re.compile(r"^(?:(?:left|right)_)?(?:arm|arms|leg|legs|hand|hands|foot|feet|forearm|thigh|neck|face|torso)$")
SIDE = re.compile(r"^(left|right)[ _]+(.+)$", re.I)


def normalize(result: dict, p: dict) -> dict:
    """Clés stables (slug du nom), catégorie déterministe (seules « head » et « body » sont du corps), doublons retirés,
    paires gauche/droite fusionnées si demandé, parties du corps retirées si « props seuls »."""
    body = (p.get("include") or "props_body") == "props_body"
    merge = p.get("merge_symmetric", True)
    seen, parts = set(), []
    for x in result.get("parts") or []:
        name = re.sub(r"\s+", " ", str(x.get("name") or "")).strip()
        key = slug(name)
        if not name or ANATOMY.match(key):
            continue
        kind = "body" if key in BODY_KEYS else "prop"
        if kind == "body" and not body:
            continue
        if key in seen:
            continue
        seen.add(key)
        parts.append({"key": key, "name": name, "kind": kind, "symmetric": bool(x.get("symmetric")),
                      "description": str(x.get("description") or "").strip(), "enabled": True})
    if merge:  # « left boot » + « right boot » → une seule pièce « right boot », symétrique
        sides: dict[str, dict[str, dict]] = {}
        for x in parts:
            m = SIDE.match(x["name"])
            if m:
                sides.setdefault(slug(m.group(2)), {})[m.group(1).lower()] = x
        drop = set()
        for pair in sides.values():
            if "left" in pair and "right" in pair:
                pair["right"]["symmetric"] = True
                drop.add(pair["left"]["key"])
        parts = [x for x in parts if x["key"] not in drop]
    return {"character": str(result.get("character") or "").strip(), "parts": parts}


def merge_parts(old: list[dict], new: list[dict]) -> list[dict]:
    """Nouvelle analyse : les parties déjà connues gardent l'état choisi par l'utilisateur (activée, nom) ; les parties
    ajoutées à la main sont conservées ; les parties qui ne sont plus détectées disparaissent."""
    prev = {x.get("key"): x for x in old or []}
    out = []
    for x in new:
        o = prev.get(x["key"])
        out.append({**x, "enabled": o.get("enabled", True), "name": o.get("name") or x["name"]} if o else x)
    keys = {x["key"] for x in out}
    out += [x for x in old or [] if x.get("manual") and x.get("key") not in keys]
    return out


GARMENTS = re.compile(r"(tunic|shirt|coat|jacket|cloak|cape|dress|robe|vest|pants|trousers|skirt|armou?r|breastplate|tabard|hoodie|"
                      r"sweater|top|outfit|costume|gown|kimono|poncho|tunic|jumpsuit|overalls|uniform|corset|blouse|shorts|leggings)")


def extraction_prompt(part: dict, character: str = "", extra: str = "") -> str:
    """Consigne d'édition d'image pour extraire UNE partie (anglais), selon les formulations qui ont fonctionné avec
    Krea 2 Identity Edit sur le flow PNJ (TEST_REPORT § 3) : portrait serré pour la tête, mannequin neutre pour un
    vêtement, « photo produit, sans personne » pour un objet. La description du personnage n'est reprise que pour le corps :
    citée dans la consigne d'un objet, elle pousse le modèle à redessiner tout le personnage (constaté le 07/10/2026)."""
    name = str(part.get("name") or part.get("key") or "item").strip()
    desc = str(part.get("description") or "").strip().rstrip(".")
    d = f" ({desc})" if desc else ""
    key = part.get("key") or slug(name)
    if key in ("body", "base_body", "bare_body"):
        who = f" ({character.rstrip('.')})" if character else ""
        text = (f"Show this same character{who} alone in the same pose, without any clothing, armor or accessories, wearing only a "
                "plain skin-tight neutral grey base suit, full body from head to feet, front view, plain light grey background, "
                "same proportions and body shape.")
    elif key in ("head", "face"):
        text = ("Close-up portrait of this same character: only the head and the top of the shoulders, filling the frame, front view. "
                "Keep exactly the same face, skin tone, hairstyle and headwear. Plain light grey background.")
    elif part.get("kind") == "body":
        text = (f"Close-up of only the {name} of this same character{d}, isolated, filling the frame, front view. Keep exactly the "
                "same proportions, colours and details. Plain light grey background.")
    elif GARMENTS.search(key):
        text = (f"Show only this same {name}{d} on a neutral blank grey mannequin in A-pose, full body, front view; the mannequin wears "
                f"nothing else. Keep exactly the same cut, layers, seams, colours and materials of the {name}. Plain light grey background.")
    else:
        one = " Show a single one (the other side is its mirror)." if part.get("symmetric") else ""
        text = (f"Product photo of this character's {name} only{d}: the {name} alone, no person, no body, front three-quarter view, "
                f"filling the frame, complete and undamaged. Keep exactly the same shape, colours, materials and details.{one} "
                "Plain light grey background.")
    extra = (extra or "").strip()
    return text + (f" {extra}" if extra else "")


def enabled_parts(params: dict) -> list[dict]:
    return [x for x in params.get("parts") or [] if x.get("enabled", True) and x.get("key")]


def dumps_parts(parts: list[dict]) -> str:
    return json.dumps(parts, ensure_ascii=False)


def analysis_of_variant(db, variant_id: str) -> dict | None:
    """Résultat d'analyse d'une variante (métadonnées de son artefact analysis.json, sans lecture de fichier)."""
    from .db import loads

    r = db.one("SELECT outputs FROM variants WHERE id = ?", (variant_id,))
    ids = (loads(r["outputs"], {}) or {}).get("analysis") if r else None
    if not ids:
        return None
    a = db.one("SELECT meta FROM artifacts WHERE id = ?", (ids[0],))
    return loads(a["meta"], {}) if a else None


def apply_analysis(db, graph_id: str, node_id: str, variant_id: str, author: str) -> dict | None:
    """Écrit les parties détectées dans les réglages de la node (en gardant les choix de l'utilisateur) : les items du
    groupe « Props » suivent à la sauvegarde. Nouvelle tentative si le graphe a été modifié entre-temps."""
    from .graph.model import GraphStore, RevisionConflict

    data = analysis_of_variant(db, variant_id)
    if not data or not graph_id:
        return None
    store = GraphStore(db)
    for _ in range(4):
        doc = store.get(graph_id)
        node = next((n for n in doc["nodes"] if n["id"] == node_id), None)
        if node is None:
            return None
        params = node.get("params") or {}
        parts = merge_parts(params.get("parts") or [], data.get("parts") or [])
        try:
            doc, _ = store.patch(graph_id, [{"op": "update_node", "node_id": node_id,
                                             "params": {"parts": parts, "character": data.get("character", "")}}],
                                 author=author, expected_revision=doc["revision"])
            return {"parts": len(parts), "enabled": len([x for x in parts if x.get("enabled", True)])}
        except RevisionConflict:
            continue
    return None
