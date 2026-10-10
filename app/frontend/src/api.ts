// Client de l'API locale (même origine, cookie de session posé par la page).

export type PortDef = { name: string; type: string; type_label: string; label: string; multi: boolean; required: boolean; dynamic_prefix?: string; description?: string };
export type NodeTypeDef = {
  type: string; label: string; category: string; kind: string; description: string;
  inputs: PortDef[]; outputs: PortDef[]; params_schema: any; actions: string[]; execution_modes: Record<string, any>;
  default_steps: { id: string; label: string; capability: string; resource: string; generative: boolean; status: string }[];
};
export type GNode = {
  id: string; type: string; label?: string; params: Record<string, any>; position?: { x: number; y: number };
  execution?: Record<string, any>; presentation?: Record<string, any>; items?: Item[]; auto?: { owner_node_id: string; item_id: string; item_key: string }; archived?: boolean;
};
export type Item = { item_id: string; item_key: string; label?: string; order?: number; edge_id?: string; status?: string };
export type GEdge = { id: string; from: [string, string]; to: [string, string]; item_key?: string; order?: number; link?: { mode: string; variant_id?: string }; auto?: boolean };
export type GraphDoc = { id: string; name: string; revision: number; schema_version: string; nodes: GNode[]; edges: GEdge[]; groups?: { id: string; label: string; node_ids: string[]; collapsed?: boolean }[] };
export type VariantLite = { id: string; author: string; created_at: string; outputs: Record<string, string[]>; from_cache_of?: string | null; seed?: number | null; job_id?: string; run_id?: string;
  texts?: string[] }; // texts : prompts écrits par « Prompts aléatoires »
export type JobLite = { id: string; status: string; stage?: string; progress?: any; error?: any; author: string; created_at: string; run_id?: string };
export type SlotView = { node_id: string; step: string; item_id: string | null; active_variant_id: string | null; variants: VariantLite[]; jobs: JobLite[] };
export type GraphView = { graph: GraphDoc; status: Record<string, any>; validation: { errors: any[]; warnings: any[]; valid: boolean }; slots: Record<string, SlotView> };

export type MediaItem = {
  artifact_id: string; kind: "image" | "video" | "mesh"; role: string; url: string; thumb_url: string | null; name: string; size: number;
  created_at: string; job_id: string; graph_id: string | null; graph_name: string | null; node_id: string; node_label: string;
  node_type: string; step: string; item_id: string | null; item_key: string | null; author: string; prompt: string | null;
  seed: number | null; variant_id: string | null; active: boolean;
};

export class ApiError extends Error {
  status: number; payload: any;
  constructor(status: number, payload: any) {
    const msg = payload?.error?.message || payload?.detail || JSON.stringify(payload);
    const details = payload?.error?.details;
    super(details && Array.isArray(details) ? `${msg} — ${details.map((d: any) => d.message || JSON.stringify(d)).join(" ; ")}` : msg);
    this.status = status; this.payload = payload;
  }
}

async function req<T = any>(method: string, path: string, body?: any): Promise<T> {
  const init: RequestInit = { method, headers: { "X-Atelier3D-Author": "utilisateur" }, credentials: "same-origin" };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.body = JSON.stringify(body); (init.headers as any)["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  const txt = await r.text();
  let data: any = txt;
  try { data = txt ? JSON.parse(txt) : null; } catch { /* texte brut */ }
  if (!r.ok) throw new ApiError(r.status, data);
  return data as T;
}

export const api = {
  health: () => req("GET", "/health"),
  capabilities: (refresh = false) => req("GET", `/api/v1/capabilities?refresh=${refresh}`),
  nodeTypes: () => req<{ node_types: NodeTypeDef[]; port_types: Record<string, string>; compatibility: Record<string, string[]> }>("GET", "/api/v1/node-types"),
  presets: () => req<{ presets: Record<string, string> }>("GET", "/api/v1/presets"),
  fromPreset: (p: string, opts: any = {}) => req<{ graph: GraphDoc }>("POST", `/api/v1/presets/${p}`, opts),
  graphs: (archived = false) => req<{ graphs: any[] }>("GET", `/api/v1/graphs${archived ? "?archived=true" : ""}`),
  deleteGraph: (id: string) => req("DELETE", `/api/v1/graphs/${id}`),
  restoreGraph: (id: string) => req("POST", `/api/v1/graphs/${id}/restore`),
  graph: (id: string) => req<GraphView>("GET", `/api/v1/graphs/${id}`),
  createGraph: (doc: any) => req<GraphView>("POST", "/api/v1/graphs", { graph: doc }),
  patch: (id: string, ops: any[], expected_revision?: number) => req<GraphView & { changes: string[] }>("PATCH", `/api/v1/graphs/${id}`, { ops, expected_revision }),
  plan: (id: string, body: any = {}) => req("POST", `/api/v1/graphs/${id}/plan`, body),
  run: (id: string, body: any = {}) => req("POST", `/api/v1/graphs/${id}/runs`, body),
  rerunNode: (id: string, node: string, body: any) => req("POST", `/api/v1/graphs/${id}/nodes/${node}/rerun`, body),
  rerunItem: (id: string, node: string, item: string, body: any) => req("POST", `/api/v1/graphs/${id}/nodes/${node}/items/${item}/rerun`, body),
  cancelItem: (id: string, node: string, item: string) => req("POST", `/api/v1/graphs/${id}/nodes/${node}/items/${item}/cancel`),
  resumeItem: (id: string, node: string, item: string) => req("POST", `/api/v1/graphs/${id}/nodes/${node}/items/${item}/resume`),
  items: (id: string, node: string) => req<{ items: any[]; mode: string }>("GET", `/api/v1/graphs/${id}/nodes/${node}/items`),
  variants: (id: string, node: string) => req<{ variants: any[]; active: any[] }>("GET", `/api/v1/graphs/${id}/nodes/${node}/variants`),
  selectVariant: (id: string, node: string, variant_id: string, extra: any = {}) => req("POST", `/api/v1/graphs/${id}/nodes/${node}/select-variant`, { variant_id, ...extra }),
  jobs: (graph_id?: string) => req<{ jobs: any[] }>("GET", `/api/v1/jobs${graph_id ? `?graph_id=${graph_id}` : ""}`),
  job: (id: string) => req("GET", `/api/v1/jobs/${id}`),
  jobLog: async (id: string) => (await fetch(`/api/v1/jobs/${id}/log`, { credentials: "same-origin" })).text(),
  cancel: (id: string) => req("POST", `/api/v1/jobs/${id}/cancel`),
  resume: (id: string) => req("POST", `/api/v1/jobs/${id}/resume`),
  artifacts: (id: string) => req<{ artifacts: any[] }>("GET", `/api/v1/jobs/${id}/artifacts`),
  artifact: (id: string) => req("GET", `/api/v1/artifacts/${id}`),
  openInBlender: (jobId: string, artifact_id?: string) => req("POST", `/api/v1/jobs/${jobId}/open-in-blender`, artifact_id ? { artifact_id } : {}),
  openFolder: (artifactId: string) => req("POST", `/api/v1/artifacts/${artifactId}/open-folder`),
  upload: (file: File) => { const fd = new FormData(); fd.append("file", file); return req("POST", "/api/v1/uploads", fd); },
  settings: () => req<any>("GET", "/api/v1/settings"),
  remoteSettings: () => req<any>("GET", "/api/v1/settings/remote"),
  putRemote: (body: any) => req<any>("PUT", "/api/v1/settings/remote", body),
  newRemoteCode: () => req<any>("POST", "/api/v1/settings/remote/code"),
  putOpenai: (body: any) => req<any>("PUT", "/api/v1/settings/openai", body),
  testOpenai: () => req<any>("POST", "/api/v1/settings/openai/test"),
  deleteOpenaiKey: () => req<any>("DELETE", "/api/v1/settings/openai/key"),
  uploadFromArtifact: (artifact_id: string) => req<any>("POST", "/api/v1/uploads/from-artifact", { artifact_id }),
  getUpload: (id: string) => req<any>("GET", `/api/v1/uploads/${id}`),
  system: () => req("GET", "/api/v1/system"),
  media: (p: { kind?: string; q?: string; graph_id?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(p).forEach(([k, v]) => { if (v !== undefined && v !== "") qs.set(k, String(v)); });
    return req<{ items: MediaItem[]; total: number; counts: Record<string, number> }>("GET", `/api/v1/media?${qs}`);
  },
};

export const artUrl = (id: string, inline = true) => `/api/v1/artifacts/${id}/download${inline ? "?inline=true" : ""}`;
export const uploadUrl = (id: string) => `/api/v1/uploads/${id}/download`;
