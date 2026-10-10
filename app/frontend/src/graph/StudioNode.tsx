import { Handle, Position, NodeProps, useUpdateNodeInternals } from "@xyflow/react";
import { memo, useEffect, useState } from "react";
import { artUrl, GNode, NodeTypeDef, VariantLite, uploadUrl } from "../api";
import { setState, STATUS_LABEL, toast, useStore } from "../store";
import { IMAGE_GEN, inputPorts, outputPorts, TYPE_COLORS } from "./ports";
import { PropsPanel } from "./PropsPanel";
import { CompareNode } from "./CompareView";
import { IMPORT_ACCEPT, IMPORT_KIND } from "../panels/Inspector";
import { api } from "../api";
import { applyOps, errText, randomSeed, rerunItem, rerunNode, runScope, selectVariant } from "../actions";

export type StudioData = {
  node: GNode;
  nt?: NodeTypeDef;
  status?: any;
  slotsOf: (nodeId: string) => any[];
  variant: (id?: string | null) => VariantLite | undefined;
  previewVariant?: VariantLite;
  connectedIn?: string[]; // entrées portant une noodle (toujours affichées)
  owner?: GNode;
};

function thumbOf(v?: VariantLite): string | undefined {
  if (!v) return undefined;
  const o = v.outputs || {};
  const id = (o.image || [])[0] || (o.previews || [])[0] || (o.frames || [])[0];
  return id ? artUrl(id) : undefined;
}

const RES_LABEL: Record<string, string> = { gpu: "du GPU", net: "du service externe", cpu: "du processeur" };

/** Raison de l'attente d'une étape (calculée par le serveur) : prérequis du run, ou étape qui occupe la même file. */
function waitingText(job: any): string {
  const w = job?.waiting_for;
  if (!w) return "en attente";
  if (w.kind === "prerequisite") return `en attente de : ${(w.nodes || []).join(", ")}`;
  const mins = w.since ? Math.max(0, Math.round((Date.now() - Date.parse(w.since)) / 60000)) : null;
  const dur = mins == null ? "" : mins < 60 ? ` depuis ${mins} min` : ` depuis ${Math.floor(mins / 60)} h ${String(mins % 60).padStart(2, "0")}`;
  return `en attente ${RES_LABEL[w.resource] || "de la file"} : ${w.node_id || "?"}${w.graph_name ? ` (« ${w.graph_name} »)` : ""}${dur}`;
}

function latestJob(slots: any[]) {
  const all = slots.flatMap((s) => s.jobs || []);
  all.sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
  return all[all.length - 1];
}

function StudioNodeImpl({ data, selected }: NodeProps) {
  const d = data as unknown as StudioData;
  const { node: n, nt } = d;
  const live = useStore((s) => s.liveProgress);
  const livePreview = useStore((s) => s.livePreview);
  const ins = inputPorts(n, nt, new Set(d.connectedIn || []));
  const outs = outputPorts(n, nt);
  // Ports ajoutés, retirés ou remplacés (mode, items foreach, pièces…) : React Flow doit remesurer les points d'accroche,
  // sinon une noodle vers un nouveau port n'est dessinée qu'au rechargement de la page (constaté quand la taille de la node
  // ne change pas, ex. « Références » → « Image → vidéo »).
  const updateInternals = useUpdateNodeInternals();
  const portSig = ins.map((p) => p.name).join("|") + "→" + outs.map((p) => p.name).join("|");
  useEffect(() => { updateInternals(n.id); }, [portSig, n.id, updateInternals]);
  const slots = d.slotsOf(n.id);
  const job = latestJob(slots);
  const lp = job ? live[job.id] : undefined;
  const running = job && (job.status === "running" || job.status === "queued");
  const st = d.status?.status;
  const statusKey = running ? job.status : st;
  const foreach = IMAGE_GEN.includes(n.type) && n.execution?.mode === "foreach_prompts";
  const items = (n.items || []).filter((i) => i.status !== "archived");
  const lastStep = nt?.default_steps?.length ? null : null;
  void lastStep;

  // Slot dont la node montre les résultats : dernière étape (node simple) ou item producteur (preview automatique).
  // La node affiche le DERNIER résultat (une régénération apparaît tout de suite) ; s'il n'est pas encore la sortie
  // utilisée en aval, il est marqué « candidate » avec un bouton « Utiliser ». Les flèches parcourent les variantes.
  let navSlot: any = null;
  let navOwner = n.id;
  if (n.auto) {
    const ownerSlots = d.slotsOf(n.auto.owner_node_id).filter((s) => s.item_id === n.auto!.item_id);
    navSlot = ownerSlots[ownerSlots.length - 1] || null;
    navOwner = n.auto.owner_node_id;
  } else if (!foreach && !d.previewVariant && n.type !== "image.import") {
    const lastSlots = slots.filter((s) => !s.item_id);
    navSlot = lastSlots[lastSlots.length - 1] || null;
  }
  const variants: VariantLite[] = navSlot?.variants || [];
  const latestId = variants.length ? variants[variants.length - 1].id : null;
  const [shownId, setShownId] = useState<string | null>(latestId);
  useEffect(() => { setShownId(latestId); }, [latestId]); // nouveau résultat : affiché automatiquement
  let shownIdx = shownId ? variants.findIndex((v) => v.id === shownId) : -1;
  if (shownIdx < 0) shownIdx = variants.length - 1;
  const shown = variants[shownIdx];
  const activeId: string | null = navSlot?.active_variant_id || null;

  let thumb: string | undefined;
  if (n.type === "image.import" && n.params?.upload_id) thumb = uploadUrl(n.params.upload_id);
  else if (shown) thumb = thumbOf(shown);
  else if (d.previewVariant && n.type !== "compare.before_after") thumb = thumbOf(d.previewVariant);
  const liveKey = `${n.id}|`;
  let liveUrl = running && livePreview[liveKey];
  if (n.auto) {
    const ownerSlots = d.slotsOf(n.auto.owner_node_id).filter((s) => s.item_id === n.auto!.item_id);
    const oj = latestJob(ownerSlots);
    if (oj && oj.status === "running") liveUrl = livePreview[`${n.auto.owner_node_id}|${n.auto.item_id}`] || liveUrl;
  }

  const prog = lp?.progress || job?.progress;
  const pct = prog && prog.value != null && prog.max ? Math.round((100 * prog.value) / prog.max) : null;

  return (
    <div className={`snode ${selected ? "sel" : ""} kind-${nt?.kind || "x"} ${n.archived ? "archived" : ""} ${n.type === "image.props" || n.type === "compare.before_after" ? "wide" : ""}`}>
      <div className="snode-head">
        <div className="snode-title">{n.label || nt?.label || n.type}</div>
        <div className="snode-sub">{nt?.label}{n.auto ? " · automatique" : ""}</div>
        {statusKey && <span className={`badge st-${statusKey}`}>{STATUS_LABEL[statusKey] || statusKey}</span>}
      </div>
      {IMAGE_GEN.includes(n.type) && (
        <div className="snode-info">
          <b>{n.type === "image.edit" ? "Éditer" : "Créer"}</b>
          {n.params?.engine === "openai"
            ? <span className="pill ext" title="Service externe : la consigne (et l'image à éditer) sont envoyées à OpenAI">☁ OpenAI{n.params?.openai_model ? ` · ${n.params.openai_model}` : ""}</span>
            : <span className="pill" title="Calcul local sur ce PC">Krea 2 · local</span>}
          {foreach ? <span className="pill">pour chaque prompt · {items.length} → {items.length} résultat{items.length > 1 ? "s" : ""}</span> : <span className="pill">unique</span>}
          {n.params?.postprocess?.remove_background && <span className="pill">+ détourage</span>}
        </div>
      )}
      {n.type === "video.import" && n.params?.upload_id && (
        <div className="snode-thumb"><video src={uploadUrl(n.params.upload_id)} muted loop playsInline preload="metadata"
          onMouseEnter={(e) => (e.target as HTMLVideoElement).play().catch(() => undefined)} onMouseLeave={(e) => (e.target as HTMLVideoElement).pause()} /></div>
      )}
      {n.type === "text.prompt" && <div className="snode-text">{String(n.params?.text || "").slice(0, 160) || <i>(vide)</i>}</div>}
      {n.type === "image.prepare" && (
        <div className="snode-info">
          {["a_pose", "t_pose"].includes(n.params?.pose)
            ? <span className={`pill ${n.params?.engine === "openai" ? "ext" : ""}`} title="Pose redessinée par IA (nouvelle image, à vérifier)">
                {n.params.pose === "a_pose" ? "A-pose" : "T-pose"} · {n.params?.engine === "openai" ? "☁ OpenAI" : "Krea 2"}</span>
            : <span className="pill">pose inchangée</span>}
          <span className="pill">{n.params?.remove_background === false ? "fond conservé" : "détourage"}</span>
          {n.params?.crop_to_subject !== false && n.params?.remove_background !== false && <span className="pill">cadrage</span>}
        </div>
      )}
      {n.type === "image.upscale" && (
        <div className="snode-info">
          <b>{(n.params?.resolution || "2k").toUpperCase()}</b>
          <span className="pill">{(n.params?.engine || "seedvr2") === "seedvr2" ? "SeedVR2 · détails" : "Real-ESRGAN · rapide"}</span>
          <span className="pill">{(n.params?.resolution || "2k") === "4k" ? "4096 px" : "2048 px"}</span>
        </div>
      )}
      {n.type === "text.randomize" && (
        <>
          <div className="snode-text" title={n.params?.context || ""}>{String(n.params?.context || "").slice(0, 120) || <i>(contexte vide)</i>}</div>
          <div className="snode-info">
            <span className="pill">{n.params?.count ?? 5} prompts</span>
            <span className="pill">{n.params?.language === "fr" ? "français" : "anglais"}</span>
            {n.params?.engine === "openai" ? <span className="pill ext" title="Le contexte est envoyé à OpenAI">☁ OpenAI</span>
              : n.params?.engine === "claude" ? <span className="pill ext" title="Le contexte est envoyé à Anthropic">☁ Claude</span>
              : <span className="pill">Qwen3-VL · local</span>}
          </div>
          {shown?.texts?.length ? (
            <ol className="snode-prompts nodrag nowheel">
              {shown.texts.map((tx, i) => <li key={i} title={tx}>{tx.length > 150 ? tx.slice(0, 150) + "…" : tx}</li>)}
            </ol>
          ) : null}
        </>
      )}
      {n.type === "image.props" && <PropsPanel n={n} slots={slots} job={job} />}
      {n.type === "compare.before_after" && <CompareNode n={n} refs={d.status?.refs} variant={d.variant} />}
      {(thumb || liveUrl) && (
        <div className="snode-thumb">
          <img src={liveUrl || thumb} alt="" draggable={false} />
          {liveUrl && <span className="live">aperçu d'échantillonnage en direct</span>}
        </div>
      )}
      {shown && !liveUrl && (
        <div className="vnav nodrag">
          {variants.length > 1 && (
            <>
              <button className="mini" disabled={shownIdx === 0} onClick={() => setShownId(variants[shownIdx - 1].id)} title="Variante précédente">‹</button>
              <span className="vcount">{shownIdx + 1}/{variants.length}</span>
              <button className="mini" disabled={shownIdx === variants.length - 1} onClick={() => setShownId(variants[shownIdx + 1].id)} title="Variante suivante">›</button>
            </>
          )}
          {shown.id === activeId
            ? <span className="vstate on" title="Sortie active : c'est elle que reçoivent les nodes en aval">✓ utilisée en aval</span>
            : <>
                <span className="vstate" title="Résultat conservé, pas encore transmis aux nodes en aval">candidate</span>
                <button className="mini use" onClick={() => selectVariant(navOwner, shown.id)} title="En faire la sortie transmise en aval (seuls les descendants concernés deviennent « à recalculer »)">Utiliser</button>
              </>}
        </div>
      )}
      {foreach && (
        <div className="snode-items">
          {items.sort((a, b) => (a.order || 0) - (b.order || 0)).map((it) => {
            const s = slots.filter((x) => x.item_id === it.item_id);
            const j = latestJob(s);
            const ss = d.status?.slots?.find((x: any) => x.item_id === it.item_id && x.step === s[s.length - 1]?.step);
            const key = j && (j.status === "running" || j.status === "queued" || j.status === "failed" || j.status === "blocked") ? j.status : ss?.status || "missing";
            return <div key={it.item_id} className="item-row"><span>{it.label || it.item_key}</span><span className={`dot st-${key}`} title={STATUS_LABEL[key] || key} /></div>;
          })}
        </div>
      )}
      {running && (
        <div className="snode-progress">
          <div className="bar"><div style={{ width: pct != null ? `${pct}%` : "100%" }} className={pct == null ? "indet" : ""} /></div>
          <div className="ptext">{lp?.stage || job.stage || (job.status === "queued" ? waitingText(job) : "traitement en cours")}{pct != null ? ` · ${prog.value}/${prog.max}` : ""}</div>
        </div>
      )}
      {job?.status === "failed" && <div className="snode-err">{job.error?.message?.slice(0, 180)}</div>}
      <div className="snode-ports">
        <div className="col">
          {ins.map((p) => (
            <div className="port in" key={p.name} title={p.description || ""}>
              <Handle type="target" position={Position.Left} id={p.name} style={{ background: TYPE_COLORS[p.type] || "#999" }} />
              <span className="pname">{p.label}</span><span className="ptype">{p.type}{p.multi ? " ×n" : ""}</span>
            </div>
          ))}
        </div>
        <div className="col right">
          {outs.map((p) => (
            <div className="port out" key={p.name} title={p.description || ""}>
              <span className="ptype">{p.type}</span><span className="pname">{p.label}</span>
              <Handle type="source" position={Position.Right} id={p.name} style={{ background: TYPE_COLORS[p.type] || "#999" }} />
            </div>
          ))}
        </div>
      </div>
      <div className="snode-actions nodrag">
        {n.auto ? (
          <>
            <button title="Relance uniquement l'item producteur (nouvelle seed), sans toucher aux autres résultats"
              onClick={() => rerunItem(n.auto!.owner_node_id, n.auto!.item_id, { scope: "item_only", params_patch: { seed: randomSeed() }, force_recompute: true }, `Régénérer « ${n.label} »`)}>
              Régénérer ce résultat</button>
          </>
        ) : n.type === "image.import" || n.type === "mesh.import" || n.type === "video.import" ? (
          <>
            <label className="filebtn mini" title="Importer un fichier depuis le disque">Fichier…
              <input type="file" accept={IMPORT_ACCEPT[n.type]}
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  try {
                    const up: any = await api.upload(f);
                    await applyOps([{ op: "update_node", node_id: n.id, params: { upload_id: up.id } }]);
                    toast(`Fichier importé : ${up.filename}`, "ok");
                  } catch (err) { toast(errText(err), "error"); }
                }} /></label>
            <button title="Choisir une image ou un modèle 3D déjà généré"
              onClick={() => setState({ mediaPick: { kind: IMPORT_KIND[n.type], nodeId: n.id, label: n.label || nt?.label || n.id } })}>
              🖼 Médiathèque</button>
          </>
        ) : n.type === "image.props" ? null : nt?.kind !== "source" && nt?.kind !== "preview" && nt?.kind !== "import" && nt?.kind !== "collection" ? (
          <>
            <button onClick={() => runScope({ scope: "until", node_id: n.id }, "Exécuter jusqu'ici")}>Exécuter</button>
            {(nt?.kind === "generative" || (n.type === "image.prepare" && ["a_pose", "t_pose"].includes(n.params?.pose))) && <button title="Nouvelle variante avec une nouvelle seed (candidate, non activée)"
              onClick={() => rerunNode(n.id, { scope: "node_only", params_patch: { seed: randomSeed() }, force_recompute: true }, "Régénérer")}>Régénérer</button>}
          </>
        ) : null}
      </div>
    </div>
  );
}

export const StudioNode = memo(StudioNodeImpl);
