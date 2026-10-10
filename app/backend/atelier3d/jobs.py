"""Service de tâches : file persistante, transitions journalisées, idempotence, annulation et reprise.

États d'une tâche :
  queued → running → succeeded | failed | cancelled
  queued → blocked (prérequis en échec/annulé)          running → interrupted (crash, à reprendre)
  cancel_requested est un drapeau : l'arrêt effectif est l'état « cancelled ».
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .db import Database, dumps, loads, new_id, now
from .events import emit

TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}
ACTIVE = {"queued", "running"}


class JobError(Exception):
    def __init__(self, message: str, code: str = "job_error", status: int = 400, details: Any = None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details

    def to_json(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def finalize_run(db: Database, run_id: str | None) -> str | None:
    """Clôt un run quand plus aucune de ses étapes n'est en attente ou en cours (statut agrégé, fiche parent mise à jour).
    Appelée par le worker ET par l'annulation : un run dont toutes les étapes sont annulées avant démarrage restait sinon
    « en attente » indéfiniment (constaté le 09/10/2026)."""
    if not run_id:
        return None
    rows = db.all("SELECT status FROM jobs WHERE run_id = ? AND kind = 'graph_step'", (run_id,))
    if not rows or any(r["status"] in ("queued", "running") for r in rows):
        return None
    sts = [r["status"] for r in rows]
    status = "succeeded" if all(s == "succeeded" for s in sts) else ("partial" if "succeeded" in sts else "failed")
    if "cancelled" in sts and all(s in ("cancelled", "blocked") for s in sts):
        status = "cancelled"  # annulé par l'utilisateur ; la suite bloquée en conséquence ne fait pas du run un « échec »
    with db.tx() as c:
        cur = c.execute("SELECT status, graph_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        c.execute("UPDATE runs SET status = ?, finished_at = ? WHERE id = ?", (status, now(), run_id))
        c.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE run_id = ? AND kind != 'graph_step'", (status, now(), run_id))
        if not cur or cur["status"] != status:
            emit(db, "run_finished", run_id=run_id, graph_id=cur["graph_id"] if cur else None, conn=c, status=status,
                 counts={s: sts.count(s) for s in set(sts)})
    return status


def block_orphans(db: Database, run_id: str | None) -> list[str]:
    """Étapes en attente dont un prérequis a échoué, été annulé ou bloqué : bloquées explicitement (sinon elles attendraient
    indéfiniment un prérequis qui ne réussira jamais)."""
    if not run_id:
        return []
    st = {r["id"]: r["status"] for r in db.all("SELECT id, status FROM jobs WHERE run_id = ?", (run_id,))}
    out = []
    changed = True
    while changed:
        changed = False
        for r in db.all("SELECT id, depends_on, node_id, item_id, item_key, step, graph_id FROM jobs WHERE run_id = ? AND status = 'queued'", (run_id,)):
            bad = [d for d in loads(r["depends_on"], []) if st.get(d) in ("failed", "cancelled", "blocked")]
            if not bad:
                continue
            err = {"code": "prerequisite_failed", "message": "Prérequis en échec ou annulé : " + ", ".join(f"{d} ({st[d]})" for d in bad),
                   "prerequisites": bad}
            with db.tx() as c:
                c.execute("UPDATE jobs SET status = 'blocked', error = ?, finished_at = ? WHERE id = ?", (dumps(err), now(), r["id"]))
                emit(db, "job_blocked", job_id=r["id"], run_id=run_id, graph_id=r["graph_id"], node_id=r["node_id"], item_id=r["item_id"] or None,
                     conn=c, status="blocked", step=r["step"], item_key=r["item_key"], error=err)
            st[r["id"]] = "blocked"
            out.append(r["id"])
            changed = True
    return out


def job_row(r) -> dict:
    d = dict(r)
    for k in ("payload", "result", "error", "depends_on", "engine_ref", "progress"):
        d[k] = loads(d.get(k), None)
    d["cancel_requested"] = bool(d.get("cancel_requested"))
    return d


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class JobService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ idempotence
    def idempotent(self, key: str | None, endpoint: str, payload: Any, fn):
        """Exécute fn() une seule fois par clé ; même clé + autre charge utile → erreur 409."""
        if not key:
            return fn()
        ph = payload_hash({"endpoint": endpoint, "payload": payload})
        r = self.db.one("SELECT * FROM idempotency WHERE key = ?", (key,))
        if r:
            if r["payload_hash"] != ph or r["endpoint"] != endpoint:
                raise JobError(f"idempotency_key « {key} » déjà utilisée avec une autre requête", code="idempotency_conflict", status=409)
            res = loads(r["response"])
            res["idempotent_replay"] = True
            return res
        res = fn()
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO idempotency(key, endpoint, payload_hash, response, created_at) VALUES(?,?,?,?,?)",
                      (key, endpoint, ph, dumps(res), now()))
        return res

    # ------------------------------------------------------------------ création
    def insert_job(self, c, job: dict, *, author: str) -> None:
        c.execute(
            "INSERT INTO jobs(id, kind, run_id, graph_id, node_id, node_type, step, item_id, item_key, status, resource, priority, payload, "
            "author, depends_on, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job["kind"], job.get("run_id"), job.get("graph_id"), job.get("node_id"), job.get("node_type"), job.get("step"),
             job.get("item_id") or "", job.get("item_key"), "queued", job.get("resource", "none"), job.get("priority", 0),
             dumps(job.get("payload", {})), author, dumps(job.get("depends_on", [])), now()))
        emit(self.db, "job_queued", job_id=job["id"], run_id=job.get("run_id"), graph_id=job.get("graph_id"), node_id=job.get("node_id"),
             item_id=job.get("item_id") or None, conn=c, step=job.get("step"), item_key=job.get("item_key"), kind=job["kind"],
             depends_on=job.get("depends_on", []), author=author)

    def create_parent(self, kind: str, *, run_id: str | None, graph_id: str | None, payload: dict, author: str) -> dict:
        jid = new_id("job")
        with self.db.tx() as c:
            c.execute("INSERT INTO jobs(id, kind, run_id, graph_id, status, resource, payload, author, depends_on, created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (jid, kind, run_id, graph_id, "queued" if run_id else "succeeded", "none", dumps(payload), author, "[]", now()))
            emit(self.db, "job_created", job_id=jid, run_id=run_id, graph_id=graph_id, conn=c, kind=kind, author=author)
        return self.get(jid)

    # ------------------------------------------------------------------ lecture
    def get(self, job_id: str) -> dict:
        r = self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not r:
            raise JobError(f"Tâche {job_id} introuvable", code="not_found", status=404)
        return job_row(r)

    def list(self, *, status: str | None = None, graph_id: str | None = None, run_id: str | None = None, kind: str | None = None,
             node_id: str | None = None, item_id: str | None = None, limit: int = 200, parents_only: bool = False) -> list[dict]:
        where, args = [], []
        for col, val in (("status", status), ("graph_id", graph_id), ("run_id", run_id), ("kind", kind), ("node_id", node_id), ("item_id", item_id)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        if parents_only:
            where.append("kind != 'graph_step'")
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        return [job_row(r) for r in self.db.all(sql, (*args, limit))]

    def run_children(self, run_id: str) -> list[dict]:
        return [job_row(r) for r in self.db.all("SELECT * FROM jobs WHERE run_id = ? AND kind = 'graph_step' ORDER BY created_at", (run_id,))]

    def aggregate(self, job: dict) -> dict:
        """État agrégé d'une tâche parent (ou d'un run) à partir de ses tâches enfants."""
        if job["kind"] == "graph_step" or not job.get("run_id"):
            return job
        kids = self.run_children(job["run_id"])
        counts: dict[str, int] = {}
        for k in kids:
            counts[k["status"]] = counts.get(k["status"], 0) + 1
        total = len(kids)
        done = counts.get("succeeded", 0)
        if total and done == total:
            status = "succeeded"
        elif counts.get("running") or (counts.get("queued") and done + counts.get("failed", 0) + counts.get("blocked", 0) + counts.get("cancelled", 0) > 0):
            status = "running"
        elif counts.get("queued") == total:
            status = "queued"
        elif counts.get("queued") or counts.get("running"):
            status = "running"
        elif counts.get("cancelled") and not counts.get("failed"):
            status = "cancelled"
        elif done and (counts.get("failed") or counts.get("blocked")):
            status = "partial"
        elif counts.get("interrupted"):
            status = "interrupted"
        else:
            status = "failed" if total else job["status"]
        running = next((k for k in kids if k["status"] == "running"), None)
        job = dict(job)
        job["status"] = status
        job["children"] = [{"job_id": k["id"], "node_id": k["node_id"], "step": k["step"], "item_id": k["item_id"] or None,
                            "item_key": k["item_key"], "status": k["status"], "stage": k["stage"], "progress": k["progress"],
                            "error": k["error"], "result": k["result"]} for k in kids]
        job["counts"] = counts
        job["summary"] = f"{done} terminé{'s' if done > 1 else ''} / {total}" + "".join(
            f", {v} {lbl}" for key, lbl in (("running", "en cours"), ("queued", "en attente"), ("failed", "en échec"),
                                             ("blocked", "bloqué(s)"), ("cancelled", "annulé(s)"), ("interrupted", "interrompu(s)"))
            if (v := counts.get(key)))
        job["current"] = {"node_id": running["node_id"], "step": running["step"], "item_key": running["item_key"],
                          "stage": running["stage"], "progress": running["progress"]} if running else None
        return job

    # ------------------------------------------------------------------ transitions (appelées par le worker)
    def transition(self, job_id: str, status: str, *, conn=None, event: str | None = None, **fields) -> None:
        sets, args = ["status = ?"], [status]
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            args.append(dumps(v) if isinstance(v, (dict, list)) else v)
        if status == "running" and "started_at" not in fields:
            sets.append("started_at = ?")
            args.append(now())
        if status in TERMINAL or status == "interrupted":
            sets.append("finished_at = ?")
            args.append(now())

        def _do(c):
            row = c.execute("SELECT run_id, graph_id, node_id, item_id, item_key, step FROM jobs WHERE id = ?", (job_id,)).fetchone()
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", (*args, job_id))
            data = {k: v for k, v in fields.items() if k in ("error", "result", "stage")}
            emit(self.db, event or f"job_{status}", job_id=job_id, run_id=row["run_id"], graph_id=row["graph_id"], node_id=row["node_id"],
                 item_id=row["item_id"] or None, conn=c, status=status, step=row["step"], item_key=row["item_key"], **data)
        if conn is not None:
            _do(conn)
        else:
            with self.db.tx() as c:
                _do(c)

    # ------------------------------------------------------------------ annulation / reprise
    def request_cancel(self, job_id: str, *, author: str) -> dict:
        job = self.get(job_id)
        targets = [job]
        if job["kind"] != "graph_step" and job.get("run_id"):
            targets = self.run_children(job["run_id"])
        out = []
        with self.db.tx() as c:
            for j in targets:
                if j["status"] == "queued":
                    c.execute("UPDATE jobs SET status = 'cancelled', cancel_requested = 1, finished_at = ? WHERE id = ?", (now(), j["id"]))
                    emit(self.db, "job_cancelled", job_id=j["id"], run_id=j["run_id"], graph_id=j["graph_id"], node_id=j["node_id"],
                         item_id=j["item_id"] or None, conn=c, step=j["step"], item_key=j["item_key"], effective=True,
                         message="Annulée avant démarrage", author=author)
                    out.append({"job_id": j["id"], "state": "cancelled"})
                elif j["status"] == "running":
                    c.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (j["id"],))
                    emit(self.db, "cancel_requested", job_id=j["id"], run_id=j["run_id"], graph_id=j["graph_id"], node_id=j["node_id"],
                         item_id=j["item_id"] or None, conn=c, step=j["step"], item_key=j["item_key"],
                         message="Annulation demandée : arrêt effectif en attente du moteur", author=author)
                    out.append({"job_id": j["id"], "state": "cancel_requested"})
                else:
                    out.append({"job_id": j["id"], "state": j["status"], "note": "déjà terminée"})
        run_id = job.get("run_id")
        if run_id:  # étapes en attente qui dépendaient d'une étape annulée : bloquées ; run clos s'il ne reste rien à faire
            block_orphans(self.db, run_id)
            finalize_run(self.db, run_id)
        return {"job_id": job_id, "targets": out}

    def resume(self, job_id: str, *, author: str) -> dict:
        """Reprend après la dernière étape validée : seules les tâches interrompues/échouées/bloquées sont remises en file."""
        job = self.get(job_id)
        targets = [job] if job["kind"] == "graph_step" else (self.run_children(job["run_id"]) if job.get("run_id") else [])
        requeued = []
        with self.db.tx() as c:
            for j in targets:
                if j["status"] in ("interrupted", "failed", "blocked", "cancelled"):
                    c.execute("UPDATE jobs SET status = 'queued', cancel_requested = 0, error = NULL, attempt = attempt + 1, "
                              "worker_pid = NULL, finished_at = NULL WHERE id = ?", (j["id"],))
                    emit(self.db, "job_resumed", job_id=j["id"], run_id=j["run_id"], graph_id=j["graph_id"], node_id=j["node_id"],
                         item_id=j["item_id"] or None, conn=c, step=j["step"], item_key=j["item_key"], author=author,
                         engine_ref=j["engine_ref"])
                    requeued.append(j["id"])
            if requeued and job.get("run_id"):
                c.execute("UPDATE runs SET status = 'running', finished_at = NULL WHERE id = ?", (job["run_id"],))
                c.execute("UPDATE jobs SET status = 'queued', finished_at = NULL WHERE run_id = ? AND kind != 'graph_step'", (job["run_id"],))
        return {"job_id": job_id, "requeued": requeued,
                "kept": [j["id"] for j in targets if j["status"] == "succeeded"]}
