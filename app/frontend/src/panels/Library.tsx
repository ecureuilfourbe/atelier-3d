import { useEffect, useState } from "react";
import { api } from "../api";
import { applyOps, deleteGraph, errText, openGraph, refreshGraphList, restoreGraph } from "../actions";
import { createBlankGraph } from "../clipboard";
import { getState, toast, useStore } from "../store";

const CATS = ["Texte", "Image", "3D", "Animation", "Vidéo", "Comparer"];

export function Library() {
  const nts = useStore((s) => s.nodeTypes);
  const caps = useStore((s) => s.capabilities);
  const view = useStore((s) => s.view);
  const [presets, setPresets] = useState<Record<string, string>>({});
  const graphs = useStore((s) => s.graphs);
  const trash = useStore((s) => s.trash);
  const [showGraphs, setShowGraphs] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  useEffect(() => { api.presets().then((r) => setPresets(r.presets)).catch(() => undefined); }, []);
  const add = (type: string) => {
    if (!view) { toast("Ouvrez ou créez d'abord un graphe", "error"); return; }
    const ns = getState().view!.graph.nodes;
    const maxX = Math.max(0, ...ns.map((n) => n.position?.x || 0));
    const base = type.split(".").pop();
    let id = base!, i = 2;
    while (ns.some((n) => n.id === id)) id = `${base}_${i++}`;
    applyOps([{ op: "add_node", node: { id, type, params: {}, position: { x: maxX + 380, y: 80 } } }]).catch(() => undefined);
  };
  const fromPreset = async (p: string) => {
    try {
      const r = await api.fromPreset(p, {});
      await refreshGraphList();
      await openGraph(r.graph.id);
      toast(`Graphe « ${presets[p]} » créé`, "ok");
    } catch (e) { toast(errText(e), "error"); }
  };
  const capStatus = (t: string) => {
    const steps = nts[t]?.default_steps || [];
    const bad = steps.find((s) => caps?.capabilities?.[s.capability]?.status === "indisponible");
    return bad ? `indisponible : ${caps.capabilities[bad.capability].reason}` : "";
  };
  return (
    <div className="library">
      <h4>Nouveau</h4>
      <button className="lib-item" onClick={createBlankGraph} title="Graphe sans aucune node">＋ Graphe vierge</button>
      <h4 className="clickable" onClick={() => setShowGraphs((x) => !x)}>{showGraphs ? "▾" : "▸"} Graphes ({graphs.length})</h4>
      {showGraphs && graphs.map((g) => (
        <div key={g.id} className={`lib-graph ${view?.graph.id === g.id ? "cur" : ""}`}>
          <button className="lib-item" onClick={() => openGraph(g.id)} title={`${g.id} · r${g.revision} · ${g.author}`}>{g.name}</button>
          <button className="mini danger" onClick={() => deleteGraph(g.id)} title="Supprimer (corbeille, restaurable)">🗑</button>
        </div>
      ))}
      <h4 className="clickable" onClick={() => setShowTrash((x) => !x)}>{showTrash ? "▾" : "▸"} Corbeille ({trash.length})</h4>
      {showTrash && (trash.length ? trash.map((g) => (
        <div key={g.id} className="lib-graph">
          <span className="lib-item trashname" title={g.id}>{g.name}</span>
          <button className="mini" onClick={() => restoreGraph(g.id)} title="Remettre dans la liste">Restaurer</button>
        </div>
      )) : <p className="hint">Vide.</p>)}
      <h4>Préréglages</h4>
      {Object.entries(presets).map(([k, label]) => <button key={k} className="lib-item" onClick={() => fromPreset(k)}>＋ {label}</button>)}
      {[...CATS, ...[...new Set(Object.values(nts).map((t) => t.category))].filter((c) => !CATS.includes(c))].map((cat) => (
        <div key={cat}>
          <h4>{cat}</h4>
          {Object.values(nts).filter((t) => t.category === cat).map((t) => {
            const un = capStatus(t.type);
            return (
              <button key={t.type} className={`lib-item ${un ? "unavail" : ""}`} title={un || t.description} onClick={() => add(t.type)}>
                {t.label}{un && <span className="pill">indisponible</span>}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}
