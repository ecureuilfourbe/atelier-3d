// Réglages → Accès à distance (VPN WireGuard de la Freebox). Modifiable seulement depuis le PC lui-même ; un appareil
// connecté à distance voit seulement son état et peut se déconnecter.
import { useEffect, useState } from "react";
import { api } from "../api";
import { errText } from "../actions";
import { toast, useStore } from "../store";

export function RemoteAccess() {
  const client = useStore((s) => s.settings?.client);
  const [r, setR] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [showCode, setShowCode] = useState(false);
  const [nets, setNets] = useState("");
  const [restart, setRestart] = useState(false);
  const load = async () => {
    try {
      const x = await api.remoteSettings();
      setR(x);
      setNets((x.networks || []).join(", "));
    } catch (e) { toast(errText(e), "error"); }
  };
  useEffect(() => { if (client && !client.remote) load(); }, [client?.remote]);

  if (client?.remote) {
    return (
      <section className="settings-section">
        <h3>Accès à distance</h3>
        <p>Vous êtes connecté à distance depuis <code>{client.ip}</code>. Les actions qui s'exécutent sur l'écran du PC (ouvrir un
          dossier, ouvrir Blender) et ces réglages ne sont disponibles que sur le PC lui-même.</p>
        <form method="post" action="/remote/logout"><button type="submit">Se déconnecter de cet appareil</button></form>
      </section>
    );
  }
  if (!r) return <section className="settings-section"><h3>Accès à distance (VPN WireGuard de la Freebox)</h3><p className="hint">Chargement…</p></section>;
  const d = r.diagnostic || {};
  const apply = async (body: any) => {
    setBusy(true);
    try {
      const x = await api.putRemote(body);
      setRestart(!!x.restart_needed);
      await load();
    } catch (e) { toast(errText(e), "error"); }
    finally { setBusy(false); }
  };
  const route = d.route || {};
  return (
    <section className="settings-section remote">
      <h3>Accès à distance (VPN WireGuard de la Freebox)</h3>
      <p className="hint">Depuis un téléphone ou un portable connecté au VPN WireGuard de votre Freebox. Seules les adresses des
        réseaux autorisés sont acceptées, après saisie du code d'accès ; les autres appareils du réseau local restent refusés.</p>
      <label className="row toggle"><span>Autoriser l'accès depuis le VPN</span>
        <input type="checkbox" className="switch" checked={!!r.enabled} disabled={busy} onChange={(e) => apply({ enabled: e.target.checked })} /></label>
      {(restart || (r.enabled !== !!d.listening_all)) && (
        <p className="warn">Redémarrez Atelier 3D pour appliquer : STOP.bat puis START.bat
          {r.enabled ? " (si Windows demande l'autorisation réseau pour Python : « Autoriser », réseaux privés)" : ""}.</p>
      )}
      <label className="field"><span>Réseaux autorisés</span>
        <div className="keyrow">
          <input value={nets} onChange={(e) => setNets(e.target.value)} spellCheck={false} />
          <button disabled={busy || nets === (r.networks || []).join(", ")}
            onClick={() => apply({ networks: nets.split(/[,\s]+/).filter(Boolean) })}>Enregistrer</button>
        </div></label>
      <p className="hint">Par défaut 192.168.27.64/27 : adresses que le serveur WireGuard de la Freebox attribue aux appareils (vérifiez la
        ligne « Address » du fichier de configuration généré par la Freebox).</p>
      <div className="remote-grid">
        <span>Adresse à ouvrir</span>
        <span>{(d.urls || []).length ? d.urls.map((u: string) => <code key={u}>{u}</code>) : <i>adresse du réseau local introuvable</i>}</span>
        <span>Code d'accès</span>
        <span className="code">
          <code>{showCode ? r.code : "••••-••••-••••"}</code>
          <button className="mini" onClick={() => setShowCode(!showCode)}>{showCode ? "Masquer" : "Afficher"}</button>
          <button className="mini" onClick={() => navigator.clipboard?.writeText(r.code).then(() => toast("Code copié", "ok"))}>Copier</button>
          <button className="mini" disabled={busy} title="Déconnecte tous les appareils déjà connectés"
            onClick={async () => { try { const x = await api.newRemoteCode(); setR({ ...r, code: x.code }); setShowCode(true); toast("Nouveau code : les appareils connectés devront le saisir", "ok"); } catch (e) { toast(errText(e), "error"); } }}>
            Nouveau code</button>
        </span>
        <span>Écoute réseau</span>
        <span className={d.listening_all ? "ok" : "hint"}>{d.listening_all ? `✓ toutes les interfaces, port ${d.port}` : `non (127.0.0.1 seulement)`}</span>
        <span>Pare-feu Windows</span>
        <span className={d.firewall_rule ? "ok" : "err"}>{d.firewall_rule ? "✓ règle « Atelier 3D (VPN WireGuard) » active"
          : "✗ aucune règle : lancez ACCES_VPN.bat (une autorisation administrateur)"}</span>
        <span>Retour vers le VPN</span>
        <span className={route.ok ? "ok" : "err"}>{route.ok ? `✓ par « ${route.interface} »`
          : `✗ les réponses vers ${route.probe} partent par « ${route.interface || "?"} » : lancez ACCES_VPN.bat (route par la Freebox)`
            + (String(route.interface || "").toLowerCase().includes("proton") ? " et excluez 192.168.27.64/27 dans le split tunneling de ProtonVPN" : "")}</span>
      </div>
      <div className="section actions" style={{ border: 0 }}>
        <button disabled={busy} onClick={load}>Revérifier</button>
      </div>
      <p className="hint">Sur la Freebox : activez le serveur VPN WireGuard, ajoutez l'appareil, et réservez l'adresse IP de ce PC (bail DHCP
        statique). Pour tout retirer : désactivez ici puis lancez <code>ACCES_VPN.bat -Remove</code>.</p>
    </section>
  );
}
