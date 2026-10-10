"""Accès à distance par VPN (WireGuard de la Freebox) : qui peut joindre le serveur, et comment il s'authentifie.

Par défaut le serveur n'écoute que sur 127.0.0.1. Quand l'accès à distance est activé (Réglages, sur le PC lui-même),
il écoute sur toutes les interfaces IPv4 MAIS ne sert que :
- le PC lui-même (127.0.0.1 / ::1), inchangé ;
- les adresses des réseaux autorisés (par défaut 192.168.27.64/27, les clients du serveur WireGuard de la Freebox),
  après saisie d'un code d'accès (cookie de session dédié, révoqué en changeant le code).
Les actions qui agissent sur l'écran du PC (ouvrir un dossier, ouvrir Blender), l'import par chemin disque et les
réglages d'accès restent réservés au PC lui-même.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import pathlib
import re
import secrets
import threading
import time

DEFAULT_NETWORKS = ["192.168.27.64/27"]  # adresses attribuées par le serveur WireGuard de la Freebox
CODE_FILE = "remote_access_code"
COOKIE = "a3d_remote"
SESSION_DAYS = 30
# chemins réservés au PC lui-même (même authentifié à distance)
HOST_ONLY = [
    re.compile(r"^/api/v1/artifacts/[^/]+/open-folder$"),
    re.compile(r"^/api/v1/jobs/[^/]+/open-in-blender$"),
    re.compile(r"^/api/v1/settings/remote(/.*)?$"),
]
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O, 1/I


# ---------------------------------------------------------------------------- configuration (relue à chaud)
_cache: dict = {"mtime": None, "data": None}
_lock = threading.Lock()


def remote_conf(config_path: pathlib.Path) -> dict:
    """Section security.remote de atelier3d.json, relue si le fichier change (activation sans redémarrage du contrôle)."""
    try:
        m = config_path.stat().st_mtime
    except OSError:
        m = None
    with _lock:
        if _cache["mtime"] != m or _cache["data"] is None:
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8")) if m else {}
            except (OSError, ValueError):
                raw = {}
            r = ((raw.get("security") or {}).get("remote") or {})
            _cache.update(mtime=m, data={"enabled": bool(r.get("enabled")), "networks": list(r.get("networks") or DEFAULT_NETWORKS),
                                         "hostnames": list(r.get("hostnames") or [])})
        return dict(_cache["data"])


def validate_networks(nets: list[str]) -> list[str]:
    """Réseaux privés IPv4 seulement, pas plus large qu'un /16 (jamais 0.0.0.0/0 ni Internet)."""
    out = []
    for s in nets:
        n = ipaddress.ip_network(str(s).strip(), strict=False)
        if n.version != 4 or not n.is_private or n.prefixlen < 16:
            raise ValueError(f"{s} : réseau privé IPv4 de /16 à /32 attendu")
        out.append(str(n))
    if not out:
        raise ValueError("au moins un réseau autorisé est nécessaire")
    return out


# ---------------------------------------------------------------------------- adresses et hôtes
def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def ip_allowed(ip: str, networks: list[str]) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for n in networks:
        try:
            if a in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def host_ok_remote(host: str, hostnames: list[str]) -> bool:
    """Anti DNS-rebinding : à distance, l'en-tête Host doit être une adresse IPv4 littérale (ou un nom déclaré)."""
    h = (host or "").split(":")[0].strip().lower()
    if h in {x.lower() for x in hostnames}:
        return True
    try:
        return ipaddress.ip_address(h).version == 4
    except ValueError:
        return False


def host_only(path: str) -> bool:
    return any(r.match(path) for r in HOST_ONLY)


# ---------------------------------------------------------------------------- code d'accès et session
def access_code(config_dir: pathlib.Path) -> str:
    p = pathlib.Path(config_dir) / CODE_FILE
    if p.is_file():
        c = p.read_text(encoding="utf-8").strip()
        if c:
            return c
    return regenerate_code(config_dir)


def regenerate_code(config_dir: pathlib.Path) -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    code = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
    p = pathlib.Path(config_dir) / CODE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code, encoding="utf-8")
    return code


def normalize_code(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def session_value(token: str, code: str) -> str:
    """Valeur du cookie à distance : dérivée du jeton local ET du code (changer le code déconnecte tous les appareils)."""
    return hmac.new(token.encode(), b"atelier3d-remote-v1:" + normalize_code(code).encode(), hashlib.sha256).hexdigest()[:48]


def code_matches(given: str, code: str) -> bool:
    return hmac.compare_digest(normalize_code(given), normalize_code(code))


class LoginLimiter:
    """5 essais ratés par adresse → blocage 10 minutes (le code ne peut pas être deviné par force brute)."""

    def __init__(self, max_fail: int = 5, lock_s: float = 600):
        self.max_fail, self.lock_s = max_fail, lock_s
        self.fails: dict[str, list[float]] = {}
        self.locked: dict[str, float] = {}

    def blocked(self, ip: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        until = self.locked.get(ip, 0)
        return max(0.0, until - now)

    def failure(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        lst = [t for t in self.fails.get(ip, []) if now - t < self.lock_s] + [now]
        self.fails[ip] = lst
        if len(lst) >= self.max_fail:
            self.locked[ip] = now + self.lock_s
            self.fails[ip] = []

    def success(self, ip: str) -> None:
        self.fails.pop(ip, None)
        self.locked.pop(ip, None)


# ---------------------------------------------------------------------------- décision (pure, testée)
def decide(*, client_ip: str, host: str, path: str, method: str, origin: str | None, authorization: str, cookies: dict,
           token: str, local_session: str, conf: dict, code: str, local_hosts: set[str]) -> tuple[str, str]:
    """Retourne (action, détail) : « ok », « login » (page de connexion), ou un refus « host », « ip », « unauthorized »,
    « origin », « host_only »."""
    h = (host or "").split(":")[0]
    if is_loopback(client_ip):
        if h not in local_hosts:
            return "host", "Hôte non autorisé"
        if path.startswith("/api/"):
            if not (authorization == f"Bearer {token}" or cookies.get("a3d_session") == local_session):
                return "unauthorized", "Jeton local requis (config/api_token) ou session du navigateur"
            if origin and method not in ("GET", "HEAD", "OPTIONS"):
                if origin.split("://", 1)[-1].split(":")[0].split("/")[0] not in local_hosts:
                    return "origin", "Origine refusée"
        return "ok", ""
    if not conf.get("enabled") or not ip_allowed(client_ip, conf.get("networks") or []):
        return "ip", f"Adresse {client_ip} non autorisée (accès à distance désactivé ou hors des réseaux autorisés)"
    if not host_ok_remote(host, conf.get("hostnames") or []):
        return "host", "Hôte non autorisé"
    if path in ("/remote/login", "/remote/logout"):
        return "ok", ""
    authed = authorization == f"Bearer {token}" or (cookies.get(COOKIE) or "") == session_value(token, code)
    if not authed:
        return ("unauthorized", "Code d'accès requis") if path.startswith("/api/") else ("login", "")
    if host_only(path):
        return "host_only", "Action réservée au PC lui-même (elle s'exécute sur son écran ou modifie l'accès à distance)"
    if origin and method not in ("GET", "HEAD", "OPTIONS"):
        if origin.split("://", 1)[-1].split("/")[0].lower() != (host or "").lower():
            return "origin", "Origine refusée"
    return "ok", ""
