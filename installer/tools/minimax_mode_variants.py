"""Outil de conversion : prépare, à partir d'une COPIE du workflow « MINIMAX H3 ULTRA + TURBO LORA » de l'utilisateur,
une variante éditeur par mode (texte, image, début/fin, références 1 ou 2 images), en ne changeant QUE l'état
activé/contourné des groupes — exactement ce que font les boutons « Fast Groups Bypasser » de l'utilisateur.

Configuration reproduite : celle de la génération réussie du 30/09/2026 sur cette machine (reçu Kaelith) :
groupe principal activé, LoRA turbo, MiniMaxH3SigmaShift (12/3) activé ; Spectrum et Patch Sage laissés contournés.
Chaque variante est ensuite convertie au format API par le frontend officiel (app.graphToPrompt) de l'instance utilisateur.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

SRC = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
base = json.loads(SRC.read_text(encoding="utf-8"))

COMMON = []  # chargeurs et Set/Get hors groupes : déjà actifs
MODES = {
    # nodes du groupe principal à activer, (instance de sous-graphe Spectrum, id SigmaShift, id Spectrum)
    "t2v": ([21, 22, 23, 24, 30, 28, 1987, 2057, 1980, 1983, 1981, 31, 32, 2000], ("b7d2ab76-9af5-45f9-b51c-8451c0ee0d7e", 1988, 1989),
            ["cd3f0dee-f39d-4aee-b1f6-0747a2fb2df8"]),
    "i2v": ([2013, 2014, 2015, 2016, 2020, 2021, 2037, 2031, 2065, 2024, 2030, 2017, 2023, 2022, 2027],
            ("21e17c8b-d2eb-49e6-9c94-f3e594cf7619", 2003, 2004), ["1e3a41dd-a979-45bc-946f-aeaedc560c28"]),
    "flf": ([2013, 2014, 2015, 2016, 2020, 2021, 2037, 2048, 2047, 2031, 2065, 2024, 2030, 2017, 2023, 2022, 2027],
            ("21e17c8b-d2eb-49e6-9c94-f3e594cf7619", 2003, 2004), ["1e3a41dd-a979-45bc-946f-aeaedc560c28"]),
    "r2v": ([2111, 2086, 2087, 2088, 2098, 2097, 2102, 2125, 2112, 2099, 2124, 2110, 2095, 2090, 2089, 2092],
            ("d66ac521-423d-41b4-8c3e-09cea388accf", 2074, 2075), ["68459711-76ae-4be6-b56d-3336e221f4ef"]),
    "r2v2": ([2111, 2086, 2087, 2088, 2098, 2097, 2102, 2125, 2112, 2099, 2100, 2124, 2110, 2095, 2090, 2089, 2092],
             ("d66ac521-423d-41b4-8c3e-09cea388accf", 2074, 2075), ["68459711-76ae-4be6-b56d-3336e221f4ef"]),
}

for mode, (enable, (spectrum_sg, sigma_id, spectrum_id), sampler_sgs) in MODES.items():
    wf = copy.deepcopy(base)
    for n in wf["nodes"]:
        if n["id"] in enable:
            n["mode"] = 0
    for sg in wf["definitions"]["subgraphs"]:
        if sg["id"] == spectrum_sg:
            for n in sg["nodes"]:
                n["mode"] = 0 if n["id"] == sigma_id else 4  # SigmaShift actif, Spectrum contourné
        if sg["id"] in sampler_sgs:
            for n in sg["nodes"]:
                n["mode"] = 0
    (OUT / f"minimax_{mode}.json").write_text(json.dumps(wf, ensure_ascii=False), encoding="utf-8")
    print(mode, "→", OUT / f"minimax_{mode}.json", "nodes activées :", len(enable))
