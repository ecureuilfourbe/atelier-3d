"""Stockage des artefacts et imports : fichiers sous <home>/data, empreintes SHA-256, provenance."""
from __future__ import annotations

import hashlib
import mimetypes
import pathlib
import shutil

from .db import Database, dumps, loads, new_id, now

mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("application/x-blender", ".blend")
mimetypes.add_type("application/octet-stream", ".fbx")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("image/webp", ".webp")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MESH_EXT = {".glb", ".gltf", ".obj", ".fbx", ".ply"}
VIDEO_EXT = {".mp4", ".webm", ".mov"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg"}


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def kind_for(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in MESH_EXT:
        return "mesh"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext == ".blend":
        return "blend"
    if ext in {".json"}:
        return "report"
    if ext in {".gif"}:
        return "animation_preview"
    if ext in {".txt", ".log"}:
        return "text"
    if ext == ".npz":
        return "data"
    return "file"


class Storage:
    def __init__(self, db: Database, data_dir: pathlib.Path):
        self.db = db
        self.data_dir = pathlib.Path(data_dir)
        (self.data_dir / "jobs").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ chemins
    def job_dir(self, job_id: str) -> pathlib.Path:
        d = self.data_dir / "jobs" / job_id
        for sub in ("inputs", "outputs", "previews", "logs"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    def rel(self, path: pathlib.Path) -> str:
        return pathlib.Path(path).resolve().relative_to(self.data_dir.resolve()).as_posix()

    def abs(self, rel: str) -> pathlib.Path:
        p = (self.data_dir / rel).resolve()
        if not str(p).startswith(str(self.data_dir.resolve())):
            raise PermissionError("chemin hors du dossier de données")
        return p

    # ------------------------------------------------------------------ artefacts
    def add_artifact(self, path: pathlib.Path, *, job_id: str | None, role: str, kind: str | None = None,
                     variant_id: str | None = None, meta: dict | None = None, conn=None) -> dict:
        path = pathlib.Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        art = {
            "id": new_id("art"),
            "job_id": job_id,
            "variant_id": variant_id,
            "kind": kind or kind_for(path),
            "role": role,
            "path": self.rel(path),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "meta": meta or {},
            "created_at": now(),
        }
        art["url"] = f"/api/v1/artifacts/{art['id']}/download"
        sql = ("INSERT INTO artifacts(id, job_id, variant_id, kind, role, path, sha256, size, mime, meta, created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)")
        vals = (art["id"], job_id, variant_id, art["kind"], role, art["path"], art["sha256"], art["size"], art["mime"],
                dumps(art["meta"]), art["created_at"])
        if conn is not None:
            conn.execute(sql, vals)
        else:
            with self.db.tx() as c:
                c.execute(sql, vals)
        return art

    def get_artifact(self, artifact_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return self.artifact_row(r) if r else None

    @staticmethod
    def artifact_row(r) -> dict:
        d = dict(r)
        d["meta"] = loads(d["meta"], {})
        d["url"] = f"/api/v1/artifacts/{d['id']}/download"
        return d

    def artifacts_for_job(self, job_id: str) -> list[dict]:
        return [self.artifact_row(r) for r in self.db.all("SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at", (job_id,))]

    def artifacts_by_ids(self, ids: list[str]) -> list[dict]:
        out = []
        for i in ids:
            a = self.get_artifact(i)
            if a:
                out.append(a)
        return out

    # ------------------------------------------------------------------ imports
    def add_upload(self, src: pathlib.Path, filename: str, *, meta: dict | None = None, move: bool = False) -> dict:
        src = pathlib.Path(src)
        ext = pathlib.Path(filename).suffix.lower()
        uid = new_id("upl")
        d = self.data_dir / "uploads" / uid
        d.mkdir(parents=True, exist_ok=True)
        safe = "original" + ext
        dest = d / safe
        if move:
            shutil.move(str(src), dest)
        else:
            shutil.copy2(src, dest)
        rec = {
            "id": uid, "filename": filename, "path": self.rel(dest), "sha256": sha256_file(dest),
            "size": dest.stat().st_size, "mime": mimetypes.guess_type(safe)[0] or "application/octet-stream",
            "kind": kind_for(dest), "meta": meta or {}, "created_at": now(),
        }
        with self.db.tx() as c:
            c.execute("INSERT INTO uploads(id, filename, path, sha256, size, mime, kind, meta, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (rec["id"], filename, rec["path"], rec["sha256"], rec["size"], rec["mime"], rec["kind"], dumps(rec["meta"]), rec["created_at"]))
        return rec

    def get_upload(self, upload_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        if not r:
            return None
        d = dict(r)
        d["meta"] = loads(d["meta"], {})
        d["url"] = f"/api/v1/uploads/{d['id']}/download"
        return d
