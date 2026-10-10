"""Outil de développement : résume un workflow ComfyUI au format éditeur (nodes, sous-graphes, modèles).

Usage : python inspect_workflow.py <workflow.json>
"""
from __future__ import annotations

import json
import sys


def short(v, n=140):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def dump_nodes(nodes, indent=""):
    for nd in nodes:
        props = nd.get("properties") or {}
        models = props.get("models")
        print(f"{indent}{nd['id']:>5} {nd['type']:<44} mode={nd.get('mode', 0)} title={nd.get('title', '')!r}")
        if nd.get("widgets_values") is not None:
            print(f"{indent}       widgets={short(nd['widgets_values'])}")
        ins = [f"{i['name']}:{i.get('type')}{'*' if i.get('link') is not None else ''}" for i in nd.get("inputs") or []]
        outs = [f"{o['name']}:{o.get('type')}" for o in nd.get("outputs") or []]
        if ins:
            print(f"{indent}       in={ins}")
        if outs:
            print(f"{indent}       out={outs}")
        if models:
            for m in models:
                print(f"{indent}       model {m.get('name')} -> {m.get('directory')} {m.get('url')}")


def main(path: str) -> None:
    wf = json.load(open(path, encoding="utf-8"))
    print("keys:", list(wf))
    dump_nodes(wf.get("nodes", []))
    for sg in (wf.get("definitions") or {}).get("subgraphs", []):
        print(f"== subgraph {sg['id']} name={sg.get('name')!r} inputs={[i.get('name') for i in sg.get('inputs', [])]} outputs={[o.get('name') for o in sg.get('outputs', [])]}")
        dump_nodes(sg.get("nodes", []), indent="    ")


if __name__ == "__main__":
    main(sys.argv[1])
