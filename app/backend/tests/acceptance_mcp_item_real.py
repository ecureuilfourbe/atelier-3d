"""Test d'acceptation RÉEL piloté par le serveur MCP stdio (comme Claude Code) : correction d'UN item foreach.

list_node_items → rerun_item (item_only, nouvelle seed, candidate non activée) → get_job_status jusqu'à la fin →
list_node_items (candidate signalée, autres items inchangés) → get_preview de la candidate → select_item_variant →
vérification que seuls les descendants de cet item deviennent à recalculer.

  python app/backend/tests/acceptance_mcp_item_real.py <graph_id> <node_id> <item_key> [<prompt_node_id> "<nouveau texte>"]
Avec un prompt : update_graph modifie d'abord le texte de la node Prompt de cet item (l'item devient « à recalculer »).
Écrit <installation>\\logs\\acceptance_mcp_item.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = pathlib.Path(__file__).resolve().parents[3]
R: dict = {"checks": []}


def check(name, ok, **info):
    R["checks"].append({"check": name, "ok": bool(ok), **info})
    print(("OK   " if ok else "ÉCHEC") + f" {name} {json.dumps(info, ensure_ascii=False)[:500]}", flush=True)


async def call(s, tool, **args):
    res = await s.call_tool(tool, args)
    if res.is_error:
        raise RuntimeError(f"{tool} : {res.content[0].text if res.content else 'erreur'}")
    return res


async def jcall(s, tool, **args):
    return json.loads((await call(s, tool, **args)).content[0].text)


async def main() -> int:
    gid, nid, key = sys.argv[1], sys.argv[2], sys.argv[3]
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp" / "atelier3d_mcp.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            before = (await jcall(s, "list_node_items", graph_id=gid, node_id=nid))["items"]
            target = next(it for it in before if it["item_key"] == key)
            others_before = {it["item_key"]: it["active_variant_id"] for it in before if it["item_key"] != key}
            g = await jcall(s, "get_graph", graph_id=gid, include_status=False)
            rev = g["graph"]["revision"]
            if len(sys.argv) >= 6:
                pnode, text = sys.argv[4], sys.argv[5]
                up = await jcall(s, "update_graph", graph_id=gid, expected_revision=rev,
                                 ops=[{"op": "update_node", "node_id": pnode, "params": {"text": text}}])
                rev = up["graph"]["revision"]
                st = (await jcall(s, "list_node_items", graph_id=gid, node_id=nid))["items"]
                check("mcp_prompt_modifie_item_a_recalculer",
                      next(it for it in st if it["item_key"] == key)["status_slot"] == "stale",
                      statut=next(it["status_slot"] for it in st if it["item_key"] == key), revision=rev)
                check("mcp_prompt_modifie_autres_items_a_jour",
                      all(it["status_slot"] == next(b["status_slot"] for b in before if b["item_key"] == it["item_key"]) for it in st if it["item_key"] != key),
                      statuts={it["item_key"]: it["status_slot"] for it in st})
            rr = await jcall(s, "rerun_item", graph_id=gid, node_id=nid, item_id=target["item_id"], scope="item_only",
                             params_patch={"seed": 77001}, force_recompute=True, activate_output=False, expected_graph_revision=rev,
                             idempotency_key=f"mcp-{gid}-{key}-77001")
            jobs = rr.get("jobs") or []
            check("mcp_rerun_item_une_tache", len(jobs) == 1, jobs=[(j.get("node_id"), j.get("item_key"), j.get("step")) for j in jobs])
            jid = jobs[0]["job_id"]
            t0 = time.time()
            while True:
                j = await jcall(s, "get_job_status", job_id=jid)
                if j["status"] in ("succeeded", "failed", "cancelled", "blocked", "partial"):
                    break
                await asyncio.sleep(10)
            check("mcp_item_termine", j["status"] == "succeeded", status=j["status"], duree_s=round(time.time() - t0), erreur=j.get("error"))
            vid = (j.get("result") or {}).get("variant_id")
            after = (await jcall(s, "list_node_items", graph_id=gid, node_id=nid))["items"]
            t2 = next(it for it in after if it["item_key"] == key)
            check("mcp_candidate_non_activee", t2["active_variant_id"] == target["active_variant_id"] and t2["candidate_not_selected"],
                  active=t2["active_variant_id"], latest=t2["latest_variant_id"], candidate=vid)
            check("mcp_autres_items_inchanges", {it["item_key"]: it["active_variant_id"] for it in after if it["item_key"] != key} == others_before)
            arts = await jcall(s, "list_artifacts", job_id=jid)
            img = next((a for a in arts.get("artifacts", []) if a.get("role") in ("image", "image_rgba")), None)
            if img:
                res = await call(s, "get_preview", artifact_id=img["id"], max_size=384)
                kinds = [c.type for c in res.content]
                check("mcp_get_preview_image", "image" in kinds, contenus=kinds)
                home = pathlib.Path(os.environ.get("ATELIER3D_HOME")
                                    or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])
                d = await jcall(s, "download_artifact", artifact_id=img["id"], dest_dir=str(home / "data" / "exports" / "acceptance_mcp_item"))
                R["candidate_path"] = d.get("path")
                print("candidate :", d.get("path"), flush=True)
            sel = await jcall(s, "select_item_variant", graph_id=gid, node_id=nid, item_id=target["item_id"], variant_id=vid)
            after2 = (await jcall(s, "list_node_items", graph_id=gid, node_id=nid))["items"]
            t3 = next(it for it in after2 if it["item_key"] == key)
            check("mcp_selection_appliquee", t3["active_variant_id"] == vid, active=t3["active_variant_id"])
            check("mcp_autres_items_toujours_inchanges",
                  {it["item_key"]: it["active_variant_id"] for it in after2 if it["item_key"] != key} == others_before)
            stale = sel.get("newly_stale") or []
            # aucun nœud qui dépend d'un AUTRE item (ex. l'édition branchée sur la preview Cheveux) ne doit devenir à recalculer
            g2 = await jcall(s, "get_graph", graph_id=gid, include_status=False)
            other_pv = {it["preview_node_id"] for it in after2 if it["item_key"] != key}
            fed_by_others = {e["to"][0] for e in g2["graph"]["edges"] if e["from"][0] in other_pv}
            check("mcp_seuls_descendants_a_recalculer", not any(str(x) in fed_by_others or str(x) in other_pv for x in stale),
                  newly_stale=stale, proteges=sorted(fed_by_others | other_pv))
    home = pathlib.Path(os.environ.get("ATELIER3D_HOME") or json.loads((ROOT / "install-location.json").read_text(encoding="utf-8-sig"))["home"])
    (home / "logs" / "acceptance_mcp_item.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Résumé :", sum(c["ok"] for c in R["checks"]), "/", len(R["checks"]), "vérifications OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
