// État applicatif minimal (sans dépendance) : abonnement + instantané immuable.
import { useSyncExternalStore } from "react";
import type { GraphView, NodeTypeDef } from "./api";

export type Toast = { id: number; kind: "info" | "error" | "ok"; text: string; action?: { label: string; run: () => void } };
export type Modal =
  | { kind: "mesh"; title: string; url: string; compareUrl?: string; compareTitle?: string; baseTitle?: string }
  | { kind: "image"; title: string; urls: { url: string; label: string }[] }
  | { kind: "video"; title: string; url: string; frames: { url: string; t: number }[] }
  | { kind: "json"; title: string; data: any }
  | { kind: "text"; title: string; text: string }
  | { kind: "compare"; title: string; before: any; after: any; mode: "slider" | "side_by_side" | "difference" }
  | null;

export type State = {
  nodeTypes: Record<string, NodeTypeDef>;
  compatibility: Record<string, string[]>;
  portTypes: Record<string, string>;
  capabilities: any;
  graphs: any[];
  trash: any[]; // graphes supprimés (restaurables)
  view: GraphView | null;
  selected: string | null; // node affichée dans l'inspecteur (sélection principale)
  selection: string[]; // toutes les nodes sélectionnées (multi-sélection)
  edgeSelection: string[]; // noodles sélectionnées (clic, Ctrl+clic) — supprimées par Suppr
  selectMode: boolean; // true : glisser = rectangle de sélection ; false : glisser = déplacer la vue
  jobs: any[];
  liveProgress: Record<string, any>; // job_id -> progrès
  livePreview: Record<string, string>; // node_id|item -> url
  eventLatencyMs: number | null;
  lastEventId: number;
  toasts: Toast[];
  modal: Modal;
  system: any;
  panels: { library: boolean; inspector: boolean; jobs: boolean };
  mediaOpen: boolean; // médiathèque (images, vidéos, modèles 3D générés)
  settingsOpen: boolean; // panneau ⚙ Réglages
  settings: any; // réglages non secrets (la clé d'API n'y figure jamais)
  mediaPick: { kind: "image" | "mesh" | "video"; nodeId: string; label: string } | null; // médiathèque ouverte pour alimenter une node Importer
  history: { past: any[]; future: any[] };
};

let state: State = {
  nodeTypes: {}, compatibility: {}, portTypes: {}, capabilities: null, graphs: [], trash: [], view: null, selected: null, selection: [], edgeSelection: [],
  selectMode: (() => { try { return localStorage.getItem("a3d_select_mode") === "1"; } catch { return false; } })(), jobs: [],
  liveProgress: {}, livePreview: {}, eventLatencyMs: null, lastEventId: 0, toasts: [], modal: null, system: null,
  panels: { library: true, inspector: true, jobs: true }, history: { past: [], future: [] }, mediaOpen: false, mediaPick: null, settingsOpen: false, settings: null,
};
const subs = new Set<() => void>();

export function getState() { return state; }
export function setState(patch: Partial<State> | ((s: State) => Partial<State>)) {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  subs.forEach((f) => f());
}
export function useStore<T>(sel: (s: State) => T): T {
  return useSyncExternalStore((cb) => { subs.add(cb); return () => subs.delete(cb); }, () => sel(state));
}

let toastId = 1;
/** `action` : bouton dans le message (ex. « Annuler » après une suppression) ; le message reste alors affiché plus longtemps. */
export function toast(text: string, kind: Toast["kind"] = "info", action?: Toast["action"]) {
  const id = toastId++;
  setState((s) => ({ toasts: [...s.toasts, { id, kind, text, action }] }));
  setTimeout(() => setState((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), action ? 10000 : kind === "error" ? 9000 : 4500);
}

export const STATUS_LABEL: Record<string, string> = {
  ok: "À jour", stale: "À recalculer", missing: "Jamais calculé", upstream: "Amont à recalculer", partial: "Partiel",
  queued: "En attente", running: "En cours", succeeded: "Terminé", failed: "Échec", blocked: "Bloqué", cancelled: "Annulé",
  interrupted: "Interrompu", cancel_requested: "Annulation demandée",
};
