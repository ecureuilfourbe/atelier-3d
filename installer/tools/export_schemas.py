"""Régénère app/schemas/graph-1.1.schema.json et app/schemas/node-types.json depuis le code (types de nodes, ports,
compatibilités). À lancer après toute modification de graph/nodetypes.py ou graph/model.py, avant make_release.py.

  <venv-app>\\python.exe installer\\tools\\export_schemas.py
"""
from __future__ import annotations

import json
import pathlib
import sys

DIST = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DIST / "app" / "backend"))

from atelier3d.graph import nodetypes as NT  # noqa: E402
from atelier3d.graph.model import json_schema  # noqa: E402


def main() -> int:
    out = DIST / "app" / "schemas"
    (out / "graph-1.1.schema.json").write_text(json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    types = []
    for nt in NT.catalog():
        steps = []
        for st in NT.get(nt["type"]).step_list({"type": nt["type"], "params": NT.defaults_for(NT.get(nt["type"]))}):
            steps.append({"id": st.id, "label": st.label, "capability": st.capability, "resource": st.resource, "generative": st.generative})
        types.append({**nt, "default_steps": steps})
    doc = {"node_types": types, "port_types": NT.PORT_TYPES, "compatibility": {k: sorted(v) for k, v in NT.COMPATIBLE.items()}}
    (out / "node-types.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{out / 'graph-1.1.schema.json'}\n{out / 'node-types.json'} ({len(types)} types de nodes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
