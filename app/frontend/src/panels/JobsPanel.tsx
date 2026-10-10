import { useEffect, useState } from "react";
import { api } from "../api";
import { errText, refreshJobs } from "../actions";
import { setState, STATUS_LABEL, toast, useStore } from "../store";

function elapsed(j: any) {
  if (!j.started_at) return "";
  const end = j.finished_at ? Date.parse(j.finished_at) : Date.now();
  const s = Math.max(0, Math.round((end - Date.parse(j.started_at)) / 1000));
  return s > 90 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
}

export function JobsPanel() {
  const jobs = useStore((s) => s.jobs);
  const live = useStore((s) => s.liveProgress);
  const lat = useStore((s) => s.eventLatencyMs);
  const [open, setOpen] = useState<string | null>(null);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 1000); return () => clearInterval(t); }, []);
  useEffect(() => { const t = setInterval(refreshJobs, 5000); return () => clearInterval(t); }, []);
  return (
    <div className="jobs">
      <div className="jobs-head"><b>Tâches</b><span className="hint">délai des événements : {lat != null ? `${lat} ms` : "—"}</span></div>
      {jobs.length === 0 && <p className="hint">Aucune tâche pour ce graphe.</p>}
      {jobs.map((j) => (
        <div key={j.id} className={`job st-${j.status}`}>
          <div className="jrow" onClick={() => setOpen(open === j.id ? null : j.id)}>
            <span className={`origin ${j.origin === "Claude Code" ? "claude" : ""}`}>{j.origin}</span>
            <b>{j.kind === "graph_run" ? "Exécution du graphe" : j.kind}</b>
            <span className={`badge st-${j.status}`}>{STATUS_LABEL[j.status] || j.status}</span>
            <span className="hint">{j.summary}</span>
            <span className="grow" />
            <span className="hint">{elapsed(j)}</span>
            {j.final_status?.label && !["en cours", "terminé"].includes(j.final_status.label) && <span className="pill">{j.final_status.label}</span>}
            {["queued", "running"].includes(j.status) && <button className="mini danger" onClick={(e) => { e.stopPropagation(); api.cancel(j.id).then(() => toast("Annulation demandée", "info")).catch((er) => toast(errText(er), "error")); }}>Annuler</button>}
            {["failed", "partial", "cancelled", "interrupted"].includes(j.status) && <button className="mini" onClick={(e) => { e.stopPropagation(); api.resume(j.id).then(() => { toast("Reprise depuis la dernière étape validée", "ok"); refreshJobs(); }); }}>Reprendre</button>}
          </div>
          {j.current && (
            <div className="jcur">{j.current.node_id}{j.current.item_key ? ` [${j.current.item_key}]` : ""} · {live[j.children?.find((c: any) => c.status === "running")?.job_id]?.stage || j.current.stage || "traitement en cours"}
              {(() => { const c = j.children?.find((x: any) => x.status === "running"); const p = c && (live[c.job_id]?.progress || c.progress); return p && p.max ? ` · ${p.value}/${p.max}` : ""; })()}
            </div>
          )}
          {open === j.id && (
            <div className="jkids">
              {(j.children || []).map((c: any) => (
                <div key={c.job_id} className={`kid st-${c.status}`}>
                  <span>{c.node_id}{c.item_key ? ` [${c.item_key}]` : ""} · {c.step}</span>
                  <span className={`badge st-${c.status}`}>{STATUS_LABEL[c.status] || c.status}</span>
                  {c.result?.cached && <span className="pill">cache</span>}
                  <span className="hint">{c.error?.message || c.stage || ""}</span>
                  <button className="mini" onClick={async () => setState({ modal: { kind: "text", title: `Journal ${c.job_id}`, text: await api.jobLog(c.job_id) } })}>journal</button>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
