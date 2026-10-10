// Médiathèque : toutes les images, vidéos et modèles 3D générés (tous graphes), avec recherche et accès à la node d'origine.
import { useEffect, useRef, useState } from "react";
import { api, artUrl, MediaItem } from "../api";
import { applyOps, errText, openGraph } from "../actions";
import { getState, setState, toast, useStore } from "../store";

const TABS: [string, string][] = [["all", "Tout"], ["image", "Images"], ["video", "Vidéos"], ["mesh", "3D"]];
const PAGE = 60;
const STEP_LABEL: Record<string, string> = {
  create: "création", edit: "édition", remove_background: "détourée", crop: "cadrée", generate: "générée", repair: "réparée",
  simplify: "simplifiée", bake: "pré-calculée", bind: "riggée", articulated_rig: "riggée (articulée)", export: "export",
};

function fmtDate(s: string) {
  const d = new Date(s);
  return `${d.toLocaleDateString("fr-FR")} ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
}

async function openViewer(it: MediaItem) {
  const title = `${it.node_label}${it.item_key ? ` · ${it.item_key}` : ""} — ${it.graph_name || "graphe supprimé"}`;
  if (it.kind === "image") setState({ modal: { kind: "image", title, urls: [{ url: artUrl(it.artifact_id), label: it.name }] } });
  else if (it.kind === "mesh") setState({ modal: { kind: "mesh", title, url: artUrl(it.artifact_id) } });
  else {
    let frames: { url: string; t: number }[] = [];
    try {
      frames = (await api.artifacts(it.job_id)).artifacts.filter((a) => a.role === "video_frame")
        .map((a) => ({ url: artUrl(a.id), t: Number(a.meta?.time_s || 0) })).sort((a, b) => a.t - b.t);
    } catch { /* frames facultatives */ }
    setState({ modal: { kind: "video", title, url: artUrl(it.artifact_id), frames } });
  }
}

async function goToNode(it: MediaItem) {
  if (!it.graph_id) return;
  try {
    await openGraph(it.graph_id);
    setState({ selected: it.node_id, selection: [it.node_id], mediaOpen: false });
  } catch (e) { toast("Graphe introuvable : " + errText(e), "error"); }
}

/** Mode « choisir » : le résultat devient le fichier source de la node Importer (copie, provenance conservée). */
async function pickFor(it: MediaItem) {
  const pk = getState().mediaPick;
  if (!pk) return;
  try {
    const up = await api.uploadFromArtifact(it.artifact_id);
    await applyOps([{ op: "update_node", node_id: pk.nodeId, params: { upload_id: up.id } }]);
    setState({ mediaPick: null, mediaOpen: false });
    toast(`« ${pk.label} » utilise maintenant ${it.node_label}${it.item_key ? ` · ${it.item_key}` : ""} (médiathèque)`, "ok");
  } catch (e) { toast("Import impossible : " + errText(e), "error"); }
}

function Card({ it, picking }: { it: MediaItem; picking: boolean }) {
  const remote = useStore((s) => !!s.settings?.client?.remote); // appareil distant : pas d'action sur l'écran du PC
  return (
    <div className={`media-card ${picking ? "picking" : ""}`}>
      <button className="media-thumb" onClick={() => (picking ? pickFor(it) : openViewer(it))} title={picking ? "Utiliser ce résultat" : "Ouvrir"}>
        {it.thumb_url ? <img src={it.thumb_url} alt="" loading="lazy" /> : <span className="media-ph">{it.kind === "mesh" ? "3D" : "▶"}</span>}
        {it.kind === "video" && <span className="media-kind">▶ vidéo</span>}
        {it.kind === "mesh" && <span className="media-kind">3D · {STEP_LABEL[it.step] || it.step}</span>}
        <span className={`media-state ${it.active ? "on" : ""}`}>{it.active ? "utilisée" : "variante"}</span>
      </button>
      <div className="media-meta">
        <div className="media-title" title={it.prompt || ""}>{it.node_label}{it.item_key ? ` · ${it.item_key}` : ""}</div>
        <div className="hint">{it.graph_name || "graphe supprimé"} · {fmtDate(it.created_at)}{it.author === "Claude Code" ? " · Claude Code" : ""}</div>
        {it.prompt && <div className="media-prompt" title={it.prompt}>{it.prompt}</div>}
        <div className="media-actions">
          {picking && <button className="mini primary" onClick={() => pickFor(it)}>Utiliser</button>}
          {picking && <button className="mini" onClick={() => openViewer(it)} title="Voir en grand">🔍</button>}
          {!picking && it.graph_id && <button className="mini" onClick={() => goToNode(it)} title="Ouvrir le graphe et sélectionner la node d'origine">↗ Node</button>}
          <a className="mini btnlike" href={it.url} download title="Télécharger">⇩</a>
          {!remote && <button className="mini" onClick={() => api.openFolder(it.artifact_id).catch((e) => toast(errText(e), "error"))} title="Ouvrir le dossier">📁</button>}
        </div>
      </div>
    </div>
  );
}

export function MediaLibrary() {
  const pick = useStore((s) => s.mediaPick);
  const open = useStore((s) => s.mediaOpen) || !!pick;
  const view = useStore((s) => s.view);
  const [tabState, setTab] = useState("all");
  const tab = pick ? pick.kind : tabState; // en mode « choisir », seul le type accepté par la node
  const [q, setQ] = useState("");
  const [onlyGraph, setOnlyGraph] = useState(false);
  const [items, setItems] = useState<MediaItem[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(false);
  const reqId = useRef(0);

  const load = async (offset = 0) => {
    const id = ++reqId.current;
    setLoading(true);
    try {
      const r = await api.media({ kind: tab, q, graph_id: onlyGraph ? view?.graph.id : undefined, limit: PAGE, offset });
      if (id !== reqId.current) return; // une recherche plus récente a été lancée
      setItems((prev) => (offset ? [...prev, ...r.items] : r.items));
      setTotal(r.total);
      setCounts(r.counts);
    } catch (e) { toast("Médiathèque : " + errText(e), "error"); }
    finally { if (id === reqId.current) setLoading(false); }
  };

  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => load(0), q ? 250 : 0);
    return () => clearTimeout(t);
  }, [open, tab, q, onlyGraph, view?.graph.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const close = () => setState({ mediaOpen: false, mediaPick: null });

  if (!open) return null;
  const all = Object.values(counts).reduce((a, b) => a + b, 0);
  return (
    <div className="media-overlay" onKeyDown={(e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      if (getState().modal) setState({ modal: null }); else close();
    }}>
      <div className="media-head">
        <b>{pick ? `Choisir ${pick.kind === "mesh" ? "un modèle 3D" : pick.kind === "video" ? "une vidéo" : "une image"} pour « ${pick.label} »` : "Médiathèque"}</b>
        <div className="seg">
          {TABS.filter(([k]) => !pick || k === pick.kind).map(([k, l]) => (
            <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l} ({k === "all" ? all : counts[k] ?? 0})</button>
          ))}
        </div>
        <input className="media-search" autoFocus placeholder="Rechercher (prompt, node, graphe, item…)" value={q} onChange={(e) => setQ(e.target.value)} />
        {view && <label className="hint"><input type="checkbox" checked={onlyGraph} onChange={(e) => setOnlyGraph(e.target.checked)} /> ce graphe seulement</label>}
        <button className="mini" onClick={() => load(0)} title="Actualiser">↻</button>
        <span className="grow" />
        <button onClick={close}>{pick ? "Annuler" : "Fermer"}</button>
      </div>
      <div className="media-grid">
        {items.map((it) => <Card key={it.artifact_id} it={it} picking={!!pick} />)}
        {!items.length && !loading && <p className="hint">Aucun résultat{q ? ` pour « ${q} »` : ""}.</p>}
      </div>
      <div className="media-foot">
        <span className="hint">{items.length} / {total} affichés · {pick ? "cliquez un résultat pour l'utiliser (une copie est importée, sa provenance est conservée)" : "« utilisée » = sortie active de sa node (transmise en aval)"}</span>
        {items.length < total && <button onClick={() => load(items.length)} disabled={loading}>Charger plus</button>}
        {loading && <span className="hint">chargement…</span>}
      </div>
    </div>
  );
}
