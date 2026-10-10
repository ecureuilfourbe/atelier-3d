import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

type Mode = "textured" | "solid" | "wireframe";

function Viewer({ url, title, sync }: { url: string; title?: string; sync?: (c: OrbitControls) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<Mode>("textured");
  const [bones, setBones] = useState(true);
  const [grid, setGrid] = useState(true);
  const [info, setInfo] = useState<string>("Chargement…");
  const [clips, setClips] = useState<string[]>([]);
  const [clip, setClip] = useState<string>("");
  const api = useRef<any>({});

  useEffect(() => {
    const el = host.current!;
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    renderer.setSize(el.clientWidth, el.clientHeight);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1b1f2a);
    const cam = new THREE.PerspectiveCamera(35, el.clientWidth / el.clientHeight, 0.01, 1000);
    const controls = new OrbitControls(cam, renderer.domElement);
    controls.enableDamping = true;
    sync?.(controls);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x404050, 1.6));
    const dl = new THREE.DirectionalLight(0xffffff, 2.2);
    dl.position.set(2, 4, 3);
    scene.add(dl);
    const gridH = new THREE.GridHelper(4, 40, 0x58607a, 0x2c3242);
    scene.add(gridH);
    const mixer = { m: null as THREE.AnimationMixer | null, actions: {} as Record<string, THREE.AnimationAction> };
    const originals = new Map<THREE.Mesh, THREE.Material | THREE.Material[]>();
    let skel: THREE.SkeletonHelper | null = null;
    let root: THREE.Object3D | null = null;
    const clock = new THREE.Clock();
    let raf = 0;
    new GLTFLoader().load(url, (g) => {
      root = g.scene;
      scene.add(root);
      const box = new THREE.Box3().setFromObject(root);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      let tris = 0, meshes = 0;
      root.traverse((o: any) => {
        if (o.isMesh) {
          meshes++;
          originals.set(o, o.material);
          const geo = o.geometry as THREE.BufferGeometry;
          tris += (geo.index ? geo.index.count : geo.attributes.position.count) / 3;
        }
      });
      skel = new THREE.SkeletonHelper(root);
      (skel.material as THREE.LineBasicMaterial).linewidth = 2;
      skel.visible = false;
      scene.add(skel);
      const hasBones = (skel as any).bones?.length > 0;
      gridH.position.y = box.min.y;
      const dist = Math.max(size.x, size.y, size.z) * 2.2;
      cam.position.set(center.x + dist * 0.4, center.y + dist * 0.2, center.z + dist);
      controls.target.copy(center);
      controls.update();
      setInfo(`${meshes} maillage(s) · ${Math.round(tris).toLocaleString("fr-FR")} triangles · ${size.x.toFixed(2)} × ${size.y.toFixed(2)} × ${size.z.toFixed(2)} (unités glTF, m)${hasBones ? ` · ${(skel as any).bones.length} os` : ""}`);
      if (g.animations.length) {
        mixer.m = new THREE.AnimationMixer(root);
        g.animations.forEach((a) => (mixer.actions[a.name] = mixer.m!.clipAction(a)));
        setClips(g.animations.map((a) => a.name));
      }
    }, undefined, (err) => setInfo("Échec du chargement : " + String(err)));
    const loop = () => {
      raf = requestAnimationFrame(loop);
      mixer.m?.update(clock.getDelta());
      controls.update();
      renderer.render(scene, cam);
    };
    loop();
    const ro = new ResizeObserver(() => { renderer.setSize(el.clientWidth, el.clientHeight); cam.aspect = el.clientWidth / el.clientHeight; cam.updateProjectionMatrix(); });
    ro.observe(el);
    api.current = {
      mode: (m: Mode) => {
        originals.forEach((mat, mesh) => {
          if (m === "textured") mesh.material = mat;
          else if (m === "solid") mesh.material = new THREE.MeshStandardMaterial({ color: 0xb8bcc8, roughness: 0.6, skinning: true } as any);
          else mesh.material = new THREE.MeshBasicMaterial({ color: 0x9fb4ff, wireframe: true });
        });
      },
      bones: (b: boolean) => { if (skel) skel.visible = b; },
      grid: (b: boolean) => { gridH.visible = b; },
      clip: (name: string) => {
        Object.values(mixer.actions).forEach((a) => a.stop());
        if (name && mixer.actions[name]) mixer.actions[name].reset().play();
      },
    };
    return () => { cancelAnimationFrame(raf); ro.disconnect(); renderer.dispose(); el.innerHTML = ""; };
  }, [url]);  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { api.current.mode?.(mode); }, [mode]);
  useEffect(() => { api.current.bones?.(bones); }, [bones]);
  useEffect(() => { api.current.grid?.(grid); }, [grid]);
  useEffect(() => { api.current.clip?.(clip); }, [clip]);

  return (
    <div className="viewer3d">
      <div className="vtools">
        {title && <b>{title}</b>}
        {(["textured", "solid", "wireframe"] as Mode[]).map((m) => (
          <button key={m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}>{{ textured: "Texturée", solid: "Solide", wireframe: "Filaire" }[m]}</button>
        ))}
        <label className="check"><input type="checkbox" checked={bones} onChange={(e) => setBones(e.target.checked)} />Squelette</label>
        <label className="check"><input type="checkbox" checked={grid} onChange={(e) => setGrid(e.target.checked)} />Grille</label>
        {clips.length > 0 && (
          <select value={clip} onChange={(e) => setClip(e.target.value)}>
            <option value="">Pose de repos</option>
            {clips.map((c) => <option key={c} value={c}>{c.replace("A3D_Test_", "test : ")}</option>)}
          </select>
        )}
      </div>
      <div className="vcanvas" ref={host} />
      <div className="vinfo">{info}</div>
    </div>
  );
}

export function Mesh3D({ url, compareUrl, title, compareTitle }: { url: string; compareUrl?: string; title?: string; compareTitle?: string }) {
  const ctrls = useRef<OrbitControls[]>([]);
  const sync = (c: OrbitControls) => {
    ctrls.current.push(c);
    c.addEventListener("change", () => {
      for (const o of ctrls.current) if (o !== c) { o.object.position.copy(c.object.position); o.target.copy(c.target); }
    });
  };
  return (
    <div className={`mesh3d ${compareUrl ? "split" : ""}`}>
      <Viewer url={url} title={compareUrl ? title || "Avant" : undefined} sync={compareUrl ? sync : undefined} />
      {compareUrl && <Viewer url={compareUrl} title={compareTitle || "Après"} sync={sync} />}
    </div>
  );
}
