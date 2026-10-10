"""Journal d'événements persistant (rejouable par identifiant croissant)."""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import Database, dumps, loads, now


def emit(db: Database, type_: str, *, job_id: str | None = None, run_id: str | None = None,
         graph_id: str | None = None, node_id: str | None = None, item_id: str | None = None,
         conn: sqlite3.Connection | None = None, **data: Any) -> int:
    """Enregistre un événement. Avec `conn`, s'insère dans la transaction de la transition d'état."""
    sql = "INSERT INTO events(ts, type, job_id, run_id, graph_id, node_id, item_id, data) VALUES(?,?,?,?,?,?,?,?)"
    args = (now(), type_, job_id, run_id, graph_id, node_id, item_id, dumps(data))
    if conn is not None:
        return conn.execute(sql, args).lastrowid
    with db.tx() as c:
        return c.execute(sql, args).lastrowid


def row_to_event(r) -> dict:
    return {
        "id": r["id"], "ts": r["ts"], "type": r["type"], "job_id": r["job_id"], "run_id": r["run_id"],
        "graph_id": r["graph_id"], "node_id": r["node_id"], "item_id": r["item_id"], "data": loads(r["data"], {}),
    }


def since(db: Database, after_id: int = 0, *, job_id: str | None = None, graph_id: str | None = None,
          run_id: str | None = None, limit: int = 500) -> list[dict]:
    where, args = ["id > ?"], [after_id]
    if job_id:
        where.append("job_id = ?")
        args.append(job_id)
    if graph_id:
        where.append("graph_id = ?")
        args.append(graph_id)
    if run_id:
        where.append("run_id = ?")
        args.append(run_id)
    rows = db.all(f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id LIMIT ?", (*args, limit))
    return [row_to_event(r) for r in rows]


def last_id(db: Database) -> int:
    r = db.one("SELECT MAX(id) AS m FROM events")
    return int(r["m"] or 0)
