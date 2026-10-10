"""Secrets locaux (clés d'API) et réglages des services externes.

- Les clés sont chiffrées avec DPAPI (Windows, liées à la session de l'utilisateur) dans <installation>\\config\\secrets.dat :
  jamais en clair sur le disque, jamais renvoyées à l'interface ni écrites dans les journaux (seulement « …abcd »).
- Les réglages non secrets (modèles par défaut, qualité…) sont dans <installation>\\config\\services.json et relus à chaque
  usage : le serveur et le worker (processus séparés) voient toujours la même valeur.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import pathlib
import sys
import threading
from ctypes import wintypes
from typing import Any

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------- DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":  # hors Windows (tests) : encodage simple, signalé par le préfixe
        return data
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    entropy_raw = b"Atelier3D-secrets-v1"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = _Blob(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # même signature pour les deux ; 2ᵉ argument : description (chiffrement) ou sortie facultative (déchiffrement)
    ok = fn(ctypes.byref(blob_in), "Atelier3D" if protect else None, ctypes.byref(entropy), None, None, flags, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI : échec ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class SecretStore:
    def __init__(self, config_dir: pathlib.Path):
        self.path = pathlib.Path(config_dir) / "secrets.dat"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _write(self, d: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def set(self, name: str, value: str) -> None:
        with _LOCK:
            d = self._read()
            d[name] = base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")
            self._write(d)

    def get(self, name: str) -> str | None:
        raw = self._read().get(name)
        if not raw:
            return None
        try:
            return _dpapi(base64.b64decode(raw), False).decode("utf-8")
        except OSError:
            return None  # chiffré par une autre session Windows : illisible ici

    def delete(self, name: str) -> None:
        with _LOCK:
            d = self._read()
            if d.pop(name, None) is not None:
                self._write(d)

    def hint(self, name: str) -> str | None:
        v = self.get(name)
        return f"…{v[-4:]}" if v else None


# ---------------------------------------------------------------------------- réglages des services

OPENAI_KEY = "openai_api_key"
OPENAI_DEFAULTS: dict[str, Any] = {"model_generate": "", "model_edit": "", "model_text": "", "quality": "auto", "moderation": "auto",
                                   "models": [], "text_models": [], "last_test": None}


def services_path(config_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(config_dir) / "services.json"


def load_services(config_dir: pathlib.Path) -> dict:
    try:
        d = json.loads(services_path(config_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        d = {}
    d["openai"] = {**OPENAI_DEFAULTS, **(d.get("openai") or {})}
    return d


def save_services(config_dir: pathlib.Path, d: dict) -> None:
    p = services_path(config_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def openai_view(config_dir: pathlib.Path) -> dict:
    """Ce que l'interface peut voir : jamais la clé, seulement sa présence et ses 4 derniers caractères."""
    s = load_services(config_dir)["openai"]
    store = SecretStore(config_dir)
    return {**s, "configured": store.get(OPENAI_KEY) is not None, "key_hint": store.hint(OPENAI_KEY)}
