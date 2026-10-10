"""Test réel du serveur MCP stdio : démarre atelier3d_mcp.py comme le ferait Claude Code, liste les outils et appelle
get_capabilities, puis (optionnel) list_node_items et get_preview sur un graphe existant.

Usage : <venv-app>\\python.exe examples\\mcp_smoke_test.py [graph_id node_id]
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = pathlib.Path(__file__).resolve().parents[1]


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp" / "atelier3d_mcp.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"{len(names)} outils : {', '.join(names)}")
            res = await s.call_tool("get_capabilities", {})
            data = json.loads(res.content[0].text)
            print("capacités :", {k: v.get("status") for k, v in data["capabilities"].items()})
            if len(sys.argv) >= 3:
                gid, nid = sys.argv[1], sys.argv[2]
                res = await s.call_tool("list_node_items", {"graph_id": gid, "node_id": nid})
                items = json.loads(res.content[0].text)["items"]
                for it in items:
                    print(f"  item {it['item_key']:12s} {it['item_id']} variante {it['active_variant_id']} image {it['image_artifact']}")
                art = next((it["image_artifact"] for it in items if it["image_artifact"]), None)
                if art:
                    res = await s.call_tool("get_preview", {"artifact_id": art, "max_size": 256})
                    kinds = [c.type for c in res.content]
                    print("get_preview →", kinds, [len(c.data) for c in res.content if c.type == "image"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
