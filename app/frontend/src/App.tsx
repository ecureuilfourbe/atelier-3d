import { useEffect } from "react";
import { api } from "./api";
import { bootstrap, deleteGraph, errText, openGraph, redo, refreshCaps, refreshGraphList, runScope, undo } from "./actions";
import { createBlankGraph, duplicateSelection, installClipboard, selectAll } from "./clipboard";
import { GraphCanvas, setSelectMode } from "./graph/GraphCanvas";
import { Inspector } from "./panels/Inspector";
import { JobsPanel } from "./panels/JobsPanel";
import { Library } from "./panels/Library";
import { MediaLibrary } from "./panels/MediaLibrary";
import { SettingsPanel } from "./panels/Settings";
import { getState, setState, toast, useStore } from "./store";
import { Modal } from "./viewers/Modal";

function Meters() {
  const s = useStore((st) => st.system);
  if (!s) return null;
  return (
    <div className="meters">
      {s.gpu && <span title={s.gpu}>VRAM {(s.vram_used_mb / 1024).toFixed(1)}/{(s.vram_total_mb / 1024).toFixed(1)} Go · GPU {s.gpu_util}%</span>}
      <span>RAM {(s.ram_used_mb / 1024).toFixed(1)}/{(s.ram_total_mb / 1024).toFixed(1)} Go</span>
    </div>
  );
}

function Engines() {
  const caps = useStore((s) => s.capabilities);
  if (!caps) return null;
  const c = caps.capabilities || {};
  const keys: [string, string][] = [["image.create", "Krea 2"], ["image.edit", "Édition"], ["image.remove_background", "Détourage"],
    ["mesh.generate", "Pixal3D"], ["rig.mia", "Rigging MIA"], ["blender", "Blender"], ["video.minimax", "MiniMax H3"],
    ...(c["image.openai"]?.status && c["image.openai"].status !== "indisponible" ? [["image.openai", "☁ OpenAI"] as [string, string]] : [])];
  return (
    <div className="engines">
      {keys.map(([k, l]) => (
        <span key={k} className={`eng st-${c[k]?.status === "disponible" ? "ok" : c[k]?.status === "à tester" ? "warn" : "bad"}`}
          title={`${c[k]?.status || "?"}${c[k]?.reason ? " — " + c[k].reason : ""}${k === "video.minimax" && c[k]?.execution_label ? " — " + c[k].execution_label : ""}`}>{l}</span>
      ))}
      <button className="mini" onClick={refreshCaps} title="Revérifier les moteurs">↻</button>
    </div>
  );
}

function TopBar() {
  const graphs = useStore((s) => s.graphs);
  const view = useStore((s) => s.view);
  const panels = useStore((s) => s.panels);
  const hist = useStore((s) => s.history);
  const importJson = async (f?: File | null) => {
    if (!f) return;
    try {
      const doc = JSON.parse(await f.text());
      const r = await api.createGraph(doc);
      await refreshGraphList();
      await openGraph(r.graph.id);
      toast("Graphe importé", "ok");
    } catch (e) { toast("Import impossible : " + errText(e), "error"); }
  };
  return (
    <div className="topbar">
      <div className="brand">◆ Atelier 3D</div>
      <button className="mini" onClick={() => setState((s) => ({ panels: { ...s.panels, library: !s.panels.library } }))}>{panels.library ? "◧" : "☰"} Bibliothèque</button>
      <button className="mini" onClick={createBlankGraph} title="Créer un graphe vierge">＋ Nouveau</button>
      <select value={view?.graph.id || ""} onChange={(e) => { openGraph(e.target.value); e.target.blur(); /* rend le focus : Ctrl+V etc. */ }}>
        <option value="" disabled>Choisir un graphe…</option>
        {graphs.map((g) => <option key={g.id} value={g.id}>{g.name} (r{g.revision})</option>)}
      </select>
      {view && <button className="mini danger" onClick={() => deleteGraph(view.graph.id)} title="Supprimer ce graphe (corbeille, restaurable)">🗑</button>}
      <button className="mini" onClick={() => setState({ mediaOpen: true })} title="Toutes les images, vidéos et modèles 3D générés">🖼 Médiathèque</button>
      <label className="filebtn mini">Importer JSON<input type="file" accept=".json" onChange={(e) => importJson(e.target.files?.[0])} /></label>
      <button className="mini" disabled={!hist.past.length} onClick={undo} title="Annuler (Ctrl+Z)">↶</button>
      <button className="mini" disabled={!hist.future.length} onClick={redo} title="Rétablir (Ctrl+Y)">↷</button>
      {view && <button className="primary" onClick={() => runScope({ scope: "graph" }, "Exécuter le graphe")}>▶ Exécuter le graphe</button>}
      <span className="grow" />
      <Engines />
      <Meters />
      <button className="mini" onClick={() => setState({ settingsOpen: true })} title="Réglages (clé d'API OpenAI…)">⚙ Réglages</button>
      <button className="mini" onClick={() => setState((s) => ({ panels: { ...s.panels, inspector: !s.panels.inspector } }))}>Inspecteur</button>
      <button className="mini" onClick={() => setState((s) => ({ panels: { ...s.panels, jobs: !s.panels.jobs } }))}>Tâches</button>
    </div>
  );
}

function Toasts() {
  const t = useStore((s) => s.toasts);
  return (
    <div className="toasts">
      {t.map((x) => (
        <div key={x.id} className={`toast ${x.kind}`}>
          {x.text}
          {x.action && <button className="mini toast-action" onClick={() => {
            setState((s) => ({ toasts: s.toasts.filter((y) => y.id !== x.id) }));
            x.action!.run();
          }}>{x.action.label}</button>}
        </div>
      ))}
    </div>
  );
}

export function App() {
  const panels = useStore((s) => s.panels);
  useEffect(() => {
    bootstrap();
    if (window.innerWidth < 900) setState({ panels: { library: false, inspector: false, jobs: false } });
    installClipboard(); // Ctrl+C / Ctrl+X / Ctrl+V (événements natifs du presse-papiers)
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      const tag = el?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el?.isContentEditable) return;
      const k = e.key.toLowerCase(), ctrl = e.ctrlKey || e.metaKey;
      if (e.key === "Escape") {
        const s = getState();
        if (s.modal) setState({ modal: null });
        else if (s.settingsOpen) setState({ settingsOpen: false });
        else if (s.mediaOpen || s.mediaPick) setState({ mediaOpen: false, mediaPick: null });
        else setState({ selected: null, selection: [], edgeSelection: [] });
        return;
      }
      if (getState().mediaOpen || getState().mediaPick || getState().settingsOpen) return; // raccourcis du canevas inactifs pendant la médiathèque
      if (ctrl && k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      if (ctrl && (k === "y" || (e.shiftKey && k === "z"))) { e.preventDefault(); redo(); }
      if (ctrl && k === "a" && getState().view) { e.preventDefault(); selectAll(); }
      if (ctrl && k === "d" && getState().view) { e.preventDefault(); duplicateSelection(); }
      if (!ctrl && !e.altKey && k === "v") setSelectMode(true);
      if (!ctrl && !e.altKey && k === "h") setSelectMode(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className={`app ${panels.library ? "" : "no-lib"} ${panels.inspector ? "" : "no-insp"} ${panels.jobs ? "" : "no-jobs"}`}>
      <TopBar />
      {panels.library && <aside className="left"><Library /></aside>}
      <main className="center"><GraphCanvas /></main>
      {panels.inspector && <aside className="right"><Inspector /></aside>}
      {panels.jobs && <section className="bottom"><JobsPanel /></section>}
      <MediaLibrary />
      <SettingsPanel />
      <Modal />
      <Toasts />
    </div>
  );
}
