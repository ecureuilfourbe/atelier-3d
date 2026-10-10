"""Worker séparé : exécute les tâches persistantes (un emplacement GPU sous verrou global + un emplacement CPU).

- Les entrées « job:<id> » d'un run sont résolues vers la variante produite DANS ce run (snapshot).
- Cache exact : clé = type, étape, moteur (workflow/sha/modèles/script), paramètres effectifs (seed incluse),
  contenu des entrées (SHA-256 des fichiers, texte). « Forcer le recalcul » contourne le cache.
- OOM : libération mémoire + UNE nouvelle tentative avec un profil réduit compatible, journalisée.
- Démarrage : réconciliation des tâches restées « running » (moteur interrogé ; jamais déclarées terminées sans preuve).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import signal
import sys
import threading
import time
import traceback

import psutil

from . import config as C
from .adapters.base import Cancelled, StepFailure
from .adapters.workflows import TemplateRegistry
from .db import Database, dumps, get_db, loads, new_id, now
from .events import emit
from .executors import EXECUTORS, Services, StepContext, executor_for
from .graph.state import StateStore, compose_text, sha
from .jobs import JobService, block_orphans, finalize_run
from .storage import Storage

SCRIPT_VERSION = "atelier3d-1.0.0"


def code_fingerprint(dist: pathlib.Path) -> str:
    """Empreinte du code qui produit les résultats hors ComfyUI (scripts Blender, exécution MIA, exécuteurs) : un script
    corrigé ne doit jamais renvoyer un ancien résultat depuis le cache (constaté : rig articulé servi depuis une version
    antérieure du script)."""
    h = hashlib.sha256()
    files = sorted((dist / "app" / "blender_scripts").glob("*.py")) + sorted((dist / "app" / "engines" / "mia_runner").glob("*.py"))
    files += [pathlib.Path(__file__).with_name(n) for n in ("executors.py", "video.py")]
    for f in files:
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]
REDUCED_PROFILES = {
    "mesh.generate": {"texture_size": 1024, "decimate_target": 150000},
    "image.create": {"width": 768, "height": 768},
    "image.edit": {"width": 768, "height": 768},
    "video.minimax": {"width": 416, "height": 736, "low_memory": True, "text_encoder": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"},
}


class Worker:
    def __init__(self, cfg: C.Config):
        self.cfg = cfg
        self.db: Database = get_db(cfg.db_path)
        self.storage = Storage(self.db, cfg.data_dir)
        self.jobs = JobService(self.db)
        self.state = StateStore(self.db)
        self.templates = TemplateRegistry(cfg.workflows_dir / "comfy")
        self.code_fp = code_fingerprint(cfg.dist)  # recalculée à chaque démarrage du worker
        self.services = Services(self.db, cfg)
        from . import video  # enregistre l'exécuteur vidéo
        video.install(self.services)
        self.pid = os.getpid()
        self.stop = threading.Event()
        self.busy = {"gpu": None, "cpu": None, "net": None}  # net : services externes (OpenAI), sans GPU
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ boucle
    def serve(self) -> None:
        self.reconcile()
        threads = [threading.Thread(target=self.slot_loop, args=(slot,), daemon=True, name=f"slot-{slot}") for slot in ("gpu", "cpu", "net")]
        for t in threads:
            t.start()
        hb = threading.Thread(target=self.heartbeat, daemon=True)
        hb.start()
        while not self.stop.is_set():
            time.sleep(0.5)

    def heartbeat(self):
        while not self.stop.is_set():
            ids = [j for j in self.busy.values() if j]
            if ids:
                with self.db.tx() as c:
                    for j in ids:
                        c.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (now(), j))
            time.sleep(5)

    def slot_loop(self, slot: str) -> None:
        while not self.stop.is_set():
            try:
                job = self.claim(slot)
            except Exception:
                traceback.print_exc()
                time.sleep(1)
                continue
            if job is None:
                time.sleep(0.3)
                continue
            self.busy[slot] = job["id"]
            try:
                self.execute(job)
            except Exception:
                traceback.print_exc()
            finally:
                self.busy[slot] = None

    def claim(self, slot: str) -> dict | None:
        resources = {"gpu": ("gpu",), "net": ("net",)}.get(slot, ("cpu", "none"))
        rows = self.db.all("SELECT * FROM jobs WHERE status = 'queued' AND kind = 'graph_step' ORDER BY priority DESC, created_at LIMIT 200")
        for r in rows:
            if r["resource"] not in resources:
                continue
            deps = loads(r["depends_on"], [])
            if deps:
                st = {x["id"]: x["status"] for x in self.db.all(
                    f"SELECT id, status FROM jobs WHERE id IN ({','.join('?' * len(deps))})", tuple(deps))}
                bad = [d for d in deps if st.get(d) in ("failed", "blocked", "cancelled", "interrupted")]
                if bad:
                    self.block(r["id"], bad, {d: st.get(d) for d in bad})
                    continue
                if any(st.get(d) != "succeeded" for d in deps):
                    continue
            with self.db.tx() as c:
                cur = c.execute("UPDATE jobs SET status = 'running', worker_pid = ?, started_at = ?, heartbeat_at = ? WHERE id = ? AND status = 'queued'",
                                (self.pid, now(), now(), r["id"]))
                if cur.rowcount != 1:
                    continue
                emit(self.db, "job_started", job_id=r["id"], run_id=r["run_id"], graph_id=r["graph_id"], node_id=r["node_id"],
                     item_id=r["item_id"] or None, conn=c, step=r["step"], item_key=r["item_key"], resource=r["resource"])
                if r["run_id"]:
                    c.execute("UPDATE runs SET status = 'running' WHERE id = ? AND status = 'queued'", (r["run_id"],))
            return self.jobs.get(r["id"])
        return None

    def block(self, job_id: str, bad: list[str], states: dict) -> None:
        self.jobs.transition(job_id, "blocked", error={"code": "prerequisite_failed",
                                                      "message": "Prérequis en échec ou annulé : " + ", ".join(f"{k} ({v})" for k, v in states.items()),
                                                      "prerequisites": bad})
        self._finish_run_if_done(self.jobs.get(job_id).get("run_id"))

    # ------------------------------------------------------------------ résolution des entrées
    def resolve(self, v):
        if isinstance(v, list):
            return [self.resolve(x) for x in v]
        if not isinstance(v, dict):
            return v
        k = v.get("kind")
        if k == "job":
            j = self.jobs.get(v["job_id"])
            res = j.get("result") or {}
            vid = res.get("variant_id")
            if not vid:
                return {"kind": "missing", "reason": f"la tâche {v['job_id']} n'a produit aucune variante"}
            ref = {"kind": "variant", "variant_id": vid, "node_id": v.get("node_id"), "step": v.get("step"),
                   "item_id": v.get("item_id"), "output": v.get("output"), "item_key": v.get("item_key"), "from_run": True}
            if v.get("text_index") is not None:  # n-ième prompt d'une liste écrite dans ce run : même identité (texte) que l'état
                return {**self.state.list_text(ref, int(v["text_index"])), **({"edge_id": v["edge_id"]} if v.get("edge_id") else {})}
            return ref
        if k in ("list", "text_list"):
            return {**v, "items": [self.resolve(x) for x in v["items"]]}
        if k == "compose":
            parts = []
            for x in (self.resolve(y) for y in v["parts"]):
                parts += x["items"] if x.get("kind") == "text_list" else [x]
            if all(x.get("kind") == "text" for x in parts):
                return compose_text(parts, v.get("format") or {}, v.get("node_id"))
            return {"kind": "missing", "reason": "fragment non textuel"}
        if k == "references":
            return {**v, "slots": [{**s, "ref": self.resolve(s.get("ref"))} for s in v["slots"]]}
        return v

    def content_identity(self, v):
        """Identité de CONTENU (pour le cache) : empreintes des fichiers, pas les identifiants."""
        if isinstance(v, list):
            return [self.content_identity(x) for x in v]
        if not isinstance(v, dict):
            return v
        k = v.get("kind")
        if k == "text":
            return {"text": v["sha"]}
        if k == "upload":
            u = self.storage.get_upload(v["upload_id"])
            return {"upload": u["sha256"] if u else None}
        if k == "variant":
            r = self.db.one("SELECT outputs FROM variants WHERE id = ?", (v["variant_id"],))
            outs = loads(r["outputs"], {}) if r else {}
            hs = {}
            for port, ids in sorted(outs.items()):
                hs[port] = [x["sha256"] for x in (self.storage.get_artifact(i) for i in ids) if x]
            return {"content": hs, "output": v.get("output")}
        if k in ("list", "text_list"):
            return {"list": [self.content_identity(x) for x in v["items"]]}
        if k == "references":
            return {"refs": [{"role": s.get("role"), "label": s.get("label"), "description": s.get("description"),
                              "ref": self.content_identity(s.get("ref"))} for s in v["slots"]]}
        return {k: True}

    def engine_fingerprint(self, payload: dict) -> dict:
        eng = payload.get("engine") or {}
        fp = {"capability": payload.get("capability"), "script_version": SCRIPT_VERSION, "code": self.code_fp}
        wid = eng.get("workflow_id")
        if wid:
            try:
                fp["workflow"] = self.templates.get(wid).fingerprint()
            except Exception:
                fp["workflow"] = wid
        if payload.get("capability") == "rig.mia":
            fp["mia_commit"] = self.cfg.get("mia.commit")
        if payload.get("capability") == "blender" or payload.get("capability") == "mesh.generate":
            fp["blender"] = self.cfg.get("blender.version")
        if payload.get("capability") in ("image.openai", "vision.openai", "text.openai"):
            from .secrets_store import load_services

            o = load_services(self.cfg.config_dir)["openai"]  # modèles par défaut des Réglages : font partie du moteur
            fp["openai"] = {k: o.get(k) for k in ("model_generate", "model_edit", "model_text", "quality", "moderation")}
        if payload.get("capability") in ("vision.claude", "text.claude"):
            from .secrets_store import load_services

            fp["anthropic"] = {"model": load_services(self.cfg.config_dir)["anthropic"].get("model")}
        if payload.get("capability") == "video.minimax":
            fp["video"] = {k: self.cfg.get(f"video.{k}") for k in ("comfy_dir", "version", "models")}
        return fp

    # ------------------------------------------------------------------ exécution
    def execute(self, job: dict) -> None:
        payload = job["payload"]
        raw_inputs = payload.get("inputs") or {}
        resolved = {k: self.resolve(v) for k, v in raw_inputs.items()}
        missing = [k for k, v in resolved.items() if isinstance(v, dict) and v.get("kind") == "missing"]
        if missing:
            self.fail(job, StepFailure("Entrées indisponibles : " + ", ".join(f"{k} ({resolved[k].get('reason')})" for k in missing),
                                       code="missing_input"))
            return
        payload["resolved_inputs"] = resolved
        identity = {k: StateStore.ref_identity(v) for k, v in sorted(resolved.items())}
        step_params = payload.get("step_params") or {}
        fingerprint = sha({"inputs": identity, "params": step_params})
        eng_fp = self.engine_fingerprint(payload)
        cache_key = sha({"node_type": job["node_type"], "step": job["step"], "engine": eng_fp, "params": step_params,
                         "seed": payload.get("seed"), "inputs": {k: self.content_identity(v) for k, v in sorted(resolved.items())},
                         "schema": "1.1"})
        if not payload.get("force_recompute"):
            hit = self.db.one("SELECT * FROM variants WHERE cache_key = ? AND status = 'succeeded' ORDER BY created_at DESC LIMIT 1", (cache_key,))
            if hit:
                self.cache_hit(job, dict(hit), resolved, fingerprint, cache_key)
                return
        cap = payload.get("capability")
        fn = executor_for(cap, job["step"])
        if fn is None:
            self.fail(job, StepFailure(f"Aucun exécuteur pour {cap}/{job['step']}", code="no_executor"))
            return
        attempt_payload = payload
        retried = None
        for attempt in (1, 2):
            ctx = StepContext(db=self.db, storage=self.storage, cfg=self.cfg, job={**job, "payload": attempt_payload},
                              templates=self.templates, services=self.services)
            ctx.params = attempt_payload.get("params") or {}
            ctx.inputs = resolved
            t0 = time.time()
            try:
                ctx.stage("start", f"Démarrage : {payload['node'].get('label') or job['node_id']} / {job['step']}"
                                   + (f" [{job['item_key']}]" if job.get("item_key") else ""))
                result = fn(ctx)
                result.setdefault("stats", {})["seconds"] = round(time.time() - t0, 2)
                result["stats"]["vram_peak_mb"] = result["stats"].get("peak_vram_mb")
                if retried:
                    result.setdefault("engine", {})["quality_reduction"] = retried
                self.succeed(job, ctx, result, resolved, fingerprint, cache_key, eng_fp, step_params)
                return
            except Cancelled as e:
                self.jobs.transition(job["id"], "cancelled", error={"code": "cancelled", "message": str(e)}, event="job_cancelled")
                self._cascade_block(job)
                self._finish_run_if_done(job.get("run_id"))
                return
            except StepFailure as e:
                if e.oom and attempt == 1 and cap in REDUCED_PROFILES:
                    reduced = REDUCED_PROFILES[cap]
                    ctx.log(f"OOM : libération mémoire et nouvelle tentative avec profil réduit {reduced}")
                    emit(self.db, "oom_retry", job_id=job["id"], run_id=job.get("run_id"), graph_id=job.get("graph_id"),
                         node_id=job.get("node_id"), item_id=job.get("item_id") or None, reduced_profile=reduced,
                         message="Mémoire insuffisante : nouvelle tentative unique avec un profil réduit (qualité inférieure)")
                    self.services.gpu_owner = None
                    try:
                        if cap.startswith(("image", "mesh")):
                            self.services.comfy("dedicated").free(unload_models=True, free_memory=True)
                    except Exception:
                        pass
                    attempt_payload = copy.deepcopy(payload)
                    attempt_payload["params"] = {**attempt_payload.get("params", {}), **reduced}
                    attempt_payload["engine_ref_reset"] = True
                    with self.db.tx() as c:
                        c.execute("UPDATE jobs SET engine_ref = NULL, attempt = attempt + 1 WHERE id = ?", (job["id"],))
                    job["engine_ref"] = None
                    retried = {"reason": "OOM", "profile": reduced, "message": str(e)}
                    time.sleep(3)
                    continue
                self.fail(job, e)
                return
            except Exception as e:  # erreur inattendue : structurée, jamais un faux « terminé »
                tb = traceback.format_exc()
                ctx.log(tb)
                self.fail(job, StepFailure(f"{e.__class__.__name__} : {e}", code="internal_error", details={"traceback": tb[-3000:]}))
                return

    def succeed(self, job, ctx, result, resolved, fingerprint, cache_key, eng_fp, step_params) -> None:
        vid = new_id("var")
        payload = job["payload"]
        outputs = result.get("outputs") or {}
        engine = {**eng_fp, **(result.get("engine") or {})}
        report = result.get("report") or {}
        meta = {"job_id": job["id"], "run_id": job.get("run_id"), "graph_revision": payload.get("graph_revision"),
                "node_id": job["node_id"], "node_type": job["node_type"], "step": job["step"], "item_key": job.get("item_key"),
                "seed": payload.get("seed"), "params": payload.get("params"), "inputs": resolved, "engine": engine,
                "stats": result.get("stats"), "cache_key": cache_key, "created_at": now(), "author": job["author"]}
        (ctx.dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if report:
            (ctx.dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        activated = False
        with self.db.tx() as c:
            c.execute("INSERT INTO variants(id, graph_id, node_id, step, item_id, job_id, run_id, status, author, params, seed, inputs, "
                      "input_fingerprint, cache_key, outputs, engine, stats, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (vid, job["graph_id"], job["node_id"], job["step"], job.get("item_id") or "", job["id"], job.get("run_id"), "succeeded",
                       job["author"], dumps(step_params), payload.get("seed"), dumps(resolved), fingerprint, cache_key, dumps(outputs),
                       dumps(engine), dumps(result.get("stats") or {}), now()))
            for ids in outputs.values():
                for aid in ids:
                    c.execute("UPDATE artifacts SET variant_id = ? WHERE id = ?", (vid, aid))
            emit(self.db, "variant_created", job_id=job["id"], run_id=job.get("run_id"), graph_id=job["graph_id"], node_id=job["node_id"],
                 item_id=job.get("item_id") or None, conn=c, step=job["step"], item_key=job.get("item_key"), variant_id=vid,
                 outputs=outputs, author=job["author"])
            if job["graph_id"]:
                activated = self._maybe_activate(c, job, vid)
            self.jobs.transition(job["id"], "succeeded", conn=c, result={"variant_id": vid, "activated": activated, "outputs": outputs,
                                                                         "report_summary": _summary(report), "stats": result.get("stats")})
        self._finish_run_if_done(job.get("run_id"))
        self._after_variant(job, vid, activated)

    def _after_variant(self, job, vid, activated) -> None:
        """Extraction de props : la liste détectée est écrite dans la node (si cette analyse devient la sortie utilisée),
        puis l'extraction reportée est lancée (nouveau run, même portée)."""
        if job.get("node_type") != "image.props" or not job.get("graph_id") or not activated:
            return
        from . import props as P

        try:
            res = P.apply_analysis(self.db, job["graph_id"], job["node_id"], vid, job["author"])
            emit(self.db, "props_analyzed", graph_id=job["graph_id"], node_id=job["node_id"], job_id=job["id"], **(res or {}))
            cont = (job["payload"] or {}).get("continue_run")
            if cont and res and res.get("enabled"):
                from . import capabilities
                from .graph.model import GraphStore
                from .graph.planner import Planner

                planner = Planner(self.db, GraphStore(self.db), self.state, capabilities_fn=lambda: capabilities.flat(self.cfg, self.db),
                                  aliases_fn=lambda: self.cfg.get("workflows.aliases", {}) or {})
                r = planner.create_run(job["graph_id"], scope=cont.get("scope") or "graph", node_id=cont.get("node_id"),
                                       author=job["author"], jobs_service=self.jobs)
                emit(self.db, "run_continued", graph_id=job["graph_id"], node_id=job["node_id"], run_id=r.get("run_id"),
                     jobs=len(r.get("jobs") or []), message=r.get("message"))
        except Exception as e:  # noqa: BLE001 — jamais d'échec de la tâche d'analyse elle-même
            emit(self.db, "warning", graph_id=job["graph_id"], node_id=job["node_id"], job_id=job["id"],
                 message=f"Extraction de props : suite automatique impossible ({type(e).__name__} : {e})")

    def _maybe_activate(self, c, job, vid) -> bool:
        pol = job["payload"].get("activate", "activate_first_success_only")
        r = c.execute("SELECT active_variant_id FROM slots WHERE graph_id = ? AND node_id = ? AND step = ? AND item_id = ?",
                      (job["graph_id"], job["node_id"], job["step"], job.get("item_id") or "")).fetchone()
        has = bool(r and r["active_variant_id"])
        if pol == "always" or (pol in ("activate_first_success_only", "never_if_exists") and not has):
            self.state.set_active(job["graph_id"], job["node_id"], job["step"], job.get("item_id") or "", vid, author=job["author"], conn=c,
                                  reason="politique : " + pol)
            return True
        return False

    def cache_hit(self, job, hit, resolved, fingerprint, cache_key) -> None:
        vid = new_id("var")
        outputs = loads(hit["outputs"], {})
        with self.db.tx() as c:
            c.execute("INSERT INTO variants(id, graph_id, node_id, step, item_id, job_id, run_id, status, author, params, seed, inputs, "
                      "input_fingerprint, cache_key, outputs, engine, stats, from_cache_of, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (vid, job["graph_id"], job["node_id"], job["step"], job.get("item_id") or "", job["id"], job.get("run_id"), "succeeded",
                       job["author"], hit["params"], hit["seed"], dumps(resolved), fingerprint, cache_key, hit["outputs"], hit["engine"],
                       dumps({"cached": True}), hit["id"], now()))
            emit(self.db, "cache_hit", job_id=job["id"], run_id=job.get("run_id"), graph_id=job["graph_id"], node_id=job["node_id"],
                 item_id=job.get("item_id") or None, conn=c, step=job["step"], item_key=job.get("item_key"), variant_id=vid,
                 reused_variant_id=hit["id"], message="Résultat réutilisé du cache (entrées, paramètres, seed et moteur identiques)")
            activated = self._maybe_activate(c, job, vid) if job["graph_id"] else False
            self.jobs.transition(job["id"], "succeeded", conn=c, result={"variant_id": vid, "activated": activated, "outputs": outputs,
                                                                         "from_cache_of": hit["id"], "cached": True})
        self._finish_run_if_done(job.get("run_id"))
        self._after_variant(job, vid, activated)

    def fail(self, job: dict, e: StepFailure) -> None:
        err = e.to_json()
        self.jobs.transition(job["id"], "failed", error=err)
        self._cascade_block(job)
        self._finish_run_if_done(job.get("run_id"))

    def _cascade_block(self, job: dict) -> None:
        if not job.get("run_id"):
            return
        for r in self.db.all("SELECT id, depends_on FROM jobs WHERE run_id = ? AND status = 'queued'", (job["run_id"],)):
            if job["id"] in loads(r["depends_on"], []):
                self.block(r["id"], [job["id"]], {job["id"]: "échec/annulé"})

    def _finish_run_if_done(self, run_id: str | None) -> None:
        finalize_run(self.db, run_id)

    # ------------------------------------------------------------------ réconciliation
    def reconcile(self) -> None:
        # runs restés ouverts alors qu'il ne reste rien à faire (annulations avant démarrage, anciennes versions) : clos
        for r in self.db.all("SELECT id FROM runs WHERE status IN ('queued', 'running')"):
            block_orphans(self.db, r["id"])
            finalize_run(self.db, r["id"])
        rows = self.db.all("SELECT * FROM jobs WHERE status = 'running' AND kind = 'graph_step'")
        for r in rows:
            pid = r["worker_pid"]
            alive = False
            if pid and pid != self.pid:
                try:
                    alive = psutil.pid_exists(pid) and "python" in psutil.Process(pid).name().lower()
                except psutil.Error:
                    alive = False
            if alive:
                continue
            ref = loads(r["engine_ref"], {}) or {}
            note = "Le worker s'est arrêté pendant l'étape : reprise depuis la dernière étape validée."
            if ref.get("engine") == "comfyui" and ref.get("prompt_id"):
                note += f" Le prompt ComfyUI {ref['prompt_id']} sera vérifié (collecte sans resoumission s'il est terminé)."
            with self.db.tx() as c:
                c.execute("UPDATE jobs SET status = 'queued', worker_pid = NULL, attempt = attempt + 1 WHERE id = ?", (r["id"],))
                emit(self.db, "job_reconciled", job_id=r["id"], run_id=r["run_id"], graph_id=r["graph_id"], node_id=r["node_id"],
                     item_id=r["item_id"] or None, conn=c, previous_status="running", engine_ref=ref, message=note)


def _summary(report: dict) -> dict:
    keep = {}
    for k in ("status", "verdict", "warnings", "prompt_final", "prompt_original", "status_label", "png_rgba_verified"):
        if k in report:
            keep[k] = report[k]
    return keep


def main() -> int:
    cfg = C.load()
    w = Worker(cfg)
    lock = cfg.run_dir / "worker.lock"
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            other = int(lock.read_text())
            if other != os.getpid() and psutil.pid_exists(other) and "python" in psutil.Process(other).name().lower():
                print(f"Un worker est déjà actif (PID {other})")
                return 1
        except (ValueError, psutil.Error):
            pass
    lock.write_text(str(os.getpid()))

    def _stop(*_):
        w.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"Worker Atelier 3D actif (PID {os.getpid()})", flush=True)
    try:
        w.serve()
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
