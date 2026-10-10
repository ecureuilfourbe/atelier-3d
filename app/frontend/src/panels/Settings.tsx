// Réglages : services externes (OpenAI / ChatGPT Images, Claude / Anthropic). Les clés ne sont jamais renvoyées par le
// serveur : seulement « enregistrée » et leurs 4 derniers caractères ; elles sont chiffrées sur ce PC (Windows DPAPI).
import { useState } from "react";
import { api } from "../api";
import { errText, refreshCaps } from "../actions";
import { setState, toast, useStore } from "../store";
import { RemoteAccess } from "./RemoteAccess";

const QUALITY_LABEL: Record<string, string> = {
  auto: "automatique", low: "basse (rapide, économique)", medium: "moyenne", high: "haute", xhigh: "très haute (GPT Image 2.5)", max: "maximale (GPT Image 2.5)",
};

/** Claude (Anthropic) : texte et analyse d'image seulement (« Prompts aléatoires », analyse d'« Extraction de props »). */
function ClaudeSettings() {
  const settings = useStore((s) => s.settings);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const a = settings?.anthropic || {};
  const apply = async (fn: () => Promise<any>) => {
    setBusy(true);
    try {
      const r = await fn();
      setState((s) => ({ settings: { ...(s.settings || {}), anthropic: r.anthropic } }));
      if (r.test) toast(r.test.message, r.test.ok ? "ok" : "error");
      await refreshCaps();
      return r;
    } catch (e) { toast(errText(e), "error"); }
    finally { setBusy(false); }
  };
  const save = () => apply(() => api.putAnthropic({ api_key: key.trim() })).then((r) => r && setKey(""));
  return (
    <section className="settings-section">
      <h3>Claude — Anthropic</h3>
      <p className="hint">
        Utilisé uniquement par les nodes dont le moteur est réglé sur <b>Claude</b> : « Prompts aléatoires » (écriture des
        prompts) et « Extraction de props » (analyse de l'image). Le contexte — et l'image à analyser — sont alors envoyés à
        Anthropic (service externe, facturé sur votre compte). Claude ne génère pas d'images.
      </p>
      <div className={`keystate ${a.configured ? "on" : ""}`}>
        {a.configured ? <>Clé enregistrée <code>{a.key_hint}</code> — chiffrée sur ce PC (Windows DPAPI), jamais affichée ni journalisée</>
          : "Aucune clé enregistrée : le moteur Claude est indisponible."}
      </div>
      {a.last_test && (
        <p className={a.last_test.ok ? "ok" : "err"}>
          {a.last_test.ok ? "✓ " : "✗ "}{a.last_test.message} <span className="hint">({new Date(a.last_test.at).toLocaleString("fr-FR")})</span>
        </p>
      )}
      <div className="keyrow">
        <input type="password" autoComplete="off" spellCheck={false} placeholder={a.configured ? "Remplacer la clé…" : "Clé d'API Claude (sk-ant-…)"}
          value={key} onChange={(e) => setKey(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && key.trim()) save(); }} />
        <button className="primary" disabled={!key.trim() || busy} onClick={save}>Enregistrer et tester</button>
      </div>
      <div className="section actions" style={{ border: 0 }}>
        {a.configured && <button disabled={busy} onClick={() => apply(() => api.testAnthropic())}>Tester la clé</button>}
        {a.configured && <button className="danger" disabled={busy} onClick={() => apply(() => api.deleteAnthropicKey())}>Supprimer la clé</button>}
        <a className="hint" href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">Créer ou gérer vos clés sur console.anthropic.com ↗</a>
      </div>
      {busy && <p className="hint">Vérification auprès d'Anthropic…</p>}
      <label className="field"><span>Modèle par défaut des nodes Claude</span>
        <select value={a.model || ""} disabled={busy || !a.configured} onChange={(e) => apply(() => api.putAnthropic({ model: e.target.value }))}>
          <option value="">recommandé automatiquement</option>
          {(a.models || []).map((m: string) => <option key={m} value={m}>{m}</option>)}
        </select></label>
      {!(a.models || []).length && <p className="hint">Les modèles proposés sont ceux réellement accessibles avec votre clé : enregistrez-la ou cliquez « Tester la clé ».</p>}
    </section>
  );
}

export function SettingsPanel() {
  const open = useStore((s) => s.settingsOpen);
  const settings = useStore((s) => s.settings);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  if (!open) return null;
  const o = settings?.openai || {};
  const close = () => { setKey(""); setState({ settingsOpen: false }); };
  const apply = async (fn: () => Promise<any>) => {
    setBusy(true);
    try {
      const r = await fn();
      setState((s) => ({ settings: { ...(s.settings || {}), openai: r.openai } }));
      if (r.test) toast(r.test.message, r.test.ok ? "ok" : "error");
      await refreshCaps();
      return r;
    } catch (e) { toast(errText(e), "error"); }
    finally { setBusy(false); }
  };
  const models: string[] = o.models || [];
  const modelSelect = (k: "model_generate" | "model_edit" | "model_text", label: string) => (
    <label className="field"><span>{label}</span>
      <select value={o[k] || ""} disabled={busy || !o.configured} onChange={(e) => apply(() => api.putOpenai({ [k]: e.target.value }))}>
        <option value="">recommandé automatiquement</option>
        {(k === "model_text" ? o.text_models || [] : models).map((m: string) => <option key={m} value={m}>{m}</option>)}
      </select></label>
  );
  return (
    <div className="modal-bg" onClick={close}>
      <div className="modal settings" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } }}>
        <div className="modal-head"><b>⚙ Réglages</b><button onClick={close}>Fermer</button></div>
        <div className="modal-body">
          <section className="settings-section">
            <h3>OpenAI — ChatGPT Images</h3>
            <p className="hint">
              Utilisé uniquement par les nodes dont le moteur est réglé sur <b>OpenAI</b> : « Générer une image », « Éditer une
              image », la pose de « Préparer l'image », « Extraction de props » (analyse, par défaut) et « Prompts aléatoires ».
              Leurs prompts — et leurs images s'il y en a — sont alors envoyés à OpenAI (service externe, facturé sur votre
              compte). Toutes les autres nodes restent entièrement locales.
            </p>
            <div className={`keystate ${o.configured ? "on" : ""}`}>
              {o.configured ? <>Clé enregistrée <code>{o.key_hint}</code> — chiffrée sur ce PC (Windows DPAPI), jamais affichée ni journalisée</>
                : "Aucune clé enregistrée : le moteur OpenAI est indisponible."}
            </div>
            {o.last_test && (
              <p className={o.last_test.ok ? "ok" : "err"}>
                {o.last_test.ok ? "✓ " : "✗ "}{o.last_test.message} <span className="hint">({new Date(o.last_test.at).toLocaleString("fr-FR")})</span>
              </p>
            )}
            <div className="keyrow">
              <input type="password" autoComplete="off" spellCheck={false} placeholder={o.configured ? "Remplacer la clé…" : "Clé d'API OpenAI (sk-…)"}
                value={key} onChange={(e) => setKey(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && key.trim()) apply(() => api.putOpenai({ api_key: key.trim() })).then((r) => r && setKey("")); }} />
              <button className="primary" disabled={!key.trim() || busy} onClick={() => apply(() => api.putOpenai({ api_key: key.trim() })).then((r) => r && setKey(""))}>
                Enregistrer et tester</button>
            </div>
            <div className="section actions" style={{ border: 0 }}>
              {o.configured && <button disabled={busy} onClick={() => apply(() => api.testOpenai())}>Tester la clé</button>}
              {o.configured && <button className="danger" disabled={busy} onClick={() => apply(() => api.deleteOpenaiKey())}>Supprimer la clé</button>}
              <a className="hint" href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">Créer ou gérer vos clés sur platform.openai.com ↗</a>
            </div>
            {busy && <p className="hint">Vérification auprès d'OpenAI…</p>}
            <h4>Valeurs par défaut des nodes OpenAI</h4>
            {!models.length && <p className="hint">Les modèles proposés sont ceux réellement accessibles avec votre clé : enregistrez-la ou cliquez « Tester la clé ».</p>}
            {modelSelect("model_generate", "Modèle pour « Générer une image »")}
            {modelSelect("model_edit", "Modèle pour « Éditer une image »")}
            {modelSelect("model_text", "Modèle texte / vision (« Extraction de props », « Prompts aléatoires »)")}
            <label className="field"><span>Qualité</span>
              <select value={o.quality || "auto"} disabled={busy} onChange={(e) => apply(() => api.putOpenai({ quality: e.target.value }))}>
                {Object.entries(QUALITY_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
              </select></label>
            <label className="field"><span>Modération</span>
              <select value={o.moderation || "auto"} disabled={busy} onChange={(e) => apply(() => api.putOpenai({ moderation: e.target.value }))}>
                <option value="auto">standard (auto)</option><option value="low">moins stricte (low)</option>
              </select></label>
            <p className="hint">Chaque node peut remplacer le modèle et la qualité dans son inspecteur. Coût et limites : selon votre compte OpenAI.</p>
          </section>
          <ClaudeSettings />
          <RemoteAccess />
        </div>
      </div>
    </div>
  );
}
