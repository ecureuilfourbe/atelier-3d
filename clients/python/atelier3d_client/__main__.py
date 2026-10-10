"""CLI : python -m atelier3d_client <commande> …

  upload <fichier>                          import d'image ou de maillage → upload_id
  create <kind> [--upload ID] [--params JSON] [--key IDEMPOTENCE]
  watch <job_id>                            événements en direct (SSE, reconnexion)
  wait <job_id> [--timeout S]               attend la fin (le timeout n'annule pas la tâche)
  download <artifact_id> <dossier|fichier>
  cancel <job_id> | resume <job_id> | status <job_id> | artifacts <job_id>
  capabilities | graph <graph_id> | items <graph_id> <node_id>
"""
from __future__ import annotations

import argparse
import json
import sys

from . import Atelier3D, Atelier3DError


def main() -> int:
    ap = argparse.ArgumentParser(prog="atelier3d")
    ap.add_argument("--author", default="utilisateur")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("upload"); s.add_argument("path")
    s = sub.add_parser("create"); s.add_argument("kind"); s.add_argument("--upload"); s.add_argument("--params", default="{}"); s.add_argument("--key")
    s = sub.add_parser("watch"); s.add_argument("job_id")
    s = sub.add_parser("wait"); s.add_argument("job_id"); s.add_argument("--timeout", type=float, default=3600)
    s = sub.add_parser("download"); s.add_argument("artifact_id"); s.add_argument("dest")
    for c in ("cancel", "resume", "status", "artifacts"):
        s = sub.add_parser(c); s.add_argument("job_id")
    sub.add_parser("capabilities")
    s = sub.add_parser("graph"); s.add_argument("graph_id")
    s = sub.add_parser("items"); s.add_argument("graph_id"); s.add_argument("node_id")
    a = ap.parse_args()
    c = Atelier3D(author=a.author)
    try:
        if a.cmd == "upload":
            out = c.upload(a.path)
        elif a.cmd == "create":
            out = c.create_job(a.kind, json.loads(a.params), upload_id=a.upload, idempotency_key=a.key)
        elif a.cmd == "watch":
            for ev in c.watch(a.job_id):
                d = ev["data"]
                print(f"[{ev['id']}] {ev['type']:18s} {ev.get('node_id') or ''} {d.get('item_key') or ''} {d.get('message') or d.get('stage') or ''} "
                      f"{('%s/%s' % (d.get('value'), d.get('max'))) if ev['type'] == 'progress' else ''}", flush=True)
                if ev["type"] in ("job_succeeded", "job_failed", "job_cancelled") and ev.get("job_id") == a.job_id:
                    break
            return 0
        elif a.cmd == "wait":
            out = c.wait(a.job_id, timeout=a.timeout, on_update=lambda j: print(f"  {j['status']} {j.get('summary') or ''} {j.get('stage') or ''}", flush=True))
        elif a.cmd == "download":
            out = c.download(a.artifact_id, a.dest)
        elif a.cmd == "cancel":
            out = c.cancel(a.job_id)
        elif a.cmd == "resume":
            out = c.resume(a.job_id)
        elif a.cmd == "status":
            out = c.job(a.job_id)
        elif a.cmd == "artifacts":
            out = c.artifacts(a.job_id)
        elif a.cmd == "capabilities":
            out = c.capabilities()
        elif a.cmd == "graph":
            out = c.graph(a.graph_id)
        elif a.cmd == "items":
            out = c.items(a.graph_id, a.node_id)
        else:
            return 2
    except Atelier3DError as e:
        print(str(e), file=sys.stderr)
        return 1
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        return 3
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
