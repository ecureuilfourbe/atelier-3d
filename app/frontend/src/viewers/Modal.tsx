import { useRef, useState } from "react";
import { setState, useStore } from "../store";
import { Mesh3D } from "./Mesh3D";
import { CompareView } from "../graph/CompareView";

function CompareBig({ m }: { m: any }) {
  const [mode, setMode] = useState(m.mode);
  return <CompareView before={m.before} after={m.after} mode={mode} onMode={setMode} big />;
}

function ImageCompare({ urls }: { urls: { url: string; label: string }[] }) {
  const [split, setSplit] = useState(50);
  const [zoom, setZoom] = useState(1);
  if (urls.length === 2) {
    return (
      <div className="cmp">
        <div className="row"><label>Curseur <input type="range" min={0} max={100} value={split} onChange={(e) => setSplit(+e.target.value)} /></label>
          <span className="hint">{urls[0].label} ← → {urls[1].label}</span></div>
        <div className="cmpbox">
          <img src={urls[1].url} />
          <div className="cmptop" style={{ clipPath: `inset(0 ${100 - split}% 0 0)` }}><img src={urls[0].url} /></div>
        </div>
        <div className="sbs">{urls.map((u) => <figure key={u.url}><img src={u.url} /><figcaption>{u.label}</figcaption></figure>)}</div>
      </div>
    );
  }
  return (
    <div className="imgview">
      <div className="row"><label>Zoom <input type="range" min={0.25} max={4} step={0.05} value={zoom} onChange={(e) => setZoom(+e.target.value)} /></label></div>
      <div className="imgscroll">{urls.map((u) => <img key={u.url} src={u.url} style={{ width: `${zoom * 100}%`, background: "repeating-conic-gradient(#3a3f4d 0 25%, #2a2e38 0 50%) 0 0/16px 16px" }} />)}</div>
    </div>
  );
}

function VideoView({ url, frames }: { url: string; frames: { url: string; t: number }[] }) {
  const v = useRef<HTMLVideoElement>(null);
  const [t, setT] = useState(0);
  return (
    <div className="videoview">
      <video ref={v} src={url} controls onTimeUpdate={(e) => setT((e.target as HTMLVideoElement).currentTime)} />
      <div className="hint">t = {t.toFixed(2)} s</div>
      <div className="strip">{frames.map((f) => (
        <figure key={f.url} onClick={() => { if (v.current) v.current.currentTime = f.t; }}><img src={f.url} /><figcaption>{f.t.toFixed(2)} s</figcaption></figure>
      ))}</div>
    </div>
  );
}

export function Modal() {
  const m = useStore((s) => s.modal);
  if (!m) return null;
  const close = () => setState({ modal: null });
  return (
    <div className="modal-bg" onClick={close}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head"><b>{m.title}</b><button onClick={close}>Fermer</button></div>
        <div className="modal-body">
          {m.kind === "mesh" && <Mesh3D url={m.url} compareUrl={m.compareUrl} title={m.baseTitle} compareTitle={m.compareTitle} />}
          {m.kind === "image" && <ImageCompare urls={m.urls} />}
          {m.kind === "video" && <VideoView url={m.url} frames={m.frames} />}
          {m.kind === "json" && <pre className="log">{JSON.stringify(m.data, null, 2)}</pre>}
          {m.kind === "text" && <pre className="log">{m.text}</pre>}
          {m.kind === "compare" && <CompareBig m={m} />}
        </div>
      </div>
    </div>
  );
}
