"""Exemple Python DIRECT ComfyUI (sans l'API du studio) : génération Krea 2 Turbo avec le workflow API vérifié.

Montre la procédure bas niveau que l'adapter automatise : /object_info → vérification → clone du workflow →
modification des seules entrées mappées → POST /prompt {prompt, client_id, prompt_id} → /ws + /history → /view.
Pour l'usage courant, préférez le client du studio (clients/python) ou le serveur MCP.

Usage : <venv-app>\\python.exe examples\\comfy_direct_krea2.py --url http://127.0.0.1:8190 --out sortie.png "votre prompt"
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "backend"))

from atelier3d.adapters.comfyui import ComfyClient, history_files, history_text, new_prompt_id  # noqa: E402
from atelier3d.adapters.workflows import TemplateRegistry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--url", default="http://127.0.0.1:8190")
    ap.add_argument("--seed", type=int, default=42001)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--out", default="krea2_direct.png")
    a = ap.parse_args()

    client = ComfyClient(a.url)
    if not client.alive():
        print(f"ComfyUI injoignable sur {a.url}")
        return 2
    tpl = TemplateRegistry(ROOT / "app" / "workflows" / "comfy").get("krea2_turbo_t2i")
    folders = {m["folder"].split("/")[0] for m in tpl.bindings["required_models"]}
    problems = tpl.verify(client.object_info(), {f: client.models(f) for f in folders})
    if problems:
        print("Workflow non exécutable sur ce ComfyUI :\n - " + "\n - ".join(problems))
        return 3
    wf = tpl.instantiate({"prompt": a.prompt, "seed": a.seed, "width": a.width, "height": a.height,
                          "refine_prompt": False, "filename_prefix": "exemple_direct/krea2"})
    pid = new_prompt_id()
    t0 = time.time()

    def on_event(kind, data):
        if kind == "progress":
            print(f"  progression {data.get('value')}/{data.get('max')} (node {data.get('node')})")
        elif kind in ("execution_start", "execution_cached", "execution_success", "execution_error"):
            print(f"  {kind}")

    h = client.run(wf, prompt_id=pid, client_id="exemple-direct", on_event=on_event)
    files = history_files(h, tpl.bindings["outputs"]["image"]["node"], "images")
    out = client.download(files[0], pathlib.Path(a.out))
    print(f"Image : {out.resolve()}  ({time.time() - t0:.1f} s, prompt_id {pid})")
    print("Prompt final :", history_text(h, tpl.bindings["outputs"]["final_prompt"]["node"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
