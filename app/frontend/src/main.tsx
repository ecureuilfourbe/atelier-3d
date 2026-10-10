import { Component, ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import { App } from "./App";

/** Garde-fou : une erreur d'affichage ne doit jamais laisser une page blanche (les données sont sur le serveur). */
class Boundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error) { console.error("Atelier 3D : erreur d'affichage", error); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="crash">
        <h2>L'interface a rencontré une erreur d'affichage</h2>
        <p>Vos graphes, calculs en cours et résultats ne sont pas affectés : ils sont enregistrés par le serveur.</p>
        <pre>{String(this.state.error?.message || this.state.error)}</pre>
        <button className="primary" onClick={() => location.reload()}>Recharger l'interface</button>
      </div>
    );
  }
}

createRoot(document.getElementById("root")!).render(<Boundary><App /></Boundary>);
