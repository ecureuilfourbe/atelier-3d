"""Tests du contrôle d'accès (accès à distance par VPN) : logique pure, sans serveur ni réseau.

Exécution : <venv-app>\\python.exe app/backend/tests/test_access.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atelier3d import access as A  # noqa: E402

TOKEN, LOCAL = "t" * 64, "s" * 48
HOSTS = {"127.0.0.1", "localhost", "[::1]"}
ON = {"enabled": True, "networks": ["192.168.27.64/27"], "hostnames": []}
CODE = "ABCD-EFGH-JKMN"


def d(ip, path="/api/v1/graphs", method="GET", host="192.168.1.20:8765", origin=None, auth="", cookies=None, conf=ON):
    return A.decide(client_ip=ip, host=host, path=path, method=method, origin=origin, authorization=auth, cookies=cookies or {},
                    token=TOKEN, local_session=LOCAL, conf=conf, code=CODE, local_hosts=HOSTS)[0]


def test_local_pc_unchanged():
    assert d("127.0.0.1", host="127.0.0.1:8765", cookies={"a3d_session": LOCAL}) == "ok"
    assert d("127.0.0.1", host="127.0.0.1:8765") == "unauthorized"
    assert d("127.0.0.1", host="evil.example:8765", cookies={"a3d_session": LOCAL}) == "host", "anti DNS-rebinding conservé"
    assert d("127.0.0.1", host="127.0.0.1:8765", method="POST", origin="http://evil.example", cookies={"a3d_session": LOCAL}) == "origin"
    assert d("127.0.0.1", path="/", host="localhost:8765") == "ok"


def test_remote_refused_when_disabled_or_outside_vpn():
    assert d("192.168.27.65", conf={**ON, "enabled": False}) == "ip", "désactivé : personne d'autre que le PC"
    assert d("192.168.1.50") == "ip", "un appareil du réseau local (hors VPN) est refusé"
    assert d("10.2.0.7") == "ip"
    assert d("192.168.27.65", cookies={"a3d_session": LOCAL}) == "unauthorized", "le cookie du PC ne vaut pas à distance"


def test_remote_login_and_session():
    assert d("192.168.27.65", path="/") == "login"
    assert d("192.168.27.65", path="/remote/login") == "ok"
    good = {A.COOKIE: A.session_value(TOKEN, CODE)}
    assert d("192.168.27.65", cookies=good) == "ok"
    assert d("192.168.27.65", cookies={A.COOKIE: A.session_value(TOKEN, "ZZZZ-ZZZZ-ZZZZ")}) == "unauthorized", "changer le code déconnecte"
    assert d("192.168.27.65", auth=f"Bearer {TOKEN}") == "ok", "client API avec le jeton"
    assert d("192.168.27.65", host="nas.evil.example", cookies=good) == "host", "à distance : Host = adresse IP littérale"
    assert d("192.168.27.65", path="/api/v1/artifacts/a1/open-folder", method="POST", cookies=good) == "host_only"
    assert d("192.168.27.65", path="/api/v1/settings/remote", cookies=good) == "host_only", "les réglages d'accès restent sur le PC"
    assert d("192.168.27.65", path="/api/v1/graphs", method="POST", origin="http://192.168.1.20:8765", cookies=good) == "ok"
    assert d("192.168.27.65", path="/api/v1/graphs", method="POST", origin="http://192.168.1.50:8765", cookies=good) == "origin"


def test_code_networks_and_limiter():
    cfgdir = pathlib.Path(tempfile.mkdtemp())
    c1 = A.access_code(cfgdir)
    assert len(A.normalize_code(c1)) == 12 and A.access_code(cfgdir) == c1
    assert A.code_matches(c1.lower().replace("-", " "), c1)
    c2 = A.regenerate_code(cfgdir)
    assert c2 != c1 and not A.code_matches(c1, c2)
    assert A.validate_networks(["192.168.27.70/27"]) == ["192.168.27.64/27"]
    for bad in (["0.0.0.0/0"], ["8.8.8.0/24"], ["10.0.0.0/8"], []):
        try:
            A.validate_networks(bad)
            raise AssertionError(f"{bad} aurait dû être refusé")
        except ValueError:
            pass
    lim = A.LoginLimiter(max_fail=3, lock_s=60)
    for _ in range(3):
        lim.failure("192.168.27.65", now=100.0)
    assert lim.blocked("192.168.27.65", now=110.0) > 0 and lim.blocked("192.168.27.66", now=110.0) == 0
    assert lim.blocked("192.168.27.65", now=200.0) == 0


def test_conf_reread_and_defaults():
    p = pathlib.Path(tempfile.mkdtemp()) / "atelier3d.json"
    p.write_text(json.dumps({"security": {}}), encoding="utf-8")
    c = A.remote_conf(p)
    assert c["enabled"] is False and c["networks"] == A.DEFAULT_NETWORKS
    import os
    import time

    p.write_text(json.dumps({"security": {"remote": {"enabled": True, "networks": ["192.168.27.64/27"]}}}), encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert A.remote_conf(p)["enabled"] is True, "activation relue sans redémarrer le contrôle"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except Exception as e:  # noqa: BLE001
                import traceback

                fails += 1
                print(f"ÉCHEC {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)
