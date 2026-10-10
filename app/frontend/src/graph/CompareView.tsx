// « Avant / après » : compare deux résultats — images, vidéos (lecture synchronisée) ou modèles 3D (rendus + vue 3D
// côte à côte à caméras liées). Modes : curseur, côte à côte, différence. Affichage seul, aucun calcul.
import { useEffect, useRef, useState } from "react";
import { api, artUrl, uploadUrl, VariantLite } from "../api";
import { applyOps } from "../actions";
import { setState } from "../store";

export type Media = { kind: "image" | "video" | "mesh"; url?: string; glb?: string; poster?: string; label: string } | null;
type Mode = "slider" | "side_by_side" | "difference";

const uploads = new Map<string, Promise<any>>();
function useUpload(id?: string) {
  const [up, setUp] = useState<any>(null);
  useEffect(() => {
    if (!id) return;
    if (!uploads.has(id)) uploads.set(id, api.getUpload(id).catch(() => null));
    let live = true;
    uploads.get(id)!.then((u) => live && setUp(u));
    return () => { live = false; };
  }, [id]);
  return up;
}

/** Média affichable pour une référence d'entrée (variante d'une étape, ou fichier importé). */
export function useMedia(ref: any, variant: (id?: string | null) => VariantLite | undefined, label: string): Media {
  const up = useUpload(ref?.kind === "upload" ? ref.upload_id : undefined);
  if (!ref || ref.kind === "missing") return null;
  if (ref.kind === "upload") {
    const url = uploadUrl(ref.upload_id);
    const prev = up?.preview_artifact_id ? artUrl(up.preview_artifact_id) : undefined;  // rendu du résultat d'origine
    if (up?.kind === "video") return { kind: "video", url, poster: prev, label };
    if (up?.kind === "mesh") return { kind: "mesh", glb: url, url: prev, label };
    return up?.kind ? { kind: "image", url, label } : null;
  }
  if (ref.kind !== "variant") return null;
  const o = variant(ref.variant_id)?.outputs || {};
  const first = (k: string) => (o[k] || [])[0];
  if (o.video?.length) return { kind: "video", url: artUrl(first("video")), poster: o.frames?.length ? artUrl(o.frames[0]) : undefined, label };
  const glb = first("rigged_mesh") || first("mesh");
  if (glb) return { kind: "mesh", glb: artUrl(glb), url: o.previews?.length ? artUrl(o.previews[0]) : undefined, label };
  const img = first(ref.output && o[ref.output] ? ref.output : "image") || first("image");
  return img ? { kind: "image", url: artUrl(img), label } : null;
}

function Visual({ m, vref, className }: { m: Media; vref?: (v: HTMLVideoElement | null) => void; className?: string }) {
  if (!m) return <div className={`cmp-empty ${className || ""}`}>—</div>;
  if (m.kind === "video") return <video ref={vref} className={className} src={m.url} poster={m.poster} muted loop playsInline preload="metadata" />;
  if (m.kind === "mesh" && !m.url) return <div className={`cmp-empty ${className || ""}`}>3D (pas de rendu)</div>;
  return <img className={className} src={m.url} alt="" draggable={false} />;
}

export function CompareView({ before, after, mode, onMode, big = false }: {
  before: Media; after: Media; mode: Mode; onMode?: (m: Mode) => void; big?: boolean;
}) {
  const [split, setSplit] = useState(50);
  const [playing, setPlaying] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const vids = useRef<(HTMLVideoElement | null)[]>([null, null]);
  const hasVideo = before?.kind === "video" || after?.kind === "video";
  const both3d = before?.kind === "mesh" && after?.kind === "mesh" && before.glb && after.glb;

  // vidéos : lecture commune, la seconde suit la première (écart > 0,15 s recalé)
  useEffect(() => {
    const [a, b] = vids.current;
    if (!a || !b) return;
    const sync = () => { if (Math.abs(a.currentTime - b.currentTime) > 0.15) b.currentTime = a.currentTime; };
    a.addEventListener("timeupdate", sync);
    return () => a.removeEventListener("timeupdate", sync);
  }, [before?.url, after?.url, mode]);
  const toggle = () => {
    const vs = vids.current.filter(Boolean) as HTMLVideoElement[];
    if (playing) vs.forEach((v) => v.pause());
    else { const t = vs[0]?.currentTime || 0; vs.forEach((v) => { v.currentTime = t; v.play().catch(() => undefined); }); }
    setPlaying(!playing);
  };
  const drag = (e: React.PointerEvent) => {
    const r = box.current?.getBoundingClientRect();
    if (!r) return;
    setSplit(Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100)));
  };
  const vref = (i: number) => (v: HTMLVideoElement | null) => { vids.current[i] = v; };
  return (
    <div className={`cmpv ${big ? "big" : ""} nodrag nowheel`}>
      {onMode && (
        <div className="seg">
          {([["slider", "Curseur"], ["side_by_side", "Côte à côte"], ["difference", "Différence"]] as [Mode, string][]).map(([k, l]) => (
            <button key={k} className={k === mode ? "on" : ""} onClick={() => k !== mode && onMode(k)}>{l}</button>))}
        </div>
      )}
      {mode === "side_by_side" ? (
        <div className="cmp-sbs">
          {[before, after].map((m, i) => (
            <figure key={i}><Visual m={m} vref={vref(i)} /><figcaption>{m?.label}{m?.kind === "mesh" ? " · rendu 3D" : ""}</figcaption></figure>
          ))}
        </div>
      ) : (
        <div ref={box} className={`cmp-stack ${mode}`}
          onPointerDown={(e) => { if (mode !== "slider") return; (e.target as HTMLElement).setPointerCapture?.(e.pointerId); drag(e); }}
          onPointerMove={(e) => { if (mode === "slider" && e.buttons) drag(e); }}>
          <Visual m={after} vref={vref(1)} className="base" />
          <div className="top" style={mode === "slider" ? { clipPath: `inset(0 ${100 - split}% 0 0)` } : undefined}>
            <Visual m={before} vref={vref(0)} />
          </div>
          {mode === "slider" && <div className="handle" style={{ left: `${split}%` }} />}
          <span className="tag l">{before?.label}</span><span className="tag r">{after?.label}</span>
        </div>
      )}
      <div className="cmp-actions">
        {hasVideo && <button className="mini" onClick={toggle}>{playing ? "⏸ Pause" : "▶ Lire ensemble"}</button>}
        {both3d && <button className="mini" title="Les deux modèles côte à côte, caméras liées"
          onClick={() => setState({ modal: { kind: "mesh", title: `${before!.label} / ${after!.label}`, url: before!.glb!, compareUrl: after!.glb!, baseTitle: before!.label, compareTitle: after!.label } })}>
          3D côte à côte</button>}
        {!big && (before || after) && <button className="mini" onClick={() => setState({ modal: { kind: "compare", title: `${before?.label || "Avant"} / ${after?.label || "Après"}`, before, after, mode } as any })}>Agrandir</button>}
      </div>
    </div>
  );
}


/** Contenu de la node « Avant / après » (les deux références viennent de l'état du graphe). */
export function CompareNode({ n, refs, variant }: { n: any; refs: any; variant: (id?: string | null) => VariantLite | undefined }) {
  const p = n.params || {};
  const before = useMedia(refs?.before, variant, p.label_before || "Avant");
  const after = useMedia(refs?.after, variant, p.label_after || "Après");
  if (!before && !after) return <div className="snode-text"><i>Branchez deux résultats (image, vidéo ou 3D) sur « Avant » et « Après ».</i></div>;
  return <CompareView before={before} after={after} mode={(p.mode || "slider") as Mode}
    onMode={(m) => applyOps([{ op: "update_node", node_id: n.id, params: { mode: m } }]).catch(() => undefined)} />;
}
