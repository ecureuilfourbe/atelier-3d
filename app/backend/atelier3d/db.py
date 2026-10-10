"""Persistance SQLite (WAL) partagée entre le serveur API et le worker.

Chaque transition est écrite et validée (commit) AVANT d'être publiée : le flux d'événements
lit uniquement la table `events`, alimentée dans la même transaction que l'état.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import pathlib
import secrets
import sqlite3
import threading
import time
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS graphs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    doc TEXT NOT NULL,
    author TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_revisions (
    graph_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    doc TEXT NOT NULL,
    author TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (graph_id, revision)
);
CREATE TABLE IF NOT EXISTS slots (
    graph_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    step TEXT NOT NULL,
    item_id TEXT NOT NULL,
    active_variant_id TEXT,
    selected_by TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (graph_id, node_id, step, item_id)
);
CREATE TABLE IF NOT EXISTS variants (
    id TEXT PRIMARY KEY,
    graph_id TEXT,
    node_id TEXT,
    step TEXT,
    item_id TEXT,
    job_id TEXT,
    run_id TEXT,
    status TEXT NOT NULL,
    author TEXT NOT NULL,
    params TEXT NOT NULL,
    seed INTEGER,
    inputs TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    cache_key TEXT,
    outputs TEXT NOT NULL,
    engine TEXT NOT NULL,
    stats TEXT,
    from_cache_of TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variants_slot ON variants(graph_id, node_id, step, item_id);
CREATE INDEX IF NOT EXISTS idx_variants_cache ON variants(cache_key);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    graph_id TEXT,
    revision INTEGER,
    snapshot TEXT,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    author TEXT NOT NULL,
    options TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    run_id TEXT,
    graph_id TEXT,
    node_id TEXT,
    node_type TEXT,
    step TEXT,
    item_id TEXT,
    item_key TEXT,
    status TEXT NOT NULL,
    resource TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    result TEXT,
    error TEXT,
    author TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    engine_ref TEXT,
    progress TEXT,
    stage TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER,
    heartbeat_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_graph ON jobs(graph_id, node_id, item_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    job_id TEXT,
    run_id TEXT,
    graph_id TEXT,
    node_id TEXT,
    item_id TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_events_graph ON events(graph_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    variant_id TEXT,
    kind TEXT NOT NULL,
    role TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    meta TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    kind TEXT NOT NULL,
    meta TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%y%m%d')}{secrets.token_hex(5)}"


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


class Database:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)  # executescript gère sa propre validation
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transaction IMMEDIATE (verrou d'écriture pris dès le début, réessais en cas de contention)."""
        c = self.conn()
        if c.in_transaction:  # transaction imbriquée : on réutilise la transaction courante
            yield c
            return
        for attempt in range(50):
            try:
                c.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.05 * (attempt + 1))
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchall()


_DB: Database | None = None
_DB_LOCK = threading.Lock()


def get_db(path: pathlib.Path | None = None) -> Database:
    global _DB
    with _DB_LOCK:
        if _DB is None:
            if path is None:
                from . import config

                path = config.load().db_path
            _DB = Database(path)
        return _DB


def reset_db_singleton() -> None:
    global _DB
    _DB = None
