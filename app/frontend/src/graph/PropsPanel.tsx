// Extraction de props : réglages d'analyse, description du personnage et liste des pièces détectées (cocher, renommer,
// régénérer, supprimer, ajouter). Chaque pièce cochée = un item du groupe « Props » (une image isolée + sa preview).
import { useState } from "react";
import type { GNode } from "../api";
import { applyOps, randomSeed, rerunItem, runScope } from "../actions";
import { STATUS_LABEL, useStore } from "../store";
import { slug } from "./ports";

type Part = { key: string; name: string; kind?: string; symmetric?: boolean; description?: string; enabled?: boolean; manual?: boolean };

function Seg<T extends string>({ value, options, onChange }: { value: T; options: [T, string, string?][]; onChange: (v: T) => void }) {
  return (
    <div className="seg nodrag">
      {options.map(([v, label, sub]) => (
        <button key={v} className={v === value ? "on" : ""} onClick={() => v !== value && onChange(v)}>
          {label}{sub && <small>{sub}</small>}
        </button>
      ))}
    </div>
  );
}

export function PropsPanel({ n, slots, job }: { n: GNode; slots: any[]; job?: any }) {
  const p: any = n.params || {};
  const parts: Part[] = p.parts || [];
  const groupId: string | undefined = (n as any).props_group?.node_id;
  const group = useStore((s) => s.view?.graph.nodes.find((x) => x.id === groupId));
  const groupStatus = useStore((s) => (groupId ? s.view?.status?.[groupId] : undefined));
  // sélecteur STABLE (l'objet du store) ; filtrer dans le sélecteur créerait un tableau neuf à chaque lecture → boucle de rendu
  const allSlots = useStore((s) => s.view?.slots);
  const groupJobs = (groupId ? Object.values(allSlots || {}).filter((x: any) => x.node_id === groupId) : []) as any[];
  const [adding, setAdding] = useState("");
  const set = (params: any) => applyOps([{ op: "update_node", node_id: n.id, params }]).catch(() => undefined);
  const setParts = (next: Part[]) => set({ parts: next });
  const itemOf = (key: string) => (group?.items || []).find((i) => i.item_key === key && i.status !== "archived");
  const itemState = (key: string) => {
    const it = itemOf(key);
    if (!it) return null;
    const jobs = groupJobs.filter((x) => x.item_id === it.item_id).flatMap((x) => x.jobs || []);
    const j = jobs.sort((a, b) => (a.created_at < b.created_at ? -1 : 1))[jobs.length - 1];
    if (j && ["running", "queued", "failed", "blocked"].includes(j.status)) return j.status;
    return groupStatus?.slots?.find((x: any) => x.item_id === it.item_id)?.status || "missing";
  };
  const engine = p.engine || "openai";
  const enabled = parts.filter((x) => x.enabled !== false);
  const analysing = job && ["running", "queued"].includes(job.status);
  const add = () => {
    const name = adding.trim();
    if (!name) return;
    let key = slug(name), k = 2;
    while (parts.some((x) => x.key === key)) key = `${slug(name)}_${k++}`;
    setParts([...parts, { key, name, kind: ["head", "body"].includes(key) ? "body" : "prop", symmetric: false, description: "", enabled: true, manual: true }]);
    setAdding("");
  };
  void slots;
  return (
    <div className="props-panel nodrag nowheel">
      <div className="props-opts">
        <div className="row"><span>Inclure</span>
          <Seg value={p.include || "props_body"} onChange={(v) => set({ include: v })}
            options={[["props", "Props seuls"], ["props_body", "Props + corps"]]} /></div>
        <div className="row"><span>Détail</span>
          <Seg value={p.detail || "standard"} onChange={(v) => set({ detail: v })}
            options={[["minimal", "Minimal", "3–6"], ["standard", "Standard", "6–15"], ["detailed", "Détaillé", "15+"]]} /></div>
        <label className="row toggle"><span>Fusionner les pièces symétriques</span>
          <input type="checkbox" className="switch" checked={p.merge_symmetric !== false} onChange={(e) => set({ merge_symmetric: e.target.checked })} /></label>
        <div className="row"><span>Analyse</span>
          {engine === "openai" ? <span className="pill ext" title="L'image est envoyée à OpenAI (service externe, facturé)">☁ ChatGPT</span>
            : engine === "claude" ? <span className="pill ext" title="L'image est envoyée à Anthropic (service externe, facturé)">☁ Claude</span>
            : <span className="pill" title="Qwen3-VL 4B sur ce PC">Qwen3-VL · local</span>}
          {group && <span className="pill" title="Moteur des images extraites (node « Props »)">{group.params?.engine === "openai" ? "extraction ☁ OpenAI" : "extraction Krea 2"}</span>}
        </div>
      </div>
      <div className="props-char">
        <div className="lbl">Personnage</div>
        <div title={p.character || ""}>{p.character || <i>{analysing ? "Analyse en cours…" : "Cliquez « Analyser » : la liste des pièces apparaîtra ici."}</i>}</div>
      </div>
      {parts.length > 0 && (
        <button className="linkbtn" onClick={() => setParts(parts.map((x) => ({ ...x, enabled: enabled.length === 0 })))}>
          {enabled.length ? "Tout désélectionner" : "Tout sélectionner"}</button>
      )}
      <div className="props-list">
        {parts.map((x, i) => {
          const st = x.enabled !== false ? itemState(x.key) : null;
          const it = itemOf(x.key);
          return (
            <div key={x.key} className={`prop-row ${x.enabled === false ? "off" : ""}`} title={x.description || ""}>
              <input type="checkbox" className="switch" checked={x.enabled !== false}
                onChange={(e) => setParts(parts.map((y, j) => (j === i ? { ...y, enabled: e.target.checked } : y)))} />
              <input className="pname-edit" defaultValue={x.name} key={`${x.key}:${x.name}`}
                onBlur={(e) => { const v = e.target.value.trim(); if (v && v !== x.name) setParts(parts.map((y, j) => (j === i ? { ...y, name: v } : y))); }}
                onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
              {x.symmetric && <span className="sym" title="Pièce symétrique : une seule est extraite (l'autre est son miroir)">⇋</span>}
              {st && <span className={`dot st-${st}`} title={STATUS_LABEL[st] || st} />}
              <button className="mini icon" disabled={!it || !groupId} title="Régénérer cette pièce (nouvelle tentative, candidate)"
                onClick={() => it && groupId && rerunItem(groupId, it.item_id, { scope: "item_only", params_patch: { seed: randomSeed() }, force_recompute: true }, `Régénérer « ${x.name} »`)}>↻</button>
              <button className="mini icon" title="Supprimer cette pièce de la liste" onClick={() => setParts(parts.filter((_, j) => j !== i))}>🗑</button>
            </div>
          );
        })}
      </div>
      <div className="prop-add">
        <input placeholder="+ Ajouter une pièce (ex. lanterne)" value={adding} onChange={(e) => setAdding(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        {adding.trim() && <button className="mini" onClick={add}>Ajouter</button>}
      </div>
      <div className="props-foot">
        {analysing ? (job.stage || "Analyse en attente…")
          : parts.length ? `${enabled.length} pièce${enabled.length > 1 ? "s" : ""} cochée${enabled.length > 1 ? "s" : ""} → ${enabled.length} image${enabled.length > 1 ? "s" : ""} isolée${enabled.length > 1 ? "s" : ""} + preview${enabled.length > 1 ? "s" : ""}`
            : "Analyse : description du personnage et pièces extractibles."}
      </div>
      <div className="snode-actions">
        <button onClick={() => runScope({ scope: "until", node_id: n.id }, "Analyser l'image")}
          title="Envoie l'image à l'analyse et remplit la liste des pièces">Analyser</button>
        <button className="primary" disabled={!groupId}
          onClick={() => groupId && runScope({ scope: "until", node_id: groupId }, "Extraire les props")}
          title="Une image isolée par pièce cochée ; si l'analyse manque ou n'est plus à jour, elle est faite d'abord">Extraire les props</button>
      </div>
    </div>
  );
}
