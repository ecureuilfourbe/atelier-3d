"""Chargement de la configuration écrite par l'installateur (<home>/config/atelier3d.json)."""
from __future__ import annotations

import json
import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Any

_LOCK = threading.Lock()
_CACHE: "Config | None" = None


def _read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


@dataclass
class Config:
    home: pathlib.Path
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ chemins
    @property
    def dist(self) -> pathlib.Path:
        return pathlib.Path(self.raw["paths"]["dist"])

    @property
    def runtime(self) -> pathlib.Path:
        return pathlib.Path(self.raw["paths"]["runtime"])

    @property
    def data_dir(self) -> pathlib.Path:
        return self.home / "data"

    @property
    def models_dir(self) -> pathlib.Path:
        return self.home / "models"

    @property
    def logs_dir(self) -> pathlib.Path:
        return self.home / "logs"

    @property
    def run_dir(self) -> pathlib.Path:
        return self.home / "run"

    @property
    def config_dir(self) -> pathlib.Path:
        return self.home / "config"

    @property
    def db_path(self) -> pathlib.Path:
        return self.data_dir / "atelier3d.sqlite3"

    @property
    def jobs_dir(self) -> pathlib.Path:
        return self.data_dir / "jobs"

    @property
    def uploads_dir(self) -> pathlib.Path:
        return self.data_dir / "uploads"

    @property
    def workflows_dir(self) -> pathlib.Path:
        return self.dist / "app" / "workflows"

    @property
    def blender_scripts_dir(self) -> pathlib.Path:
        return self.dist / "app" / "blender_scripts"

    @property
    def frontend_dist(self) -> pathlib.Path:
        return self.dist / "app" / "frontend" / "dist"

    # ------------------------------------------------------------------ sections
    def section(self, name: str) -> dict:
        return self.raw.get(name) or {}

    @property
    def app_host(self) -> str:
        return self.section("app").get("host", "127.0.0.1")

    @property
    def app_port(self) -> int:
        return int(self.section("app").get("port", 8765))

    @property
    def components(self) -> list[str]:
        return list(self.raw.get("components") or [])

    def allowed_roots(self) -> list[pathlib.Path]:
        """Dossiers où l'API peut lire/écrire des fichiers (uploads depuis disque, téléchargements MCP)."""
        roots = [self.data_dir]
        for p in self.section("security").get("allowed_dirs", []):
            roots.append(pathlib.Path(p))
        return roots

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def save(self) -> None:
        path = self.config_dir / "atelier3d.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def find_home(explicit: str | None = None) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("ATELIER3D_HOME")
    if env:
        return pathlib.Path(env)
    here = pathlib.Path(__file__).resolve()
    for cand in [here.parents[3] / "install-location.json",
                 pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Atelier3D" / "install-location.json"]:
        if cand.is_file():
            return pathlib.Path(_read_json(cand)["home"])
    raise RuntimeError("Installation Atelier 3D introuvable : lancez INSTALL.bat ou définissez ATELIER3D_HOME.")


def load(home: str | None = None, reload: bool = False) -> Config:
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not reload and (home is None or pathlib.Path(home) == _CACHE.home):
            return _CACHE
        h = find_home(home)
        path = h / "config" / "atelier3d.json"
        raw = _read_json(path) if path.is_file() else {}
        _CACHE = Config(home=h, raw=raw)
        return _CACHE
