import type { GEdge, GNode, NodeTypeDef, PortDef } from "../api";

export const TYPE_COLORS: Record<string, string> = {
  Prompt: "#e3b341", PromptList: "#f0883e", Image: "#3fb950", ImageList: "#2ea043", Mask: "#8b949e",
  ReferenceCollection: "#a371f7", Mesh: "#58a6ff", Materials: "#79c0ff", RiggedMesh: "#d2a8ff", Animation: "#ff7b72",
  Video: "#f778ba", Report: "#c9d1d9", Files: "#ffa657", Media: "#e2c08d",
};

/** Entrées affichées. `connected` : entrées portant une noodle — jamais masquées, même si le mode de la node ne les
 * utilise plus (sinon la noodle deviendrait invisible et impossible à supprimer). */
/** Les deux nodes d'image générative (Générer : texte → image ; Éditer : image + consigne). */
export const IMAGE_GEN = ["image.generate", "image.edit"];

export function inputPorts(n: GNode, nt?: NodeTypeDef, connected?: Set<string>): PortDef[] {
  if (!nt) return [];
  const out: PortDef[] = [];
  for (const p of nt.inputs) {
    if (p.dynamic_prefix) {
      if (n.type === "reference.collection") {
        for (const s of (n.params?.slots || []) as any[]) {
          out.push({ ...p, name: `${p.dynamic_prefix}${s.id}`, label: `${s.label || s.id} (${s.role || "sujet"})`, dynamic_prefix: undefined });
        }
      }
      continue;
    }
    const keep = connected?.has(p.name);
    if (IMAGE_GEN.includes(n.type) && !keep) {
      const mode = n.execution?.mode || "single";
      if (mode === "foreach_prompts" && p.name === "prompt") continue;
      if (mode !== "foreach_prompts" && p.name === "prompts") continue;
      if (p.name === "image_b" && n.params?.engine !== "openai") continue; // seconde image : moteur OpenAI seulement
    }
    if (n.type === "video.generate" && !keep) {
      const m = n.params?.mode || "image_to_video";
      if (p.name === "image" && !["image_to_video", "first_last_to_video"].includes(m)) continue;
      if (p.name === "last_image" && m !== "first_last_to_video") continue;
      if (p.name === "references" && m !== "reference_to_video") continue;
    }
    out.push(p);
  }
  return out;
}

export function outputPorts(n: GNode, nt?: NodeTypeDef): PortDef[] {
  if (!nt) return [];
  const out: PortDef[] = [];
  const foreach = IMAGE_GEN.includes(n.type) && n.execution?.mode === "foreach_prompts";
  for (const p of nt.outputs) {
    if (IMAGE_GEN.includes(n.type)) {
      if (foreach && p.name === "image") continue;
      if (!foreach && (p.name === "images" || p.dynamic_prefix)) continue;
      if (foreach && p.dynamic_prefix) {
        for (const it of (n.items || []).filter((i) => i.status !== "archived").sort((a, b) => (a.order || 0) - (b.order || 0))) {
          out.push({ ...p, name: `image:${it.item_key}`, label: it.label || it.item_key, dynamic_prefix: undefined });
        }
        continue;
      }
    }
    if (n.type === "text.randomize" && p.dynamic_prefix) { // une sortie par prompt écrit : prompt:1 … prompt:N
      const count = Math.max(1, Number(n.params?.count ?? 5));
      for (let i = 1; i <= count; i++) out.push({ ...p, name: `prompt:${i}`, label: `Prompt ${i}`, dynamic_prefix: undefined });
      continue;
    }
    if (p.dynamic_prefix) continue;
    out.push(p);
  }
  return out;
}

export function portType(n: GNode | undefined, nt: NodeTypeDef | undefined, name: string, dir: "in" | "out"): string | undefined {
  if (!n || !nt) return undefined;
  const list = dir === "in" ? nt.inputs : nt.outputs;
  for (const p of list) {
    if (p.name === name) return p.type;
    if (p.dynamic_prefix && name.startsWith(p.dynamic_prefix)) return p.type;
  }
  return undefined;
}

export function slug(s: string) {
  return (s || "x").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "x";
}

export function uniqueItemKey(edges: GEdge[], target: string, base: string) {
  const used = new Set(edges.filter((e) => e.to[0] === target).map((e) => e.item_key));
  let k = base, i = 2;
  while (used.has(k)) k = `${base}_${i++}`;
  return k;
}
