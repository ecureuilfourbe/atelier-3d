"""Templates de workflows ComfyUI au format API + bindings vérifiés.

Le graph.json du studio n'est jamais envoyé à /prompt : on clone le workflow.api.json validé et on modifie
UNIQUEMENT les entrées déclarées dans bindings.json, après vérification contre /object_info.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
from typing import Any


class WorkflowError(Exception):
    pass


class Template:
    def __init__(self, directory: pathlib.Path):
        self.dir = pathlib.Path(directory)
        self.id = self.dir.name
        self.api = json.loads((self.dir / "workflow.api.json").read_text(encoding="utf-8"))
        self.bindings = json.loads((self.dir / "bindings.json").read_text(encoding="utf-8"))
        self.sha256 = hashlib.sha256((self.dir / "workflow.api.json").read_bytes()).hexdigest()

    @property
    def label(self) -> str:
        return self.bindings.get("label", self.id)

    def fingerprint(self) -> dict:
        return {"workflow_id": self.id, "workflow_sha256": self.sha256, "engine": self.bindings.get("engine"),
                "required_models": self.bindings.get("required_models", [])}

    def verify(self, object_info: dict, model_lists: dict[str, list[str]]) -> list[str]:
        """Classes, noms d'inputs et modèles réellement présents. Retourne la liste des problèmes."""
        problems = []
        for nid, node in self.api.items():
            ct = node.get("class_type")
            if ct not in object_info:
                problems.append(f"classe {ct} (node {nid}) absente de ce ComfyUI")
                continue
            spec = object_info[ct].get("input", {})
            known = set((spec.get("required") or {}).keys()) | set((spec.get("optional") or {}).keys()) | set((spec.get("hidden") or {}).keys())
            if str(object_info[ct].get("python_module", "")).startswith("custom_nodes"):
                continue  # nodes personnalisées à entrées dynamiques (VHS, rgthree…) : la validation de /prompt fait foi
            for inp in node.get("inputs", {}):
                base = inp.split(".")[0]
                if known and base not in known and not any(k.startswith(base) for k in known):
                    problems.append(f"input « {inp} » inconnu pour {ct} (node {nid})")
        for m in self.bindings.get("required_models", []):
            folder = m["folder"].split("/")[0]
            name = m["name"] if "/" not in m["folder"] else m["folder"].split("/", 1)[1] + "/" + m["name"]
            lst = [x.replace("\\", "/") for x in model_lists.get(folder, [])]
            if name not in lst:
                problems.append(f"modèle manquant : {m['folder']}/{m['name']}")
        for key, b in self.bindings.get("inputs", {}).items():
            for bb in (b if isinstance(b, list) else [b]):
                if bb["node"] not in self.api:
                    problems.append(f"binding {key} : node {bb['node']} absente du workflow")
        return problems

    def instantiate(self, values: dict[str, Any]) -> dict:
        """Clone le workflow et applique les valeurs sur les seules entrées déclarées."""
        wf = copy.deepcopy(self.api)
        values = dict(values)
        for key, b in self.bindings.get("inputs", {}).items():  # valeurs par défaut documentées (ex. correction de nom de LoRA)
            if not isinstance(b, list) and "default" in b and values.get(key) is None:
                values[key] = copy.deepcopy(b["default"])
        for key, val in values.items():
            if val is None:
                continue
            b = self.bindings.get("inputs", {}).get(key)
            if b is None:
                raise WorkflowError(f"entrée « {key} » non exposée par le workflow {self.id}")
            for bb in (b if isinstance(b, list) else [b]):
                node = wf[bb["node"]]
                v = val + bb["offset"] if "offset" in bb and isinstance(val, int) else val
                # replaces_link : la valeur remplace une connexion ; adds_input : entrée absente de l'export (LoadImage sans fichier par défaut)
                if bb.get("input") not in node["inputs"] and not (bb.get("replaces_link") or bb.get("adds_input")):
                    raise WorkflowError(f"input {bb['input']} absent de la node {bb['node']}")
                node["inputs"][bb["input"]] = v
        return wf


def normalize_combo_paths(wf: dict, object_info: dict) -> list[str]:
    """Remplace une valeur de liste (modèle, LoRA…) écrite avec un autre séparateur de dossier (« / » vs « \\ »)
    par la valeur EXACTE annoncée par /object_info. Ne modifie rien d'autre ; retourne le journal des remplacements."""
    log = []
    for nid, node in wf.items():
        spec = (object_info.get(node.get("class_type")) or {}).get("input", {})
        allspec = {**(spec.get("required") or {}), **(spec.get("optional") or {})}
        for name, val in list(node.get("inputs", {}).items()):
            if not isinstance(val, str) or ("/" not in val and "\\" not in val):
                continue
            s = allspec.get(name)
            options = None
            if isinstance(s, list) and s and isinstance(s[0], list):
                options = s[0]
            elif isinstance(s, list) and s and s[0] == "COMBO" and len(s) > 1 and isinstance(s[1], dict):
                options = s[1].get("options")
            if not options or val in options:
                continue
            norm = val.replace("\\", "/")
            match = [o for o in options if isinstance(o, str) and o.replace("\\", "/") == norm]
            if len(match) == 1:
                node["inputs"][name] = match[0]
                log.append(f"node {nid} {name} : « {val} » → « {match[0]} » (séparateur de dossier Windows)")
    return log


class TemplateRegistry:
    def __init__(self, root: pathlib.Path):
        self.root = pathlib.Path(root)
        self._cache: dict[str, Template] = {}

    def ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "workflow.api.json").is_file() and (p / "bindings.json").is_file()) if self.root.is_dir() else []

    def get(self, wid: str) -> Template:
        if wid not in self._cache:
            d = self.root / wid
            if not (d / "workflow.api.json").is_file():
                raise WorkflowError(f"workflow {wid} introuvable")
            self._cache[wid] = Template(d)
        return self._cache[wid]

    def resolve(self, alias_or_id: str, aliases: dict[str, str]) -> Template:
        wid = aliases.get(alias_or_id, alias_or_id)
        if not wid:
            raise WorkflowError(f"alias {alias_or_id} non résolu")
        return self.get(wid)
