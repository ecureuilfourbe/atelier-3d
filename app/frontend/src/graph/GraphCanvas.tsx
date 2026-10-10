import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, useReactFlow, Connection, Edge, Node, NodeChange,
  applyNodeChanges, Handle, Position, NodeProps, Panel, SelectionMode, OnSelectionChangeParams, EdgeChange,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { GNode, GraphDoc, NodeTypeDef, PortDef, VariantLite } from "../api";
import { applyOps } from "../actions";
import { copySelection, cutSelection, deleteSelection, duplicateSelection, pasteFromButton, selectAll, setPasteAnchor } from "../clipboard";
import { getState, setState, STATUS_LABEL, toast, useStore } from "../store";
import { StudioNode, StudioData } from "./StudioNode";
import { portType, slug, TYPE_COLORS, uniqueItemKey } from "./ports";
import { NodePicker, PickerState } from "./NodePicker";

/** Opérations pour brancher une noodle (même règles que le glisser-déposer : entrée simple remplacée, item foreach créé). */
function edgeOps(doc: GraphDoc, c: { source: string; sourceHandle: string; target: string; targetHandle: string },
  nts: Record<string, NodeTypeDef>, extra: Record<string, GNode> = {}): any[] {
  const find = (id: string) => extra[id] || doc.nodes.find((n) => n.id === id)!;
  const src = find(c.source), dst = find(c.target);
  const port = nts[dst.type]?.inputs.find((p) => p.name === c.targetHandle);
  const ops: any[] = [];
  const existing = doc.edges.filter((e) => e.to[0] === dst.id && e.to[1] === c.targetHandle);
  if (port && !port.multi) existing.forEach((e) => ops.push({ op: "remove_edge", edge_id: e.id }));
  const edge: any = { from: [c.source, c.sourceHandle], to: [c.target, c.targetHandle] };
  if (c.targetHandle === "prompts") {
    edge.item_key = uniqueItemKey(doc.edges, dst.id, slug(src.label || src.id.replace(/^p_/, "")));
    edge.order = existing.length;
  }
  ops.push({ op: "add_edge", edge });
  return ops;
}

function GroupNode({ data }: NodeProps) {
  const d = data as any;
  return (
    <div className="groupbox" style={{ width: d.w, height: d.h }}>
      <div className="grouphead nodrag">
        <span>▣ {d.label}</span>
        <button onClick={d.toggle}>{d.collapsed ? "Déplier" : "Replier"}</button>
      </div>
    </div>
  );
}

function MacroNode({ data }: NodeProps) {
  const d = data as any;
  return (
    <div className="snode macro">
      <Handle type="target" position={Position.Left} id="in" />
      <div className="snode-head"><div className="snode-title">▣ {d.label}</div><div className="snode-sub">macro · {d.count} étapes</div></div>
      <div className="snode-items">{d.summary.map((s: any) => <div className="item-row" key={s.id}><span>{s.label}</span><span className={`dot st-${s.status}`} title={STATUS_LABEL[s.status] || s.status} /></div>)}</div>
      <div className="snode-actions nodrag"><button onClick={d.toggle}>Déplier</button></div>
      <Handle type="source" position={Position.Right} id="out" />
    </div>
  );
}

const nodeTypes = { studio: StudioNode, groupbox: GroupNode, macro: MacroNode };

function Canvas() {
  const view = useStore((s) => s.view);
  const nodeTypesDef = useStore((s) => s.nodeTypes);
  const compat = useStore((s) => s.compatibility);
  const selected = useStore((s) => s.selected);
  const selection = useStore((s) => s.selection);
  const edgeSelection = useStore((s) => s.edgeSelection);
  const selectMode = useStore((s) => s.selectMode);
  const rf = useReactFlow();
  const wrapper = useRef<HTMLDivElement>(null);
  const pointer = useRef<{ x: number; y: number } | null>(null);
  const [picker, setPicker] = useState<PickerState | null>(null);
  const selBefore = useRef<string[]>([]); // sélection au moment du clic (pour Ctrl+clic)
  const ctrlLock = useRef<{ until: number; ids: string[] }>({ until: 0, ids: [] });
  const [rfNodes, setRfNodes] = useState<Node[]>([]);
  const dragging = useRef(false);
  const fitted = useRef<string | null>(null);

  const variantIndex = useMemo(() => {
    const m = new Map<string, VariantLite>();
    if (view) for (const s of Object.values(view.slots || {})) for (const v of s.variants) m.set(v.id, v);
    return m;
  }, [view]);
  const slotsByNode = useMemo(() => {
    const m = new Map<string, any[]>();
    if (view) for (const s of Object.values(view.slots || {})) {
      const a = m.get(s.node_id) || [];
      a.push(s);
      m.set(s.node_id, a);
    }
    return m;
  }, [view]);

  const groups = view?.graph.groups || [];
  const collapsedMembers = useMemo(() => {
    const m = new Map<string, string>();
    for (const g of groups) if (g.collapsed) for (const id of g.node_ids) m.set(id, g.id);
    return m;
  }, [groups]);

  const toggleGroup = useCallback((gid: string) => {
    const v = getState().view;
    if (!v) return;
    const gs = (v.graph.groups || []).map((g) => (g.id === gid ? { ...g, collapsed: !g.collapsed } : g));
    applyOps([{ op: "set_groups", groups: gs }]).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!view || dragging.current) return;
    const doc = view.graph;
    const byId = new Map(doc.nodes.map((n) => [n.id, n]));
    const out: Node[] = [];
    for (const g of groups) {
      const members = g.node_ids.map((id) => byId.get(id)).filter(Boolean) as GNode[];
      if (!members.length) continue;
      const xs = members.map((n) => n.position?.x || 0), ys = members.map((n) => n.position?.y || 0);
      const x0 = Math.min(...xs) - 30, y0 = Math.min(...ys) - 50, x1 = Math.max(...xs) + 330, y1 = Math.max(...ys) + 330;
      if (g.collapsed) {
        out.push({ id: `macro_${g.id}`, type: "macro", position: { x: x0 + 40, y: y0 + 60 }, data: {
          label: g.label, count: members.length, toggle: () => toggleGroup(g.id),
          summary: members.map((m) => ({ id: m.id, label: m.label || m.id, status: view.status?.[m.id]?.status || "missing" })),
        } });
      } else {
        out.push({ id: `grp_${g.id}`, type: "groupbox", position: { x: x0, y: y0 }, draggable: false, selectable: false, zIndex: -1,
          data: { label: g.label, w: x1 - x0, h: y1 - y0, collapsed: false, toggle: () => toggleGroup(g.id) } });
      }
    }
    for (const n of doc.nodes) {
      if (collapsedMembers.has(n.id)) continue;
      const nt = nodeTypesDef[n.type];
      let previewVariant: VariantLite | undefined;
      const st = view.status?.[n.id];
      if (st?.ref?.kind === "variant") previewVariant = variantIndex.get(st.ref.variant_id);
      const data: StudioData = {
        node: n, nt, status: st, previewVariant, connectedIn: doc.edges.filter((e) => e.to[0] === n.id).map((e) => e.to[1]),
        slotsOf: (id) => slotsByNode.get(id) || [], variant: (id) => (id ? variantIndex.get(id) : undefined),
      };
      out.push({ id: n.id, type: "studio", position: n.position || { x: 0, y: 0 }, data: data as any,
        selected: n.id === selected || selection.includes(n.id) });
    }
    // conserver les dimensions déjà mesurées : sinon React Flow masque les nodes jusqu'à une nouvelle mesure, qui n'arrive
    // pas si leur taille n'a pas changé (constaté après annuler/rétablir)
    setRfNodes((prev) => {
      const m = new Map(prev.map((p) => [p.id, p]));
      return out.map((n) => { const p = m.get(n.id); return p?.measured ? { ...n, measured: p.measured } : n; });
    });
  }, [view, nodeTypesDef, selected, selection, variantIndex, slotsByNode, groups, collapsedMembers, toggleGroup]);

  // collage à l'endroit de la souris si elle est sur le canevas, sinon au centre de la vue
  useEffect(() => {
    setPasteAnchor(() => {
      if (pointer.current) return pointer.current;
      const r = wrapper.current?.getBoundingClientRect();
      return r ? rf.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 }) : null;
    });
  }, [rf]);

  useEffect(() => {
    if (view && fitted.current !== view.graph.id) {
      fitted.current = view.graph.id;
      setTimeout(() => rf.fitView({ padding: 0.2 }), 80); // marge : la barre d'outils recouvre le haut du canevas
    }
  }, [view, rf]);

  const edges: Edge[] = useMemo(() => {
    if (!view) return [];
    const doc = view.graph;
    const byId = new Map(doc.nodes.map((n) => [n.id, n]));
    const res: Edge[] = [];
    for (const e of doc.edges) {
      let source = e.from[0], target = e.to[0], sh: string | null = e.from[1], th: string | null = e.to[1];
      const gs = collapsedMembers.get(source), gt = collapsedMembers.get(target);
      if (gs && gs === gt) continue;
      if (gs) { source = `macro_${gs}`; sh = "out"; }
      if (gt) { target = `macro_${gt}`; th = "in"; }
      const t = portType(byId.get(e.from[0]), nodeTypesDef[byId.get(e.from[0])?.type || ""], e.from[1], "out") || "";
      const pinned = e.link?.mode === "pinned";
      const sel = edgeSelection.includes(e.id);
      res.push({
        id: e.id, source, target, sourceHandle: sh, targetHandle: th, selected: sel, interactionWidth: 24,
        label: e.item_key ? `${e.item_key} #${(e.order ?? 0) + 1}` : pinned ? "épinglé" : undefined,
        style: { stroke: sel ? "#ffffff" : TYPE_COLORS[t] || "#888", strokeWidth: sel ? 4 : 2, strokeDasharray: pinned ? "6 4" : undefined },
        labelStyle: { fontSize: 10, fill: "#cfd6e4" }, labelBgStyle: { fill: "#1d2230" },
        animated: !!view.slots && Object.values(view.slots).some((s) => s.node_id === e.to[0] && s.jobs.some((j) => j.status === "running")),
      });
    }
    return res;
  }, [view, nodeTypesDef, collapsedMembers, edgeSelection]);

  // sélection des noodles (clic, Ctrl+clic, rectangle) : React Flow propose, l'état partagé décide (graphe contrôlé)
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    const sel = new Set(getState().edgeSelection);
    let changed = false;
    for (const c of changes) {
      if (c.type !== "select") continue;
      changed = true;
      if (c.selected) sel.add(c.id); else sel.delete(c.id);
    }
    if (changed) setState({ edgeSelection: [...sel] });
  }, []);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setRfNodes((nds) => applyNodeChanges(changes, nds));
  }, []);

  // sélection (clic, Ctrl+clic, rectangle) -> état partagé ; l'inspecteur montre la dernière node ajoutée
  const onSelectionChange = useCallback(({ nodes }: OnSelectionChangeParams) => {
    const ids = nodes.filter((n) => n.type === "studio").map((n) => n.id);
    // juste après un clic, React Flow peut notifier une sélection périmée : on garde celle calculée au clic
    const lock = ctrlLock.current;
    if (Date.now() < lock.until && !(ids.length === lock.ids.length && ids.every((x) => lock.ids.includes(x)))) return;
    const s = getState();
    const same = ids.length === s.selection.length && ids.every((x) => s.selection.includes(x));
    if (same && (ids.length === 0 || (s.selected !== null && ids.includes(s.selected)))) return;
    const added = ids.filter((x) => !s.selection.includes(x));
    const primary = added.length ? added[added.length - 1] : s.selected && ids.includes(s.selected) ? s.selected : ids[ids.length - 1] ?? null;
    setState({ selection: ids, selected: primary });
  }, []);

  // Clic sur une node : la sélection est mise à jour immédiatement (sans attendre la notification de React Flow), pour que
  // des clics rapprochés s'enchaînent correctement. Ctrl/Cmd+clic ajoute ou retire la node, d'après le modificateur du
  // clic lui-même (robuste même si la touche Ctrl n'a pas été vue au clavier).
  const onNodeClick = useCallback((e: React.MouseEvent, n: Node) => {
    if (n.type !== "studio") return;
    const before = selBefore.current;
    const next = e.ctrlKey || e.metaKey ? (before.includes(n.id) ? before.filter((x) => x !== n.id) : [...before, n.id]) : [n.id];
    ctrlLock.current = { until: Date.now() + 250, ids: next };
    setState({ selection: next, selected: next.includes(n.id) ? n.id : next[next.length - 1] ?? null });
  }, []);

  const onNodeDragStart = useCallback(() => { dragging.current = true; }, []);
  const onNodeDragStop = useCallback((_: any, __: Node, nodes: Node[]) => {
    dragging.current = false;
    const ops = nodes.filter((n) => n.type === "studio").map((n) => ({ op: "update_node", node_id: n.id, position: { x: Math.round(n.position.x), y: Math.round(n.position.y) } }));
    if (ops.length) applyOps(ops, { silent: true }).catch(() => undefined);
  }, []);

  const isValidConnection = useCallback((c: Connection | Edge) => {
    const v = getState().view;
    if (!v) return false;
    const src = v.graph.nodes.find((n) => n.id === c.source), dst = v.graph.nodes.find((n) => n.id === c.target);
    const st = portType(src, nodeTypesDef[src?.type || ""], c.sourceHandle || "", "out");
    const dt = portType(dst, nodeTypesDef[dst?.type || ""], c.targetHandle || "", "in");
    if (!st || !dt) return false;
    return (compat[st] || [st]).includes(dt);
  }, [nodeTypesDef, compat]);

  const onConnect = useCallback((c: Connection) => {
    const v = getState().view;
    if (!v) return;
    const src = v.graph.nodes.find((n) => n.id === c.source)!, dst = v.graph.nodes.find((n) => n.id === c.target)!;
    const st = portType(src, nodeTypesDef[src.type], c.sourceHandle || "", "out");
    const dt = portType(dst, nodeTypesDef[dst.type], c.targetHandle || "", "in");
    if (!st || !dt || !(compat[st] || [st]).includes(dt)) {
      toast(`Branchement refusé : ${st} → ${dt} (types incompatibles). Une ${st} ne peut pas alimenter une entrée ${dt}.`, "error");
      return;
    }
    const ops = edgeOps(v.graph, { source: c.source, sourceHandle: c.sourceHandle || "", target: c.target, targetHandle: c.targetHandle || "" }, nodeTypesDef);
    applyOps(ops).catch(() => undefined);
  }, [nodeTypesDef, compat]);

  // Noodle lâchée dans le vide : menu de recherche de nodes, la node choisie est créée là et branchée
  const onConnectEnd = useCallback((event: MouseEvent | TouchEvent, cs: any) => {
    if (cs?.isValid) return;
    if (cs?.toHandle) { toast("Branchement incompatible : vérifiez le type des ports.", "error"); return; }
    if (cs?.toNode || !cs?.fromNode || !cs?.fromHandle || !wrapper.current) return;
    const v = getState().view;
    const fromDoc = v?.graph.nodes.find((n) => n.id === cs.fromNode.id);
    if (!fromDoc) return;
    const handleType = cs.fromHandle.type as "source" | "target";
    const pType = portType(fromDoc, nodeTypesDef[fromDoc.type], cs.fromHandle.id || "", handleType === "source" ? "out" : "in");
    if (!pType) return;
    const pt = "changedTouches" in event ? event.changedTouches[0] : event;
    const r = wrapper.current.getBoundingClientRect();
    setPicker({ x: pt.clientX - r.left, y: pt.clientY - r.top, flow: rf.screenToFlowPosition({ x: pt.clientX, y: pt.clientY }),
      from: { nodeId: fromDoc.id, handleId: cs.fromHandle.id || "", handleType, portType: pType } });
  }, [nodeTypesDef, rf]);

  const pickNode = useCallback((t: NodeTypeDef, port: PortDef | null | undefined) => {
    const v = getState().view;
    const pk = picker;
    setPicker(null);
    if (!v || !pk) return;
    const base = t.type.split(".").pop() || "node";
    let id = base, i = 2;
    while (v.graph.nodes.some((n) => n.id === id)) id = `${base}_${i++}`;
    const from = pk.from;
    const pos = from?.handleType === "target" ? { x: pk.flow.x - 300, y: pk.flow.y - 40 } : { x: pk.flow.x, y: pk.flow.y - 40 };
    const node: GNode = { id, type: t.type, params: {}, position: { x: Math.round(pos.x), y: Math.round(pos.y) } };
    const ops: any[] = [{ op: "add_node", node }];
    if (from && port) {
      const c = from.handleType === "source"
        ? { source: from.nodeId, sourceHandle: from.handleId, target: id, targetHandle: port.name }
        : { source: id, sourceHandle: port.name, target: from.nodeId, targetHandle: from.handleId };
      ops.push(...edgeOps(v.graph, c, nodeTypesDef, { [id]: node }));
    } else if (from) {
      toast(`« ${t.label} » n'a pas de port compatible avec ${from.portType} : node créée sans noodle.`, "info");
    }
    applyOps(ops).then(() => setState({ selected: id, selection: [id] })).catch(() => undefined);
  }, [picker, nodeTypesDef]);

  // Suppr : nodes et noodles en UNE seule modification (deux requêtes concurrentes se heurteraient à la révision)
  const onDelete = useCallback(({ nodes: nds, edges: eds }: { nodes: Node[]; edges: Edge[] }) => {
    const gone = new Set(nds.filter((n) => n.type === "studio").map((n) => n.id));
    const ops: any[] = [...gone].map((id) => ({ op: "remove_node", node_id: id }));
    for (const e of eds) {
      if (e.source.startsWith("macro_") || e.target.startsWith("macro_")) continue;
      if (gone.has(e.source) || gone.has(e.target)) continue; // retirée avec sa node
      ops.push({ op: "remove_edge", edge_id: e.id });
    }
    if (ops.length) applyOps(ops).then(() => setState(gone.size ? { selection: [], selected: null, edgeSelection: [] } : { edgeSelection: [] }))
      .catch(() => undefined);
  }, []);

  if (!view) return <div className="empty">Aucun graphe ouvert. Créez un graphe vierge (＋ Nouveau), partez d'un préréglage de la bibliothèque ou importez un JSON.</div>;
  return (
    <div ref={wrapper} className="canvas-wrap"
      onPointerDownCapture={() => { const s = getState(); selBefore.current = s.selection.length ? [...s.selection] : s.selected ? [s.selected] : []; }}
      onDoubleClick={(e) => {
        // double-clic sur le fond : même menu, sans branchement
        if (!(e.target as HTMLElement).classList.contains("react-flow__pane") || !wrapper.current) return;
        const r = wrapper.current.getBoundingClientRect();
        setPicker({ x: e.clientX - r.left, y: e.clientY - r.top, flow: rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }) });
      }}
      onMouseMove={(e) => { pointer.current = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }); }}
      onMouseLeave={() => { pointer.current = null; }}>
    <ReactFlow
      nodes={rfNodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onConnect={onConnect}
      isValidConnection={isValidConnection} onNodeDragStart={onNodeDragStart} onNodeDragStop={onNodeDragStop}
      onDelete={onDelete} onPaneClick={() => { setPicker(null); setState({ selected: null, selection: [], edgeSelection: [] }); }}
      onEdgesChange={onEdgesChange}
      onSelectionChange={onSelectionChange} onNodeClick={onNodeClick}
      deleteKeyCode={["Delete", "Backspace"]} minZoom={0.1} maxZoom={2.5} proOptions={{ hideAttribution: true }} colorMode="dark"
      selectionOnDrag={selectMode} panOnDrag={selectMode ? [1, 2] : true} selectionMode={SelectionMode.Partial}
      multiSelectionKeyCode={["Control", "Meta"]} selectionKeyCode="Shift"
      onConnectEnd={onConnectEnd} zoomOnDoubleClick={false} onMoveStart={() => setPicker(null)}
    >
      <Panel position="top-left" className="canvas-tools">
        <div className="seg" role="group" aria-label="Mode du canevas">
          <button className={!selectMode ? "on" : ""} onClick={() => setSelectMode(false)} title="Mode déplacement (H) : glisser déplace la vue ; Maj+glisser trace un rectangle de sélection"><span className="ico">✋</span><span className="lbl">Déplacer</span></button>
          <button className={selectMode ? "on" : ""} onClick={() => setSelectMode(true)} title="Mode sélection (V) : glisser trace un rectangle de sélection ; clic molette ou droit déplace la vue"><span className="ico">⬚</span><span className="lbl">Sélection</span></button>
        </div>
        <button onClick={selectAll} title="Tout sélectionner (Ctrl+A)"><span className="ico">▦</span><span className="lbl">Tout</span></button>
        <button disabled={!selection.length && !selected} onClick={() => copySelection()} title="Copier (Ctrl+C)"><span className="ico">⧉</span><span className="lbl">Copier</span></button>
        <button disabled={!selection.length && !selected} onClick={() => cutSelection()} title="Couper (Ctrl+X)"><span className="ico">✂</span><span className="lbl">Couper</span></button>
        <button onClick={() => pasteFromButton()} title="Coller (Ctrl+V) · Ctrl+Maj+V : avec les connexions entrantes"><span className="ico">📋</span><span className="lbl">Coller</span></button>
        <button disabled={!selection.length && !selected} onClick={duplicateSelection} title="Dupliquer (Ctrl+D)"><span className="ico">⿻</span><span className="lbl">Dupliquer</span></button>
        <button className="danger" disabled={!selection.length && !selected && !edgeSelection.length} onClick={deleteSelection} title="Supprimer les nodes ou noodles sélectionnées (Suppr)"><span className="ico">🗑</span><span className="lbl">Supprimer</span></button>
        {selection.length > 1 && <span className="selcount">{selection.length} sélectionnées</span>}
        {edgeSelection.length > 0 && <span className="selcount">{edgeSelection.length} noodle{edgeSelection.length > 1 ? "s" : ""} · Suppr pour supprimer</span>}
      </Panel>
      {view.graph.nodes.length === 0 && (
        <Panel position="top-center" className="empty-graph">
          Graphe vierge : ajoutez des nodes depuis la bibliothèque, ou collez des nodes copiées (Ctrl+V).
        </Panel>
      )}
      <Background gap={24} size={1} />
      <Controls position="bottom-left" />
      <MiniMap pannable zoomable position="bottom-right" nodeColor={(n) => (n.type === "studio" ? "#3a4258" : "#222")} />
    </ReactFlow>
    {picker && <NodePicker st={picker} onPick={pickNode} onClose={() => setPicker(null)}
      bounds={{ w: wrapper.current?.clientWidth || 800, h: wrapper.current?.clientHeight || 600 }} />}
    </div>
  );
}

export function setSelectMode(on: boolean) {
  setState({ selectMode: on });
  try { localStorage.setItem("a3d_select_mode", on ? "1" : "0"); } catch { /* préférence non conservée */ }
}

export function GraphCanvas() {
  return <ReactFlowProvider><Canvas /></ReactFlowProvider>;
}
