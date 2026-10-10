// Sélection, copier / couper / coller de nodes (Ctrl+C / Ctrl+X / Ctrl+V, Ctrl+Maj+V avec les connexions entrantes).
// Le presse-papiers système transporte un JSON autonome : on peut coller dans un autre graphe ou un autre onglet.
// Seules les définitions sont copiées (type, réglages, libellé, noodles) : les résultats et variantes restent sur l'original.
import { api, GEdge, GNode } from "./api";
import { applyOps, errText, openGraph, refreshGraphList } from "./actions";
import { getState, setState, toast } from "./store";

export type Clip = { atelier3d_clipboard: 1; nodes: GNode[]; edges: GEdge[]; incoming: GEdge[] };

let memory: Clip | null = null; // repli si le presse-papiers système est indisponible
let anchor: () => { x: number; y: number } | null = () => null; // position de collage (souris sur le canevas)
let pasteWithInputs = false;
let repeat = 0;
let lastAt: { x: number; y: number } | null = null;
let stack = 0;

export function setPasteAnchor(fn: () => { x: number; y: number } | null) { anchor = fn; }

function editable(t: EventTarget | null) {
  const el = t as HTMLElement | null;
  return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
}

/** Nodes réellement sélectionnées (les previews automatiques d'items foreach sont recréées par le graphe, pas copiées). */
export function selectedNodes(): GNode[] {
  const s = getState();
  if (!s.view) return [];
  const ids = new Set(s.selection.length ? s.selection : s.selected ? [s.selected] : []);
  return s.view.graph.nodes.filter((n) => ids.has(n.id) && !n.auto);
}

export function buildClip(nodes: GNode[] = selectedNodes()): Clip | null {
  const v = getState().view;
  if (!v || !nodes.length) return null;
  const ids = new Set(nodes.map((n) => n.id));
  const byId = new Map(v.graph.nodes.map((n) => [n.id, n]));
  const clean = (n: GNode): GNode => {
    const c: any = JSON.parse(JSON.stringify(n));
    delete c.items; delete c.auto; delete c.archived;
    return c;
  };
  const edge = (e: GEdge): GEdge => {
    const c: any = { from: [...e.from], to: [...e.to] };
    if (e.item_key) c.item_key = e.item_key;
    if (e.order !== undefined) c.order = e.order;
    return c; // pas de lien épinglé : la variante épinglée appartient à la node d'origine
  };
  const edges: GEdge[] = [], incoming: GEdge[] = [];
  for (const e of v.graph.edges) {
    if (e.auto || !ids.has(e.to[0])) continue;
    const src = byId.get(e.from[0]);
    if (ids.has(e.from[0])) edges.push(edge(e));
    else if (src?.auto && ids.has(src.auto.owner_node_id)) {
      // sortie d'une preview automatique dont la node productrice est copiée : on rebranche sur le port de l'item
      edges.push({ ...edge(e), from: [src.auto.owner_node_id, `image:${src.auto.item_key}`] });
    } else incoming.push(edge(e));
  }
  return { atelier3d_clipboard: 1, nodes: nodes.map(clean), edges, incoming };
}

function parseClip(text: string | undefined | null): Clip | null {
  if (!text) return null;
  try {
    const d = JSON.parse(text);
    if (d?.atelier3d_clipboard === 1 && Array.isArray(d.nodes)) return d as Clip;
    // un graph.json complet (export) peut aussi être collé : ses nodes et noodles internes sont ajoutées
    if (Array.isArray(d?.nodes) && Array.isArray(d?.edges)) {
      const nodes = d.nodes.filter((n: any) => n?.id && n?.type && !n.auto);
      const ids = new Set(nodes.map((n: any) => n.id));
      return { atelier3d_clipboard: 1, nodes, edges: d.edges.filter((e: any) => !e.auto && ids.has(e.from?.[0]) && ids.has(e.to?.[0])), incoming: [] };
    }
  } catch { /* pas un JSON d'Atelier 3D */ }
  return null;
}

/** Ajoute les nodes du presse-papiers au graphe ouvert (nouveaux identifiants, noodles internes rebranchées). */
export async function pasteClip(clip: Clip, opts: { withInputs?: boolean; offset?: number } = {}) {
  const v = getState().view;
  if (!v) { toast("Ouvrez ou créez d'abord un graphe", "error"); return; }
  const existing = new Set(v.graph.nodes.map((n) => n.id));
  const map = new Map<string, string>();
  for (const n of clip.nodes) {
    const base = n.id.replace(/(_copie(_\d+)?)$/, "").replace(/_\d+$/, "") || "node";
    let i = 2, id = `${base}_${i}`;
    while (existing.has(id)) id = `${base}_${++i}`;
    existing.add(id);
    map.set(n.id, id);
  }
  const xs = clip.nodes.map((n) => n.position?.x || 0), ys = clip.nodes.map((n) => n.position?.y || 0);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const at = opts.offset === undefined ? anchor() : null;
  const shift = opts.offset ?? 60 * ++repeat;
  // collages successifs au même endroit : décalés pour ne pas s'empiler
  const same = at && lastAt && Math.abs(at.x - lastAt.x) < 2 && Math.abs(at.y - lastAt.y) < 2;
  stack = same ? stack + 1 : 0;
  lastAt = at;
  const dx = at ? at.x - minX + 40 * stack : shift, dy = at ? at.y - minY + 40 * stack : shift;
  const ops: any[] = clip.nodes.map((n) => ({
    op: "add_node",
    node: { ...n, id: map.get(n.id), position: { x: Math.round((n.position?.x || 0) + dx), y: Math.round((n.position?.y || 0) + dy) } },
  }));
  for (const e of clip.edges) {
    if (!map.has(e.from[0]) || !map.has(e.to[0])) continue;
    ops.push({ op: "add_edge", edge: { ...e, from: [map.get(e.from[0]), e.from[1]], to: [map.get(e.to[0]), e.to[1]] } });
  }
  let kept = 0;
  if (opts.withInputs) {
    for (const e of clip.incoming) {
      if (!v.graph.nodes.some((n) => n.id === e.from[0]) || !map.has(e.to[0])) continue;
      ops.push({ op: "add_edge", edge: { ...e, to: [map.get(e.to[0]), e.to[1]] } });
      kept++;
    }
  }
  try {
    await applyOps(ops);
    const ids = [...map.values()];
    setState({ selection: ids, selected: ids[ids.length - 1] || null });
    toast(`${ids.length} node(s) collée(s)${kept ? `, ${kept} connexion(s) entrante(s) conservée(s)` : ""} — résultats non copiés : à calculer`, "ok");
  } catch { /* erreur déjà affichée par applyOps */ }
}

export function copySelection(target?: DataTransfer | null, verb = "copiée(s)"): Clip | null {
  const clip = buildClip();
  if (!clip) { toast("Aucune node sélectionnée à copier", "info"); return null; }
  memory = clip;
  repeat = 0;
  const txt = JSON.stringify(clip);
  if (target) target.setData("text/plain", txt);
  else navigator.clipboard?.writeText(txt).catch(() => undefined);
  toast(`${clip.nodes.length} node(s) ${verb}`, "info");
  return clip;
}

export async function cutSelection(target?: DataTransfer | null) {
  const nodes = selectedNodes();
  if (!copySelection(target, "coupée(s)")) return;
  repeat = -1; // le premier collage reprend la position d'origine
  try {
    await applyOps(nodes.map((n) => ({ op: "remove_node", node_id: n.id })));
    setState({ selection: [], selected: null });
  } catch { /* erreur déjà affichée */ }
}

/** Bouton « Coller » : dernière copie faite dans cet onglet ; sinon lecture du presse-papiers système (le navigateur
 * peut alors demander l'autorisation, ce que le raccourci Ctrl+V évite). */
export async function pasteFromButton(withInputs = false) {
  let clip: Clip | null = memory;
  if (!clip) {
    try { clip = parseClip(await navigator.clipboard.readText()); } catch { /* lecture refusée */ }
  }
  if (!clip) { toast("Presse-papiers vide : copiez d'abord des nodes (Ctrl+C)", "info"); return; }
  await pasteClip(clip, { withInputs });
}

export async function duplicateSelection() {
  const clip = buildClip();
  if (!clip) { toast("Aucune node sélectionnée", "info"); return; }
  await pasteClip(clip, { offset: 40 }); // ne touche pas au presse-papiers
}

/** Supprime les nodes ET les noodles sélectionnées, en une seule modification (une seule étape d'annulation). */
export async function deleteSelection() {
  const s = getState();
  if (!s.view) return;
  const nodes = selectedNodes();
  const gone = new Set(nodes.map((n) => n.id));
  const edges = s.view.graph.edges.filter((e) => s.edgeSelection.includes(e.id) && !gone.has(e.from[0]) && !gone.has(e.to[0]));
  const ops = [...nodes.map((n) => ({ op: "remove_node", node_id: n.id })), ...edges.map((e) => ({ op: "remove_edge", edge_id: e.id }))];
  if (!ops.length) return;
  try {
    await applyOps(ops);
    setState({ selection: [], selected: null, edgeSelection: [] });
  } catch { /* erreur déjà affichée */ }
}

export function selectAll() {
  const v = getState().view;
  if (!v) return;
  const ids = v.graph.nodes.filter((n) => !n.archived).map((n) => n.id);
  setState({ selection: ids, selected: ids[ids.length - 1] || null });
}

export async function createBlankGraph() {
  const d = new Date();
  const name = `Graphe vierge ${d.toLocaleDateString("fr-FR")} ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
  try {
    const r = await api.createGraph({ schema_version: "1.1", name, nodes: [], edges: [] });
    await refreshGraphList();
    await openGraph(r.graph.id);
    if (window.innerWidth >= 900) setState((s) => ({ panels: { ...s.panels, library: true } })); // tiroir sur écran étroit : laissé fermé
    toast("Graphe vierge créé : ajoutez des nodes depuis la bibliothèque ou collez-en (Ctrl+V). Renommez-le dans l'inspecteur.", "ok");
  } catch (e) { toast("Création impossible : " + errText(e), "error"); }
}

/** Raccourcis : Ctrl+C / Ctrl+X traités à l'appui de touche (Chrome n'émet pas toujours « copy » sans texte sélectionné) ;
 * Ctrl+V lit l'événement « paste » (aucune autorisation demandée), avec repli sur la dernière copie faite ici. */
let pendingPaste: any = null;

function textSelected() { return !!window.getSelection()?.toString(); }

export function installClipboard() {
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || editable(e.target) || !getState().view || getState().mediaOpen || !!getState().mediaPick) return;
    const k = e.key.toLowerCase();
    if ((k === "c" || k === "x") && !textSelected() && selectedNodes().length) {
      e.preventDefault();
      if (k === "c") copySelection(); else cutSelection();
    }
    if (k === "v") {
      pasteWithInputs = e.shiftKey;
      clearTimeout(pendingPaste);
      // si l'événement « paste » n'arrive pas (navigateur, focus), on colle la dernière copie faite dans cet onglet
      pendingPaste = setTimeout(() => { pendingPaste = null; if (memory) pasteClip(memory, { withInputs: pasteWithInputs }); }, 120);
    }
  }, true);
  document.addEventListener("copy", (e) => {
    if (editable(e.target) || textSelected() || !getState().view || !selectedNodes().length) return;
    e.preventDefault();
    copySelection(e.clipboardData);
  });
  document.addEventListener("cut", (e) => {
    if (editable(e.target) || textSelected() || !getState().view || !selectedNodes().length) return;
    e.preventDefault();
    cutSelection(e.clipboardData);
  });
  document.addEventListener("paste", (e) => {
    if (editable(e.target) || !getState().view || getState().mediaOpen || !!getState().mediaPick) return;
    clearTimeout(pendingPaste);
    pendingPaste = null;
    const txt = e.clipboardData?.getData("text/plain");
    const clip = parseClip(txt) || (!txt ? memory : null);
    if (!clip) return;
    e.preventDefault();
    const withInputs = pasteWithInputs;
    pasteWithInputs = false;
    pasteClip(clip, { withInputs });
  });
}
