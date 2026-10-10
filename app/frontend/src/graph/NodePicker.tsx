// Menu « ajouter une node » : s'ouvre quand on lâche une noodle dans le vide (ou double-clic sur le fond).
// Recherche : chaque mot tapé doit apparaître, ou abréger un mot (« img » trouve « Image »), sans tenir compte des accents.
import { useEffect, useMemo, useRef, useState } from "react";
import { NodeTypeDef, PortDef } from "../api";
import { useStore } from "../store";
import { TYPE_COLORS } from "./ports";

export type PickerFrom = { nodeId: string; handleId: string; handleType: "source" | "target"; portType: string };
export type PickerState = { x: number; y: number; flow: { x: number; y: number }; from?: PickerFrom };

const norm = (s: string) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

/** Score d'un mot tapé dans un texte : sous-chaîne (meilleur, surtout en début de mot), sinon abréviation d'UN mot du
 * texte (même première lettre, lettres dans l'ordre : « img » → « image », mais pas « importer un maillage »). 0 = absent. */
function wordScore(word: string, text: string, fuzzy: boolean): number {
  const i = text.indexOf(word);
  if (i >= 0) return 100 - Math.min(i, 50) + (i === 0 || /[^a-z0-9]/.test(text[i - 1]) ? 30 : 0);
  if (!fuzzy) return 0;
  let best = 0;
  for (const tok of text.split(/[^a-z0-9]+/)) {
    if (!tok || tok[0] !== word[0]) continue;
    let pos = 0, gaps = 0, ok = true;
    for (const ch of word.slice(1)) {
      const j = tok.indexOf(ch, pos + 1);
      if (j < 0) { ok = false; break; }
      gaps += j - pos - 1;
      pos = j;
    }
    if (ok) best = Math.max(best, 60 - gaps * 3);
  }
  return best;
}

function score(q: string, t: NodeTypeDef): number {
  const words = norm(q).split(/\s+/).filter(Boolean);
  if (!words.length) return 1;
  // libellé, identifiant et catégorie : abréviations acceptées ; description : mot exact seulement
  const fields: [string, number, boolean][] = [[norm(t.label), 3, true], [norm(t.type), 2, true], [norm(t.category), 1.5, true],
    [norm(t.description || ""), 0.5, false]];
  let total = 0;
  for (const w of words) {
    const best = Math.max(...fields.map(([f, k, fz]) => wordScore(w, f, fz) * k));
    if (best <= 0) return 0; // chaque mot doit correspondre quelque part
    total += best;
  }
  return total;
}

/** Port de la nouvelle node à brancher : entrée compatible (si on tire une sortie) ou sortie compatible (si on tire une entrée). */
export function matchPort(t: NodeTypeDef, from: PickerFrom | undefined, compat: Record<string, string[]>): PortDef | null | undefined {
  if (!from) return undefined;
  if (from.handleType === "source") {
    const ok = t.inputs.filter((p) => !p.dynamic_prefix && (compat[from.portType] || [from.portType]).includes(p.type));
    return ok.find((p) => p.type === from.portType) || ok[0] || null;
  }
  const ok = t.outputs.filter((p) => (compat[p.type] || [p.type]).includes(from.portType));
  return ok.find((p) => p.type === from.portType) || ok[0] || null;
}

export function NodePicker({ st, onPick, onClose, bounds }: {
  st: PickerState; onPick: (t: NodeTypeDef, port: PortDef | null | undefined) => void; onClose: () => void; bounds: { w: number; h: number };
}) {
  const nts = useStore((s) => s.nodeTypes);
  const compat = useStore((s) => s.compatibility);
  const caps = useStore((s) => s.capabilities);
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  const list = useMemo(() => {
    // toutes les nodes sont proposées ; la recherche filtre au fur et à mesure. Avec une noodle, celles qu'on peut y
    // brancher viennent en premier, les autres restent listées (créées sans branchement).
    const rows = Object.values(nts).map((t) => ({ t, port: matchPort(t, st.from, compat), s: score(q, t) }))
      .filter((r) => r.s > 0);
    const ok = (r: { port: PortDef | null | undefined }) => (st.from && r.port === null ? 1 : 0);
    rows.sort((a, b) => ok(a) - ok(b) || b.s - a.s || a.t.category.localeCompare(b.t.category) || a.t.label.localeCompare(b.t.label));
    return rows;
  }, [nts, compat, q, st.from]);

  useEffect(() => { setIdx(0); }, [q]);
  useEffect(() => { listRef.current?.querySelector(".np-row.on")?.scrollIntoView({ block: "nearest" }); }, [idx]);

  const unavailable = (t: NodeTypeDef) => (t.default_steps || []).some((s) => caps?.capabilities?.[s.capability]?.status === "indisponible");
  const W = 300, H = 340;
  const left = Math.max(8, Math.min(st.x, bounds.w - W - 8)), top = Math.max(8, Math.min(st.y, bounds.h - H - 8));
  return (
    <div className="np" style={{ left, top, width: W }} onMouseDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
      <input autoFocus placeholder={st.from ? `Ajouter une node (noodle ${st.from.portType})… tapez pour filtrer` : "Ajouter une node… tapez pour filtrer"} value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setIdx((i) => Math.min(i + 1, list.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(i - 1, 0)); }
          else if (e.key === "Enter") { e.preventDefault(); const r = list[idx]; if (r) onPick(r.t, r.port); }
          else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); }
        }} />
      <div className="np-list" ref={listRef} style={{ maxHeight: H - 44 }}>
        {list.map((r, i) => (
          <div key={r.t.type}>
            {st.from && r.port === null && (i === 0 || list[i - 1].port !== null) && (
              <div className="np-sep">Autres nodes — non branchables sur {st.from.portType}, créées sans noodle</div>
            )}
            <div className={`np-row ${i === idx ? "on" : ""} ${st.from && r.port === null ? "nolink" : ""}`} onMouseEnter={() => setIdx(i)}
              onClick={() => onPick(r.t, r.port)} title={r.t.description}>
              <span className="np-label">{r.t.label}{unavailable(r.t) && <span className="pill">indisponible</span>}</span>
              <span className="np-sub">
                {r.t.category}
                {r.port && <> · <i style={{ color: TYPE_COLORS[r.port.type] || "#aaa" }}>{st.from?.handleType === "source" ? "→ " : "← "}{r.port.label}</i></>}
              </span>
            </div>
          </div>
        ))}
        {!list.length && <div className="np-empty">Aucune node pour « {q} »</div>}
      </div>
    </div>
  );
}
