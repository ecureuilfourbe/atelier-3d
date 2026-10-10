"""Construit l'archive livrable Atelier3D-1.0.0.zip à côté du dossier de distribution.

Exclut ce qui est propre à une machine ou régénérable : node_modules, __pycache__, install-location.json, journaux.
Inclut le frontend compilé (app/frontend/dist) : l'utilisateur n'a besoin ni de Node.js ni de npm.
Écrit aussi <zip>.sha256.

  <venv-app>\\python.exe installer\\tools\\make_release.py [--out E:\\App]
  <venv-app>\\python.exe installer\\tools\\make_release.py --share   → Atelier3D-1.0.0-partage.zip

--share : version à transmettre à une autre personne. Mêmes logiciel, installateur et workflows (dont les workflows
MiniMax H3 Ultra), sans le rapport de test de la machine de développement, et avec les chemins personnels des
documents (.md) remplacés par des descriptions neutres. Le contenu des workflows et du code n'est pas modifié.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
import zipfile

DIST = pathlib.Path(__file__).resolve().parents[2]
EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git", ".vite", "logs"}
EXCLUDE_FILES = {"install-location.json", "QUESTIONS_SESSION.md"}  # document de travail, pas un livrable
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".part"}
SHARE_EXCLUDE = {"TEST_REPORT.md"}  # journal de tests de la machine de développement
SHARE_SUBST = [  # documents seulement ; du plus long au plus court
    ("E:\\AI\\VidGen\\ComfyUI_windows_portable", "ComfyUI MiniMax déjà installé sur la machine de test"),
    ("E:\\AI\\VidGen", "ComfyUI MiniMax déjà installé sur la machine de test"),
    ("F:\\Blender\\blender.exe", "Blender déjà installé"),
    ("F:\\Blender", "Blender déjà installé"),
    ("E:\\App\\Gpasdenon", "<dossier du logiciel>"),
    ("E:\\Atelier 3D Données", "<dossier d'installation>"),
    ("E:\\Atelier3D_runtime", "<disque>:\\Atelier3D_runtime"),
    ("192.168.1.194", "192.168.1.20"),
]


def files(share: bool = False):
    for p in sorted(DIST.rglob("*")):
        rel = p.relative_to(DIST)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if share and p.name in SHARE_EXCLUDE:
            continue
        if p.is_file() and p.name not in EXCLUDE_FILES and p.suffix.lower() not in EXCLUDE_SUFFIXES:
            yield p, rel


def share_text(p: pathlib.Path) -> bytes | None:
    """Document .md avec chemins personnels neutralisés (None : fichier copié tel quel)."""
    if p.suffix.lower() != ".md":
        return None
    t = p.read_text(encoding="utf-8")
    for a, b in SHARE_SUBST:
        t = t.replace(a, b)
    return t.encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DIST.parent))
    ap.add_argument("--name", default=None)
    ap.add_argument("--share", action="store_true", help="version à transmettre (voir l'en-tête)")
    a = ap.parse_args()
    a.name = a.name or ("Atelier3D-1.0.0-partage" if a.share else "Atelier3D-1.0.0")
    if not (DIST / "app" / "frontend" / "dist" / "index.html").is_file():
        print("Frontend non compilé : lancez « npx vite build » dans app/frontend avant d'empaqueter.")
        return 2
    out = pathlib.Path(a.out) / f"{a.name}.zip"
    n = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p, rel in files(a.share):
            arc = pathlib.PurePosixPath("Atelier3D", *rel.parts).as_posix()
            data = share_text(p) if a.share else None
            if data is None:
                z.write(p, arc)
            else:
                z.writestr(arc, data)
            n += 1
    h = hashlib.sha256(out.read_bytes()).hexdigest()
    pathlib.Path(str(out) + ".sha256").write_text(f"{h}  {out.name}\n", encoding="ascii")
    print(f"{out}  ({n} fichiers, {out.stat().st_size / 2**20:.1f} Mio)\nSHA-256 {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
