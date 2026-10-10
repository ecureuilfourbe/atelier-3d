import { useEffect, useMemo, useState } from "react";
import { api, artUrl, GNode, uploadUrl } from "../api";
import { applyOps, errText, randomSeed, refreshGraph, rerunItem, rerunNode, runScope, selectVariant } from "../actions";
import { copySelection, cutSelection, deleteSelection, duplicateSelection } from "../clipboard";
import { IMAGE_GEN } from "../graph/ports";
import { getState, setState, STATUS_LABEL, toast, useStore } from "../store";

function Field({ name, schema, value, onChange }: { name: string; schema: any; value: any; onChange: (v: any) => void }) {
  const title = schema.title || name;
  if (schema.enum && schema.type !== "array") {
    return (
      <label className="field"><span>{title}</span>
        <select value={value ?? schema.default ?? ""} onChange={(e) => onChange(schema.type === "integer" ? parseInt(e.target.value) : e.target.value)}>
          {schema.enum.map((o: any) => <option key={o} value={o}>{schema.enumLabels?.[o] || o}</option>)}
        </select></label>
    );
  }
  if (schema.type === "boolean") {
    return <label className="field check"><input type="checkbox" checked={!!(value ?? schema.default)} onChange={(e) => onChange(e.target.checked)} /><span>{title}</span></label>;
  }
  if (schema.type === "integer" || schema.type === "number") {
    return (
      <label className="field"><span>{title}</span>
        <input type="number" value={value ?? schema.default ?? ""} min={schema.minimum} max={schema.maximum} step={schema.multipleOf || (schema.type === "number" ? 0.01 : 1)}
          onChange={(e) => onChange(e.target.value === "" ? null : schema.type === "integer" ? parseInt(e.target.value) : parseFloat(e.target.value))} />
        {name === "seed" && <button className="mini" onClick={() => onChange(randomSeed())}>🎲</button>}
      </label>
    );
  }
  if (schema.type === "string") {
    if (schema.format === "textarea" || name === "text") {
      return <label className="field"><span>{title}</span><textarea rows={6} defaultValue={value ?? ""} onBlur={(e) => e.target.value !== (value ?? "") && onChange(e.target.value)} /></label>;
    }
    return <label className="field"><span>{title}</span><input defaultValue={value ?? schema.default ?? ""} onBlur={(e) => e.target.value !== (value ?? "") && onChange(e.target.value)} /></label>;
  }
  if (schema.type === "array" && schema.items?.enum) {
    const cur: string[] = value ?? schema.default ?? [];
    return (
      <div className="field"><span>{title}</span><div className="row">
        {schema.items.enum.map((o: string) => (
          <label key={o} className="check"><input type="checkbox" checked={cur.includes(o)} onChange={(e) => onChange(e.target.checked ? [...cur, o] : cur.filter((x) => x !== o))} />{o}</label>
        ))}</div></div>
    );
  }
  if (schema.type === "object" && schema.properties) {
    const cur = value ?? {};
    return (
      <fieldset className="field"><legend>{title}</legend>
        {Object.entries(schema.properties).map(([k, s]: any) => <Field key={k} name={k} schema={s} value={cur[k]} onChange={(v) => onChange({ ...cur, [k]: v })} />)}
      </fieldset>
    );
  }
  return (
    <label className="field"><span>{title} (JSON)</span>
      <textarea rows={4} defaultValue={value != null ? JSON.stringify(value, null, 1) : ""} onBlur={(e) => {
        try { onChange(e.target.value.trim() ? JSON.parse(e.target.value) : null); } catch { toast("JSON invalide", "error"); }
      }} /></label>
  );
}

function Params({ node }: { node: GNode }) {
  const nt = useStore((s) => s.nodeTypes[node.type]);
  const [adv, setAdv] = useState(false);
  if (!nt) return null;
  const props = Object.entries(nt.params_schema.properties || {}) as [string, any][];
  const set = (k: string, v: any) => applyOps([{ op: "update_node", node_id: node.id, params: { [k]: v } }]).catch(() => undefined);
  const basic = props.filter(([, s]) => !s["x-advanced"] && !s["x-hidden"]);
  const advanced = props.filter(([, s]) => s["x-advanced"] && !s["x-hidden"]);
  const val = (k: string) => node.params?.[k] ?? nt.params_schema.properties?.[k]?.default;
  const engine = val("engine") || "krea2";
  const visible = (k: string) => {
    const s = nt.params_schema.properties?.[k];
    if (s?.["x-engine"] && s["x-engine"] !== engine) return false; // réglages propres à l'autre moteur
    const cond = s?.["x-show-if"] as Record<string, any[]> | undefined; // ex. réglages de pose seulement si une pose est choisie
    if (cond && !Object.entries(cond).every(([pk, vals]) => vals.includes(val(pk)))) return false;
    if (["image.import", "mesh.import", "video.import"].includes(node.type) && k === "upload_id") return false;
    return true;
  };
  const field = (k: string, s: any) => s["x-claude-models"]
    ? <ClaudeModelField key={k} value={node.params?.[k] || ""} onChange={(v) => set(k, v)} />
    : s["x-openai-models"] || s["x-openai-text-models"]
    ? <OpenAIModelField key={k} value={node.params?.[k] || ""} onChange={(v) => set(k, v)}
        purpose={s["x-openai-text-models"] ? "text" : node.type === "image.generate" ? "generate" : "edit"} />
    : <Field key={k} name={k} schema={s} value={node.params?.[k]} onChange={(v) => set(k, v)} />;
  return (
    <div className="section">
      <h4>Réglages</h4>
      {IMAGE_GEN.includes(node.type) && engine === "openai" && (
        <p className="warn">Moteur OpenAI : {node.type === "image.edit" ? "l'image et la consigne sont envoyées" : "la consigne est envoyée"} à OpenAI
          (service externe, facturé sur votre compte). Clé : ⚙ Réglages.</p>
      )}
      {node.type === "image.props" && engine === "openai" && (
        <p className="warn">Analyse par OpenAI (ChatGPT) : l'image est envoyée à OpenAI (service externe, facturé sur votre compte). Clé : ⚙ Réglages.
          Pour tout garder sur ce PC : « Analyse par » → Qwen3-VL 4B (local).</p>
      )}
      {node.type === "text.randomize" && engine === "openai" && (
        <p className="warn">Écriture par OpenAI : le contexte est envoyé à OpenAI (service externe, facturé sur votre compte). Clé : ⚙ Réglages.</p>
      )}
      {node.type === "text.randomize" && engine === "claude" && (
        <p className="warn">Écriture par Claude : le contexte est envoyé à Anthropic (service externe, facturé sur votre compte). Clé : ⚙ Réglages.</p>
      )}
      {node.type === "image.props" && engine === "claude" && (
        <p className="warn">Analyse par Claude : l'image est envoyée à Anthropic (service externe, facturé sur votre compte). Clé : ⚙ Réglages.
          Les images extraites restent produites par Krea 2 sur ce PC.</p>
      )}
      {node.type === "image.prepare" && engine === "openai" && ["a_pose", "t_pose"].includes(val("pose")) && (
        <p className="warn">Pose par OpenAI : l'image est envoyée à OpenAI (service externe, facturé sur votre compte). Clé : ⚙ Réglages.</p>
      )}
      {basic.filter(([k]) => visible(k)).map(([k, s]) => field(k, s))}
      {advanced.length > 0 && (
        <details open={adv} onToggle={(e) => setAdv((e.target as HTMLDetailsElement).open)}>
          <summary>Réglages avancés (paramètres réellement pris en charge par le moteur)</summary>
          {advanced.filter(([k]) => visible(k)).map(([k, s]) => field(k, s))}
        </details>
      )}
    </div>
  );
}

function ClaudeModelField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const a = useStore((s) => s.settings?.anthropic) || {};
  const models: string[] = a.models || [];
  return (
    <label className="field"><span>Modèle Claude</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">par défaut des Réglages{a.model ? ` (${a.model})` : ""}</option>
        {models.map((m) => <option key={m} value={m}>{m}</option>)}
        {value && !models.includes(value) && <option value={value}>{value}</option>}
      </select></label>
  );
}

function OpenAIModelField({ value, purpose, onChange }: { value: string; purpose: "generate" | "edit" | "text"; onChange: (v: string) => void }) {
  const o = useStore((s) => s.settings?.openai) || {};
  const models: string[] = (purpose === "text" ? o.text_models : o.models) || [];
  const def = purpose === "text" ? o.model_text : purpose === "edit" ? o.model_edit : o.model_generate;
  return (
    <label className="field"><span>{purpose === "text" ? "Modèle OpenAI (texte / vision)" : "Modèle OpenAI"}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">par défaut des Réglages{def ? ` (${def})` : ""}</option>
        {models.map((m) => <option key={m} value={m}>{m}</option>)}
        {value && !models.includes(value) && <option value={value}>{value}</option>}
      </select></label>
  );
}

function Execution({ node }: { node: GNode }) {
  const nt = useStore((s) => s.nodeTypes[node.type]);
  if (!IMAGE_GEN.includes(node.type) || !nt) return null;
  const ex = node.execution || { mode: "single" };
  const setEx = (patch: any) => applyOps([{ op: "update_node", node_id: node.id, execution: { ...ex, ...patch },
    ...(patch.mode === "foreach_prompts" && !node.presentation?.auto_previews ? { presentation: { auto_previews: "per_item" } } : {}) }]).catch(() => undefined);
  return (
    <div className="section">
      <h4>Exécution</h4>
      <label className="field"><span>Mode de liste</span>
        <select value={ex.mode || "single"} onChange={(e) => setEx({ mode: e.target.value })}>
          {Object.entries(nt.execution_modes).map(([k, m]: any) => <option key={k} value={k} disabled={!m.implemented}>{m.label}{m.implemented ? "" : " — non intégré"}</option>)}
        </select></label>
      <p className="hint">{(nt.execution_modes[ex.mode || "single"] as any)?.description || (nt.execution_modes[ex.mode || "single"] as any)?.reason}</p>
      {ex.mode === "foreach_prompts" && (
        <>
          <label className="field"><span>Nombre maximal d'items</span><input type="number" min={1} max={64} defaultValue={ex.max_items ?? 16} onBlur={(e) => setEx({ max_items: parseInt(e.target.value) })} /></label>
          <label className="field"><span>Si un item échoue</span>
            <select value={ex.on_item_error || "continue"} onChange={(e) => setEx({ on_item_error: e.target.value })}>
              <option value="continue">continuer les autres items</option></select></label>
          <label className="field check"><input type="checkbox" checked={node.presentation?.auto_previews === "per_item"}
            onChange={(e) => applyOps([{ op: "update_node", node_id: node.id, presentation: { auto_previews: e.target.checked ? "per_item" : "none" } }]).catch(() => undefined)} />
            <span>Créer une node Preview par item</span></label>
          <p className="hint">Une variante par item (5 prompts → 5 résultats). Les seeds d'item dérivent de la seed de base et de l'item_key (stables au réordonnancement).</p>
        </>
      )}
    </div>
  );
}

function VariantCard({ v, active, onUse, onPick, picked, nodeId }: any) {
  const o = v.outputs || {};
  const img = (o.image || o.previews || o.frames || [])[0];
  return (
    <div className={`vcard ${active ? "active" : ""} ${picked ? "picked" : ""}`}>
      {img ? <img src={artUrl(img)} onClick={() => onPick(v)} /> : <div className="noimg" onClick={() => onPick(v)}>{Object.keys(o).join(", ") || "—"}</div>}
      <div className="vmeta">
        <span className={`author ${v.author === "Claude Code" ? "claude" : ""}`}>{v.author}</span>
        {v.seed != null && <span>seed {v.seed}</span>}
        {v.from_cache_of && <span className="pill">réutilisé du cache</span>}
        <span title={v.created_at}>{new Date(v.created_at).toLocaleTimeString()}</span>
      </div>
      <div className="row">
        {active ? <b className="activetag">Sortie active</b> : <button onClick={() => onUse(v)}>Utiliser</button>}
        <ArtifactsMenu v={v} nodeId={nodeId} />
      </div>
    </div>
  );
}

function ArtifactsMenu({ v }: { v: any; nodeId: string }) {
  const remote = useStore((s) => !!s.settings?.client?.remote); // appareil distant : pas d'action sur l'écran du PC
  const [arts, setArts] = useState<any[] | null>(null);
  const load = async () => {
    const ids = Object.values(v.outputs || {}).flat() as string[];
    setArts(await Promise.all(ids.map((i) => api.artifact(i))));
  };
  const open3d = (a: any) => setState({ modal: { kind: "mesh", title: a.path.split("/").pop(), url: artUrl(a.id) } });
  return (
    <details className="arts" onToggle={(e) => (e.target as HTMLDetailsElement).open && !arts && load()}>
      <summary>Fichiers</summary>
      {arts?.map((a) => (
        <div key={a.id} className="art">
          <span title={a.path}>{a.role} · {a.path.split("/").pop()}</span>
          {a.path.endsWith(".glb") && <button className="mini" onClick={() => open3d(a)}>3D</button>}
          {a.kind === "video" && <button className="mini" onClick={() => setState({ modal: { kind: "video", title: "Vidéo", url: artUrl(a.id), frames: [] } })}>▶</button>}
          {a.kind === "report" && <button className="mini" onClick={async () => setState({ modal: { kind: "json", title: a.path.split("/").pop(), data: await (await fetch(artUrl(a.id))).json() } })}>voir</button>}
          <a className="mini" href={artUrl(a.id, false)}>⇩</a>
          {!remote && <button className="mini" title="Ouvrir le dossier" onClick={() => api.openFolder(a.id).catch((e) => toast(errText(e), "error"))}>📁</button>}
          {!remote && a.path.endsWith(".blend") && <button className="mini" onClick={() => api.openInBlender(a.job_id, a.id).then(() => toast("Ouverture dans Blender…", "ok")).catch((e) => toast(errText(e), "error"))}>Blender</button>}
        </div>
      ))}
    </details>
  );
}

function SlotsSection({ node }: { node: GNode }) {
  const view = useStore((s) => s.view)!;
  const nt = useStore((s) => s.nodeTypes[node.type]);
  const [pick, setPick] = useState<any[]>([]);
  const status = view.status?.[node.id];
  const slots = Object.values(view.slots || {}).filter((s) => s.node_id === node.id && !s.item_id);
  const steps = nt?.default_steps || [];
  const stepIds = (status?.slots || []).filter((s: any) => !s.item_id).map((s: any) => s.step);
  const pickV = (v: any) => setPick((p) => (p.find((x) => x.id === v.id) ? p.filter((x) => x.id !== v.id) : [...p.slice(-1), v]));
  const compare = () => {
    const urls = pick.map((v) => ({ url: artUrl((v.outputs.image || v.outputs.previews || [])[0]), label: `${v.author} · ${v.seed ?? ""} · ${v.id}` }));
    setState({ modal: { kind: "image", title: "Comparer les variantes", urls } });
  };
  const outgoing = view.graph.edges.filter((e) => e.from[0] === node.id).map((e) => e.id);
  if (!stepIds.length) return null;
  return (
    <div className="section">
      <h4>Sous-étapes, variantes et sorties</h4>
      {stepIds.map((sid: string) => {
        const meta = steps.find((s) => s.id === sid);
        const sl = slots.find((s) => s.step === sid);
        const ss = (status?.slots || []).find((s: any) => s.step === sid && !s.item_id);
        return (
          <div key={sid} className="stepbox">
            <div className="stephead">
              <b>{meta?.label || sid}</b> <span className={`badge st-${ss?.status}`}>{STATUS_LABEL[ss?.status] || ss?.status}</span>
              {ss?.reasons && <span className="hint"> ({ss.reasons.join(" ; ")})</span>}
              <span className="grow" />
              {stepIds.length > 1 && <button className="mini" onClick={() => rerunNode(node.id, { scope: "step_only", step: sid, force_recompute: true }, `Recalculer « ${meta?.label || sid} »`)}>Recalculer cette sous-étape</button>}
            </div>
            <div className="vgrid">
              {(sl?.variants || []).slice().reverse().map((v) => (
                <VariantCard key={v.id} v={v} nodeId={node.id} active={v.id === sl?.active_variant_id} picked={!!pick.find((x) => x.id === v.id)}
                  onPick={pickV} onUse={(vv: any) => selectVariant(node.id, vv.id)} />
              ))}
              {!sl?.variants?.length && <p className="hint">Aucune variante.</p>}
            </div>
          </div>
        );
      })}
      <div className="row">
        <button disabled={pick.length !== 2} onClick={compare}>Comparer (2)</button>
        <button disabled={pick.length !== 1 || !outgoing.length} title="Épingle les liens sortants sur cette variante précise"
          onClick={() => selectVariant(node.id, pick[0].id, { pin_edges: outgoing })}>Épingler pour les liens sortants</button>
        <button disabled={pick.length !== 1} title="Sélectionne aussi les descendants compatibles (provenance)"
          onClick={() => selectVariant(node.id, pick[0].id, { cascade: true })}>Utiliser + restaurer la chaîne</button>
      </div>
    </div>
  );
}

function ItemsSection({ node }: { node: GNode }) {
  const view = useStore((s) => s.view)!;
  const [items, setItems] = useState<any[]>([]);
  const load = () => api.items(view.graph.id, node.id).then((r) => setItems(r.items)).catch(() => undefined);
  useEffect(() => { load(); }, [view]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (node.execution?.mode !== "foreach_prompts") return null;
  const running = items.filter((i) => i.jobs?.[0]?.status === "running").length;
  const queued = items.filter((i) => i.jobs?.[0]?.status === "queued").length;
  const done = items.filter((i) => i.status_slot === "ok").length;
  return (
    <div className="section">
      <h4>Items ({done} à jour / {items.filter((i) => i.status === "active").length}{running ? `, ${running} en cours` : ""}{queued ? `, ${queued} en attente` : ""})</h4>
      <div className="row">
        <button onClick={() => rerunNode(node.id, { scope: "all_items", params_patch: { seed: randomSeed() }, force_recompute: true }, "Relancer tous les items")}>Relancer tous les items</button>
      </div>
      {items.map((it) => {
        const img = (it.outputs?.image || [])[0];
        const j = it.jobs?.[0];
        return (
          <div key={it.item_id} className={`itembox ${it.status}`}>
            {img ? <img src={artUrl(img.id)} onClick={() => setState({ modal: { kind: "image", title: it.label, urls: [{ url: artUrl(img.id), label: it.label }] } })} /> : <div className="noimg" />}
            <div className="grow">
              <div><b>{it.label}</b> <code>{it.item_key}</code> {it.status === "archived" && <span className="pill">archivé</span>}</div>
              <div className="hint">item_id {it.item_id} · seed {it.seed ?? "—"} · {it.variants_count} variante(s) · {STATUS_LABEL[j?.status] || STATUS_LABEL[it.status_slot] || it.status_slot}</div>
              {it.candidate_not_selected && <div className="cand-inline">Candidate plus récente non sélectionnée</div>}
              {j?.error && <div className="err">{j.error.message}</div>}
              {it.status === "active" && (
                <div className="row">
                  <button onClick={() => rerunItem(node.id, it.item_id, { scope: "item_only", params_patch: { seed: randomSeed() }, force_recompute: true }, `Régénérer « ${it.label} »`)}>Régénérer ce résultat</button>
                  <button onClick={() => rerunItem(node.id, it.item_id, { scope: "item_only", force_recompute: false }, `Relancer « ${it.label} » (mêmes paramètres)`)}>Mêmes paramètres</button>
                  <button onClick={() => rerunItem(node.id, it.item_id, { scope: "item_and_descendants", force_recompute: true }, `« ${it.label} » + descendants`)}>+ descendants</button>
                  {j && ["queued", "running"].includes(j.status) && <button onClick={() => api.cancelItem(view.graph.id, node.id, it.item_id).then(load)}>Annuler</button>}
                  {j && ["failed", "blocked", "interrupted", "cancelled"].includes(j.status) && <button onClick={() => api.resumeItem(view.graph.id, node.id, it.item_id).then(() => { load(); refreshGraph(); })}>Reprendre</button>}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function PreviewSection({ node }: { node: GNode }) {
  const view = useStore((s) => s.view)!;
  if (!node.type.endsWith(".preview")) return null;
  const st = view.status?.[node.id];
  const ref = st?.ref;
  const owner = node.auto?.owner_node_id;
  const slot = Object.values(view.slots).find((s) => (ref?.kind === "variant") && s.variants.some((v) => v.id === ref.variant_id));
  const v = slot?.variants.find((x) => x.id === ref?.variant_id);
  const out = v?.outputs || {};
  return (
    <div className="section">
      <h4>Aperçu {ref?.pinned ? "(variante épinglée)" : "(variante sélectionnée de la source)"}</h4>
      <p className="hint">Ouvrir ou actualiser la preview ne lance aucun calcul. La sortie transmet exactement la variante sélectionnée.</p>
      {node.type === "image.preview" && (out.image || [])[0] && (
        <img className="bigimg" src={artUrl(out.image[0])} onClick={() => setState({ modal: { kind: "image", title: node.label || node.id, urls: [{ url: artUrl(out.image[0]), label: node.label || "" }] } })} />
      )}
      {node.type === "mesh.preview" && (out.mesh || out.rigged_mesh) && (
        <>
          {(out.previews || [])[0] && <img className="bigimg" src={artUrl(out.previews[0])} />}
          <button onClick={async () => {
            const ids = [...(out.rigged_mesh || []), ...(out.mesh || [])];
            for (const i of ids) { const a: any = await api.artifact(i); if (a.path.endsWith(".glb")) { setState({ modal: { kind: "mesh", title: node.label || "3D", url: artUrl(a.id) } }); return; } }
          }}>Ouvrir la vue 3D</button>
        </>
      )}
      {node.type === "video.preview" && (out.video || [])[0] && (
        <button onClick={async () => {
          const frames = await Promise.all((out.frames || []).map(async (i: string) => { const a: any = await api.artifact(i); return { url: artUrl(i), t: a.meta?.time_s ?? 0 }; }));
          setState({ modal: { kind: "video", title: node.label || "Vidéo", url: artUrl(out.video[0]), frames: frames.filter((f) => f.t != null) } });
        }}>Lire la vidéo</button>
      )}
      {!ref || ref.kind === "missing" ? <p className="hint">{ref?.reason || "Pas encore de résultat."}</p> : null}
      {owner && node.auto && (
        <div className="row">
          <button onClick={() => rerunItem(owner, node.auto!.item_id, { scope: "item_only", params_patch: { seed: randomSeed() }, force_recompute: true }, `Régénérer « ${node.label} »`)}>Régénérer ce résultat</button>
          <button onClick={() => rerunItem(owner, node.auto!.item_id, { scope: "item_and_descendants", force_recompute: true }, `« ${node.label} » + descendants`)}>Régénérer + descendants</button>
          <button onClick={() => setState({ selected: owner })}>Voir les variantes de l'item</button>
        </div>
      )}
      <button onClick={() => refreshGraph()}>Actualiser</button>
    </div>
  );
}

export const IMPORT_ACCEPT: Record<string, string> = {
  "image.import": ".png,.jpg,.jpeg,.webp", "mesh.import": ".glb,.gltf,.obj,.fbx,.ply", "video.import": ".mp4,.webm,.mov",
};
export const IMPORT_KIND: Record<string, "image" | "mesh" | "video"> = { "image.import": "image", "mesh.import": "mesh", "video.import": "video" };

function ImportSection({ node }: { node: GNode }) {
  const [up, setUp] = useState<any>(null);
  const uid = node.params?.upload_id;
  useEffect(() => { setUp(null); if (uid) api.getUpload(uid).then(setUp).catch(() => undefined); }, [uid]);
  if (!["image.import", "mesh.import", "video.import"].includes(node.type)) return null;
  const src = up?.meta?.source;
  const onFile = async (f?: File | null) => {
    if (!f) return;
    try {
      const up: any = await api.upload(f);
      await applyOps([{ op: "update_node", node_id: node.id, params: { upload_id: up.id } }]);
      toast(`Fichier importé : ${up.filename}`, "ok");
    } catch (e) { toast(errText(e), "error"); }
  };
  return (
    <div className="section">
      <h4>Fichier</h4>
      {node.params?.upload_id && node.type === "image.import" && <img className="bigimg" src={uploadUrl(node.params.upload_id)} />}
      {node.params?.upload_id && node.type === "video.import" && <video className="bigimg" src={uploadUrl(node.params.upload_id)} controls muted />}
      <p className="hint">{uid ? `${up?.filename || uid} (original conservé)` : "Aucun fichier"}
        {src && <><br />Depuis la médiathèque : node <code>{src.node_id}</code>{src.item_key ? ` · ${src.item_key}` : ""}, graphe <code>{src.graph_id}</code></>}</p>
      <div className="section actions" style={{ border: 0, padding: 0 }}>
        <label className="filebtn">{uid ? "Remplacer par un fichier" : "Choisir un fichier"}
          <input type="file" accept={IMPORT_ACCEPT[node.type]} onChange={(e) => onFile(e.target.files?.[0])} /></label>
        <button onClick={() => setState({ mediaPick: { kind: IMPORT_KIND[node.type], nodeId: node.id, label: node.label || node.id } })}>
          🖼 Choisir dans la médiathèque</button>
      </div>
    </div>
  );
}

function ReferencesEditor({ node }: { node: GNode }) {
  if (node.type !== "reference.collection") return null;
  const slots: any[] = node.params?.slots || [];
  const save = (s: any[]) => applyOps([{ op: "update_node", node_id: node.id, params: { slots: s } }]).catch(() => undefined);
  return (
    <div className="section">
      <h4>Slots de références (ordre conservé jusqu'au prompt)</h4>
      {slots.map((s, i) => (
        <div key={s.id} className="refslot">
          <input defaultValue={s.label} placeholder="Nom (ex. Kaelith)" onBlur={(e) => save(slots.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)))} />
          <select value={s.role || "subject"} onChange={(e) => save(slots.map((x, j) => (j === i ? { ...x, role: e.target.value } : x)))}>
            {["subject", "identity", "decor", "style", "movement", "camera"].map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <input defaultValue={s.description} placeholder="Description" onBlur={(e) => save(slots.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)))} />
          <button className="mini" onClick={() => save(slots.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      <button onClick={() => save([...slots, { id: `ref${slots.length + 1}`, role: "subject", label: `Référence ${slots.length + 1}` }])}>Ajouter un slot</button>
    </div>
  );
}

function LogSection({ node }: { node: GNode }) {
  const view = useStore((s) => s.view)!;
  const [txt, setTxt] = useState("");
  const jobs = Object.values(view.slots).filter((s) => s.node_id === node.id).flatMap((s) => s.jobs);
  jobs.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  const last = jobs[0];
  if (!last) return null;
  return (
    <div className="section">
      <details onToggle={(e) => (e.target as HTMLDetailsElement).open && api.jobLog(last.id).then(setTxt)}>
        <summary>Journal technique (dernière tâche {last.id} · {STATUS_LABEL[last.status] || last.status} · {last.author})</summary>
        <pre className="log">{txt || "…"}</pre>
        <button className="mini" onClick={() => api.jobLog(last.id).then(setTxt)}>Rafraîchir</button>
      </details>
      {last.error && <div className="err"><b>Erreur :</b> {last.error.message}{last.error.corrective_action && <><br /><b>Action corrective :</b> {last.error.corrective_action}</>}</div>}
    </div>
  );
}

function MultiSelection({ ids }: { ids: string[] }) {
  const view = useStore((s) => s.view)!;
  const nts = useStore((s) => s.nodeTypes);
  const nodes = view.graph.nodes.filter((n) => ids.includes(n.id));
  const autos = nodes.filter((n) => n.auto).length;
  return (
    <div className="inspector">
      <div className="section">
        <h4>{nodes.length} nodes sélectionnées</h4>
        <ul className="sel-list">
          {nodes.map((n) => (
            <li key={n.id}><button className="linklike" onClick={() => setState({ selected: n.id, selection: [n.id] })}>{n.label || nts[n.type]?.label || n.id}</button>
              <span className="hint"> · {nts[n.type]?.label}{n.auto ? " (preview automatique)" : ""}</span></li>
          ))}
        </ul>
      </div>
      <div className="section actions">
        <button onClick={() => copySelection()} title="Ctrl+C">Copier</button>
        <button onClick={() => cutSelection()} title="Ctrl+X">Couper</button>
        <button onClick={duplicateSelection} title="Ctrl+D">Dupliquer</button>
        <button className="danger" onClick={deleteSelection} title="Suppr">Supprimer</button>
      </div>
      <p className="hint">
        Les noodles entre nodes sélectionnées sont copiées avec elles ; Ctrl+Maj+V colle aussi les connexions entrantes.
        Seules les définitions sont copiées (réglages, prompts) : les résultats restent sur les nodes d'origine.
        {autos > 0 && ` Les ${autos} preview(s) automatique(s) ne sont pas copiées : elles sont recréées pour chaque item.`}
      </p>
    </div>
  );
}

export function Inspector() {
  const view = useStore((s) => s.view);
  const selected = useStore((s) => s.selected);
  const selection = useStore((s) => s.selection);
  const nt = useStore((s) => (selected && view ? s.nodeTypes[view.graph.nodes.find((n) => n.id === selected)?.type || ""] : undefined));
  const node = useMemo(() => view?.graph.nodes.find((n) => n.id === selected), [view, selected]);
  if (!view) return <div className="inspector"><p className="hint">Aucun graphe.</p></div>;
  if (selection.length > 1) return <MultiSelection ids={selection} />;
  if (!node) return <GraphInfo />;
  const kind = nt?.kind;
  const exec = kind && !["source", "preview", "import", "collection"].includes(kind);
  const running = Object.values(view.slots).filter((s) => s.node_id === node.id).flatMap((s) => s.jobs).filter((j) => ["queued", "running"].includes(j.status));
  return (
    <div className="inspector">
      <div className="section">
        <input className="title-input" defaultValue={node.label || nt?.label} key={node.id + (node.label || "")}
          onBlur={(e) => e.target.value !== (node.label || "") && applyOps([{ op: "update_node", node_id: node.id, label: e.target.value }]).catch(() => undefined)} />
        <div className="hint">{nt?.label} · <code>{node.id}</code> · {nt?.description}</div>
        {view.validation.errors.filter((e: any) => e.node_id === node.id).map((e: any, i: number) => <div key={i} className="err">{e.message}</div>)}
        {view.validation.warnings.filter((e: any) => e.node_id === node.id).map((e: any, i: number) => <div key={i} className="warn">{e.message}</div>)}
      </div>
      {exec && (
        <div className="section actions">
          <button onClick={() => runScope({ scope: "until", node_id: node.id }, "Exécuter cette node")} title="Cette node et ses prérequis manquants">Exécuter cette node</button>
          <button onClick={() => runScope({ scope: "branch", node_id: node.id }, "Exécuter cette branche")}>Exécuter cette branche</button>
          {kind === "generative"
            ? <button onClick={() => rerunNode(node.id, { scope: "node_only", params_patch: { seed: randomSeed() }, force_recompute: true }, "Régénérer (nouvelle seed)")}>Régénérer (nouvelle seed)</button>
            : <button onClick={() => rerunNode(node.id, { scope: "node_only", force_recompute: true }, "Recalculer")}>Recalculer</button>}
          {kind === "generative" && <button onClick={() => rerunNode(node.id, { scope: "node_only", force_recompute: false }, "Même seed (cache autorisé)")}>Même seed</button>}
          <button onClick={() => rerunNode(node.id, { scope: "node_and_descendants", force_recompute: true }, "Node + descendants")}>Recalculer + descendants</button>
          <button onClick={() => runScope({ scope: "until", node_id: node.id, force_recompute: true }, "Forcer le recalcul")} title="Réexécute réellement, sans réutiliser le cache">Forcer le recalcul</button>
          {running.length > 0 && <button className="danger" onClick={() => Promise.all(running.map((j) => api.cancel(j.id))).then(() => toast("Annulation demandée (arrêt effectif signalé par le moteur)", "info"))}>Annuler ({running.length})</button>}
        </div>
      )}
      <div className="section actions">
        <button onClick={duplicateSelection} title="Ctrl+D">Dupliquer</button>
        <button onClick={() => copySelection()} title="Ctrl+C">Copier</button>
        <button onClick={() => cutSelection()} title="Ctrl+X">Couper</button>
        <button className="danger" onClick={() => applyOps([{ op: "remove_node", node_id: node.id }]).then(() => setState({ selected: null })).catch(() => undefined)}>Supprimer</button>
      </div>
      <ImportSection node={node} />
      <PreviewSection node={node} />
      <Execution node={node} />
      <ReferencesEditor node={node} />
      {!["image.preview", "mesh.preview", "video.preview"].includes(node.type) && <Params node={node} />}
      <ItemsSection node={node} />
      <SlotsSection node={node} />
      <LogSection node={node} />
    </div>
  );
}

function GraphInfo() {
  const view = useStore((s) => s.view)!;
  const [plan, setPlan] = useState<any>(null);
  const doPlan = async () => { try { setPlan(await api.plan(view.graph.id)); refreshGraph(); } catch (e) { toast(errText(e), "error"); } };
  const exportJson = () => {
    const blob = new Blob([JSON.stringify(view.graph, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `${view.graph.name.replace(/[^\w-]+/g, "_")}.graph.json`; a.click();
  };
  return (
    <div className="inspector">
      <div className="section">
        <input className="title-input" defaultValue={view.graph.name} key={view.graph.id}
          onBlur={(e) => e.target.value !== view.graph.name && applyOps([{ op: "set_name", name: e.target.value }]).catch(() => undefined)} />
        <div className="hint"><code>{view.graph.id}</code> · révision {view.graph.revision} · schéma {view.graph.schema_version}</div>
      </div>
      <div className="section actions">
        <button onClick={doPlan}>Planifier (sans générer)</button>
        <button className="primary" onClick={() => runScope({ scope: "graph" }, "Exécuter le graphe")}>Exécuter le graphe</button>
        <button onClick={exportJson}>Exporter graph.json</button>
      </div>
      {view.validation.errors.length > 0 && <div className="section"><h4>Erreurs</h4>{view.validation.errors.map((e: any, i: number) => <div key={i} className="err">{e.message}</div>)}</div>}
      {view.validation.warnings.length > 0 && <div className="section"><h4>Avertissements</h4>{view.validation.warnings.map((e: any, i: number) => <div key={i} className="warn">{e.message}</div>)}</div>}
      {plan && (
        <div className="section">
          <h4>Plan ({plan.scope_label})</h4>
          {Object.entries(plan.foreach || {}).map(([nid, f]: any) => <div key={nid} className="ok">{nid} : {f.summary}</div>)}
          {plan.errors.map((e: any, i: number) => <div key={i} className="err">{e.message}</div>)}
          <ol className="planlist">{plan.executions.map((e: any, i: number) => (
            <li key={i}>{e.label}{e.item_key ? ` [${e.item_key}]` : ""} · {e.step_label} — <i>{e.reason}</i> · {e.engine?.workflow_id || e.capability} ({e.engine?.status})</li>
          ))}</ol>
          {plan.skipped.length > 0 && <p className="hint">{plan.skipped.length} étape(s) à jour réutilisée(s).</p>}
        </div>
      )}
      <p className="hint">Sélectionnez une node pour voir ses réglages, sous-étapes, variantes et journaux.</p>
      <div className="section">
        <h4>Raccourcis</h4>
        <ul className="shortcuts">
          <li><kbd>V</kbd> mode sélection (glisser = rectangle) · <kbd>H</kbd> mode déplacement · <kbd>Maj</kbd>+glisser : rectangle dans les deux modes</li>
          <li><kbd>Ctrl</kbd>+clic : ajouter/retirer une node · <kbd>Ctrl</kbd>+<kbd>A</kbd> : tout sélectionner · <kbd>Échap</kbd> : désélectionner</li>
          <li><kbd>Ctrl</kbd>+<kbd>C</kbd> / <kbd>X</kbd> / <kbd>V</kbd> : copier / couper / coller (à la position de la souris) · <kbd>Ctrl</kbd>+<kbd>Maj</kbd>+<kbd>V</kbd> : coller avec les connexions entrantes</li>
          <li><kbd>Ctrl</kbd>+<kbd>D</kbd> : dupliquer · <kbd>Suppr</kbd> : supprimer · <kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd> : annuler / rétablir</li>
        </ul>
      </div>
    </div>
  );
}
