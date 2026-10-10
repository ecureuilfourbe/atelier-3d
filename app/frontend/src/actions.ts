// Actions : chargement, modifications du graphe (avec annuler/rétablir), lancements et flux d'événements.
import { api, ApiError, GraphDoc } from "./api";
import { getState, setState, toast } from "./store";

export function errText(e: any) { return e instanceof ApiError ? e.message : String(e?.message || e); }

/** Toast d'erreur ; si la cause est la clé OpenAI absente/refusée, propose d'ouvrir directement les Réglages. */
export function errToast(e: any, prefix = "") {
  const text = prefix + errText(e);
  toast(text, "error", /Réglages → OpenAI|openai_(key_missing|auth)/.test(text + JSON.stringify((e as any)?.payload || ""))
    ? { label: "⚙ Réglages", run: () => setState({ settingsOpen: true }) } : undefined);
}

export async function bootstrap() {
  try {
    const [nt, caps, gl, tr, st] = await Promise.all([api.nodeTypes(), api.capabilities(), api.graphs(), api.graphs(true),
      api.settings().catch(() => null)]);
    setState({ settings: st });
    const map: Record<string, any> = {};
    nt.node_types.forEach((t) => (map[t.type] = t));
    setState({ nodeTypes: map, compatibility: nt.compatibility, portTypes: nt.port_types, capabilities: caps, graphs: gl.graphs, trash: tr.graphs });
    const qs = new URLSearchParams(location.search);
    const last = qs.get("graph") || localStorageGet("a3d_graph");
    const target = gl.graphs.find((g) => g.id === last) || gl.graphs[0];
    if (target) await openGraph(target.id);
    if (qs.get("node")) setState({ selected: qs.get("node") });
  } catch (e) { toast("Chargement impossible : " + errText(e), "error"); }
  setInterval(refreshSystem, 3000);
  refreshSystem();
}

function localStorageGet(k: string) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k: string, v: string) { try { localStorage.setItem(k, v); } catch { /* ignoré */ } }

let sessionWarned = false;
async function refreshSystem() {
  try { setState({ system: await api.system() }); sessionWarned = false; }
  catch (e) {
    // 401 : session du navigateur refusée (ex. jeton local régénéré) -> le dire une fois au lieu d'échouer en silence
    if (e instanceof ApiError && e.status === 401 && !sessionWarned) {
      sessionWarned = true;
      toast("Session du navigateur refusée par le serveur : rechargez la page (F5).", "error");
    }
  }
}

export async function refreshCaps() {
  try { setState({ capabilities: await api.capabilities(true) }); } catch (e) { toast(errText(e), "error"); }
}

export async function openGraph(id: string) {
  if (!id) return; // identifiant vide : GET /graphs/ renverrait la LISTE des graphes, pas un graphe
  const v = await api.graph(id);
  if (!v?.graph) throw new Error(`Graphe ${id} illisible`);
  try { const h: any = await api.health(); setState({ lastEventId: h.events_last_id || 0 }); } catch { /* ignoré */ }
  setState({ view: v, selected: null, history: { past: [], future: [] } });
  localStorageSet("a3d_graph", id);
  connectEvents(id);
  refreshJobs();
}

export async function refreshGraph() {
  const v = getState().view;
  if (!v) return;
  try { setState({ view: await api.graph(v.graph.id) }); } catch (e) { /* silencieux */ }
}

export async function refreshJobs() {
  const v = getState().view;
  try { setState({ jobs: (await api.jobs(v?.graph.id)).jobs }); } catch { /* ignoré */ }
}

export async function refreshGraphList() {
  const [g, t] = await Promise.all([api.graphs(), api.graphs(true)]);
  setState({ graphs: g.graphs, trash: t.graphs });
}

/** Suppression réversible (corbeille), sans confirmation ; « Annuler » dans le message la défait. Si c'est le graphe
 * ouvert, on ouvre le suivant de la liste. */
export async function deleteGraph(id: string) {
  const s = getState();
  const g = s.graphs.find((x) => x.id === id);
  const name = g?.name || id;
  // pas de confirmation : la suppression est réversible (corbeille) et le message propose « Annuler »
  try {
    await api.deleteGraph(id);
    await refreshGraphList();
    if (s.view?.graph.id === id) {
      const next = getState().graphs[0];
      if (next) await openGraph(next.id);
      else setState({ view: null, selected: null, selection: [], edgeSelection: [] });
    }
    toast(`« ${name} » supprimé (corbeille de la bibliothèque)`, "ok", { label: "Annuler", run: () => restoreGraph(id) });
  } catch (e) { toast("Suppression impossible : " + errText(e), "error"); }
}

export async function restoreGraph(id: string) {
  try {
    await api.restoreGraph(id);
    await refreshGraphList();
    await openGraph(id);
    toast("Graphe restauré", "ok");
  } catch (e) { toast("Restauration impossible : " + errText(e), "error"); }
}

/** Applique des opérations PATCH ; conserve l'état précédent pour annuler. */
export async function applyOps(ops: any[], opts: { silent?: boolean; history?: boolean } = {}) {
  const v = getState().view;
  if (!v) return;
  const before = v.graph;
  try {
    const r = await api.patch(v.graph.id, ops, v.graph.revision);
    setState((s) => ({ view: r, history: opts.history === false ? s.history : { past: [...s.history.past.slice(-49), before], future: [] },
      graphs: s.graphs.map((g) => (g.id === r.graph.id ? { ...g, name: r.graph.name, revision: r.graph.revision } : g)) }));
    return r;
  } catch (e: any) {
    if (e instanceof ApiError && e.status === 409) {
      toast("Le graphe a été modifié ailleurs (révision différente) : rechargement.", "error");
      await refreshGraph();
    } else if (!opts.silent) toast(errText(e), "error");
    throw e;
  }
}

function docForReplace(d: GraphDoc) {
  return { schema_version: "1.1", name: d.name, nodes: d.nodes, edges: d.edges, groups: d.groups || [] };
}

export async function undo() {
  const s = getState();
  const prev = s.history.past[s.history.past.length - 1];
  if (!prev || !s.view) return;
  const cur = s.view.graph;
  try {
    const r = await api.patch(cur.id, [{ op: "replace", doc: docForReplace(prev) }], cur.revision);
    setState((st) => ({ view: r, history: { past: st.history.past.slice(0, -1), future: [cur, ...st.history.future] } }));
  } catch (e) { toast(errText(e), "error"); }
}

export async function redo() {
  const s = getState();
  const next = s.history.future[0];
  if (!next || !s.view) return;
  const cur = s.view.graph;
  try {
    const r = await api.patch(cur.id, [{ op: "replace", doc: docForReplace(next) }], cur.revision);
    setState((st) => ({ view: r, history: { past: [...st.history.past, cur], future: st.history.future.slice(1) } }));
  } catch (e) { toast(errText(e), "error"); }
}

export async function runScope(body: any, label: string) {
  const v = getState().view;
  if (!v) return;
  try {
    const r: any = await api.run(v.graph.id, { ...body, expected_graph_revision: v.graph.revision });
    const reused = (r.adopted?.length ? ` · ${r.adopted.length} résultat(s) déjà calculé(s) avec ces réglages réutilisé(s), sans recalcul` : "")
      + (r.continue_after_analysis ? " · l'extraction des props sera lancée automatiquement après l'analyse" : "")
      + ((r.warnings || []).some((w: any) => w.code === "skipped_invalid")
        ? ` · ignorée(s), incomplète(s) : ${[...new Set((r.warnings || []).filter((w: any) => w.code === "skipped_invalid").map((w: any) => w.node_id))].join(", ")}` : "");
    if (!r.run_id) toast((r.adopted?.length ? "Tout est à jour" : r.message || "Rien à exécuter.") + reused, "info");
    else toast(`${label} : ${r.jobs.length} étape(s) planifiée(s)${r.skipped?.length ? `, ${r.skipped.length} à jour réutilisée(s)` : ""}${reused}`, "ok");
    refreshGraph(); refreshJobs();
  } catch (e) { errToast(e); }
}

export async function rerunNode(nodeId: string, body: any, label: string) {
  const v = getState().view;
  if (!v) return;
  try {
    const r: any = await api.rerunNode(v.graph.id, nodeId, { ...body, expected_graph_revision: v.graph.revision });
    toast(`${label} : ${r.jobs?.length || 0} tâche(s)`, "ok");
    refreshGraph(); refreshJobs();
  } catch (e) { errToast(e); }
}

export async function rerunItem(nodeId: string, itemId: string, body: any, label: string) {
  const v = getState().view;
  if (!v) return;
  try {
    const r: any = await api.rerunItem(v.graph.id, nodeId, itemId, { ...body, expected_graph_revision: v.graph.revision });
    toast(`${label} : ${r.jobs?.length || 0} tâche(s)`, "ok");
    refreshGraph(); refreshJobs();
  } catch (e) { errToast(e); }
}

export async function selectVariant(nodeId: string, variantId: string, extra: any = {}) {
  const v = getState().view;
  if (!v) return;
  try {
    const r: any = await api.selectVariant(v.graph.id, nodeId, variantId, extra);
    toast(r.newly_stale?.length ? `Variante utilisée ; à recalculer : ${r.newly_stale.join(", ")}` : "Variante utilisée", "ok");
    if (r.restored_chain?.length) toast(`Chaîne restaurée par provenance : ${r.restored_chain.length} étape(s)`, "ok");
    refreshGraph();
  } catch (e) { toast(errText(e), "error"); }
}

export const randomSeed = () => Math.floor(Math.random() * 2 ** 31);

// --------------------------------------------------------------------------- événements (SSE, rejouables)
let es: EventSource | null = null;
let refreshTimer: any = null;

function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; refreshGraph(); refreshJobs(); }, 350);
}

export function connectEvents(graphId: string) {
  if (es) es.close();
  const after = getState().lastEventId;
  es = new EventSource(`/api/v1/events?graph_id=${encodeURIComponent(graphId)}&after=${after}`);
  es.onmessage = () => { /* les événements typés sont traités ci-dessous */ };
  const handle = (ev: MessageEvent) => {
    const e = JSON.parse(ev.data);
    const latency = Date.now() - Date.parse(e.ts);
    // délai mesuré uniquement sur les événements récents (les événements rejoués après reconnexion sont exclus)
    setState((s) => ({ lastEventId: Math.max(s.lastEventId, e.id), eventLatencyMs: latency < 60000 ? latency : s.eventLatencyMs }));
    if (e.type === "progress" || e.type === "stage") {
      setState((s) => ({ liveProgress: { ...s.liveProgress, [e.job_id]: { ...(s.liveProgress[e.job_id] || {}), ...(e.type === "progress" ? { progress: e.data } : { stage: e.data.message }), at: Date.now() } } }));
      return;
    }
    if (e.type === "live_preview") {
      setState((s) => ({ livePreview: { ...s.livePreview, [`${e.node_id}|${e.item_id || ""}`]: e.data.url } }));
      return;
    }
    if (e.type === "engine_node" || e.type === "engine_cached_nodes") return;
    if (e.type === "props_analyzed" && e.id > after) toast(`Analyse terminée : ${e.data?.parts ?? "?"} pièce(s) détectée(s)`, "ok");
    if (e.type === "run_continued" && e.id > after) toast(e.data?.run_id ? `Extraction des props lancée : ${e.data.jobs} étape(s)` : `Extraction : ${e.data?.message || "rien à faire"}`, "ok");
    scheduleRefresh();
  };
  ["job_queued", "job_started", "job_succeeded", "job_failed", "job_cancelled", "job_blocked", "cancel_requested", "variant_created",
    "active_output_changed", "graph_updated", "run_created", "run_finished", "cache_hit", "artifact_created", "progress", "stage",
    "live_preview", "oom_retry", "job_resumed", "job_reconciled", "engine_switch", "warning", "resume_collect", "props_analyzed",
    "run_continued"].forEach((t) => es!.addEventListener(t, handle as any));
  es.onerror = () => { /* EventSource se reconnecte seul avec Last-Event-ID */ };
}
