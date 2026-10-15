"""« Animer le modèle 3D » : cinématique SOMA (Kimodo), conversion MediaPipe, étapes et entrées par étape, invalidation ciblée.
Sans GPU ni moteur : la logique seulement.

  <venv-app>\\python.exe app\\backend\\tests\\test_animation.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from atelier3d import motion as M  # noqa: E402
from atelier3d.graph import nodetypes as NT  # noqa: E402
from atelier3d.graph.model import validate  # noqa: E402
from test_graph_logic import CAPS, env, run_all  # noqa: E402


def test_soma_fk_rest_pose_and_axes():
    T = 3
    root = np.tile([0.0, 1.0, 0.0], (T, 1))
    quats = np.tile([0.0, 0.0, 0.0, 1.0], (T, len(M.SOMA30_NAMES), 1))
    pos = M.soma_fk(root, quats)
    c = M.soma_to_canon(pos)
    assert set(M.CANON) <= set(c), set(M.CANON) - set(c)
    assert c["l_shoulder"][0, 0] > 0.1 > -0.1 > c["r_shoulder"][0, 0], "gauche du personnage = +X"
    assert c["l_toe"][0, 2] > c["l_ankle"][0, 2], "orteils vers l'avant (+Z)"
    assert c["head"][0, 1] > c["neck"][0, 1] > c["hips"][0, 1] > c["l_knee"][0, 1] > c["l_ankle"][0, 1], "Y vers le haut"
    # rotation de 90° du bassin autour de Y : la gauche passe vers -Z
    q = quats.copy()
    q[:, 0] = [0.0, np.sin(np.pi / 4), 0.0, np.cos(np.pi / 4)]
    c2 = M.soma_to_canon(M.soma_fk(root, q))
    assert c2["l_hip"][0, 2] < -0.05 and abs(c2["l_hip"][0, 0]) < 0.05, c2["l_hip"][0]


def test_mediapipe_conversion_axes():
    spec = importlib.util.spec_from_file_location("mp_runner", ROOT.parents[0] / "engines" / "motion_runner" / "mediapipe_pose.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    w = np.zeros((2, 33, 3))
    # personne face à la caméra (repère MediaPipe : x vers la droite de l'image, y vers le bas, z s'éloigne)
    for i, (x, y) in {R.L_HIP: (0.1, 0.0), R.R_HIP: (-0.1, 0.0), R.L_SH: (0.18, -0.5), R.R_SH: (-0.18, -0.5), R.L_EAR: (0.07, -0.65),
                      R.R_EAR: (-0.07, -0.65), R.L_KNEE: (0.1, 0.45), R.R_KNEE: (-0.1, 0.45), R.L_ANK: (0.1, 0.85), R.R_ANK: (-0.1, 0.85),
                      R.L_FOOT: (0.1, 0.9), R.R_FOOT: (-0.1, 0.9)}.items():
        w[:, i] = [x, y, 0.0]
    w[:, R.L_FOOT, 2] = w[:, R.R_FOOT, 2] = -0.12  # orteils plus près de la caméra = vers l'avant
    c = R.canon(w)
    assert c["l_hip"][0, 0] > 0 > c["r_hip"][0, 0] and c["head"][0, 1] > c["neck"][0, 1] > c["hips"][0, 1] > c["l_knee"][0, 1]
    assert c["l_toe"][0, 2] > 0, "vers la caméra = +Z (avant d'une personne face caméra)"
    filled = R.fill_gaps([np.ones((33, 3)), None, np.full((33, 3), 3.0)])
    assert np.allclose(filled[1], 2.0)


def _animate_graph(g, source_port="prompt"):
    nodes = [{"id": "imp", "type": "mesh.import", "params": {"upload_id": "upl_rig_a"}},
             {"id": "anim", "type": "mesh.animate", "params": {"duration_s": 3}}]
    edges = [{"id": "e1", "from": ["imp", "mesh"], "to": ["anim", "rigged_mesh"]}]
    if source_port == "prompt":
        nodes.append({"id": "p", "type": "text.prompt", "params": {"text": "a person waves with the right hand"}})
        edges.append({"id": "e2", "from": ["p", "text"], "to": ["anim", "prompt"]})
    else:
        nodes.append({"id": "v", "type": "video.import", "params": {"upload_id": "upl_video"}})
        edges.append({"id": "e2", "from": ["v", "video"], "to": ["anim", "video"]})
    return g.create({"schema_version": "1.1", "name": "animation", "nodes": nodes, "edges": edges}, author="test")


def test_animate_steps_read_only_their_inputs_and_mesh_change_reruns_only_retarget():
    db, g, s, j, pl = env()
    pl.capabilities_fn = lambda: {**CAPS, "motion.generate": {"status": "à tester"}}
    doc = _animate_graph(g)
    plan = pl.plan(doc["id"])
    assert plan["valid"], plan["errors"]
    ex = [e for e in plan["executions"] if e["node_id"] == "anim"]
    assert [e["step"] for e in ex] == ["motion", "retarget"], ex
    run = pl.create_run(doc["id"], author="test", jobs_service=j)
    jobs = {jj["step"]: j.get(jj["job_id"]) for jj in run["jobs"] if jj["node_id"] == "anim"}
    assert set(jobs["motion"]["payload"]["inputs"]) == {"prompt"}, jobs["motion"]["payload"]["inputs"]
    assert set(jobs["retarget"]["payload"]["inputs"]) == {"previous", "rigged_mesh"}, jobs["retarget"]["payload"]["inputs"]
    run_all(db, s, j, run)
    st = s.graph_status(g.get(doc["id"]))
    assert st["anim"]["status"] == "ok", st["anim"]
    # autre modèle : seul le transfert est à refaire, le mouvement (Kimodo) reste valable
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "imp", "params": {"upload_id": "upl_rig_b"}}], author="test")
    slots = {x["step"]: x["status"] for x in s.graph_status(doc)["anim"]["slots"]}
    assert slots["motion"] == "ok" and slots["retarget"] == "stale", slots
    plan = pl.plan(doc["id"])
    assert [e["step"] for e in plan["executions"] if e["node_id"] == "anim"] == ["retarget"], plan["executions"]
    # autre prompt : les deux étapes
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "p", "params": {"text": "a person jumps"}}], author="test")
    plan = pl.plan(doc["id"])
    assert [e["step"] for e in plan["executions"] if e["node_id"] == "anim"] == ["motion", "retarget"]


def test_animate_validation():
    db, g, s, j, pl = env()
    doc = _animate_graph(g, "video")
    assert not [e for e in validate(doc)["errors"] if e.get("node_id") == "anim"]
    doc, _ = g.patch(doc["id"], [{"op": "add_node", "node": {"id": "p", "type": "text.prompt", "params": {"text": "walk"}}},
                                 {"op": "add_edge", "edge": {"id": "e3", "from": ["p", "text"], "to": ["anim", "prompt"]}}], author="test")
    assert any(e["code"] == "ambiguous_source" for e in validate(doc)["errors"])
    doc, _ = g.patch(doc["id"], [{"op": "update_node", "node_id": "anim", "params": {"source": "video"}}], author="test")
    assert not [e for e in validate(doc)["errors"] if e.get("node_id") == "anim"]
    st = NT.get("mesh.animate").steps({"type": "mesh.animate", "params": {}})
    assert st[0].resource == "gpu" and st[1].capability == "blender" and st[1].inputs == ("rigged_mesh",)


def test_installed_engines_are_preferred():
    import tempfile

    from atelier3d.setup_install import detect_animation

    home = pathlib.Path(tempfile.mkdtemp())
    runtime = home / "runtime"
    dist = home / "dist"
    exe = dist / "app" / "engines" / "kimodo_cpp" / "bin" / "kmd-generate.exe"
    files = [exe, home / "models" / "kimodo" / "kimodo-soma-rp-v1.1-f32.gguf", home / "models" / "kimodo" / "Llama-3-Kimodo-Q8_0.gguf",
             home / "models" / "kimodo" / "tokenizer.gguf", runtime / "venv-motion" / "Scripts" / "python.exe",
             home / "models" / "mediapipe" / "pose_landmarker_heavy.task"]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    prev = {"kimodo": {"exe": str(files[1]), "model": str(files[1]), "text_model": str(files[1]), "origin": "existant"}}
    a = detect_animation(home, runtime, prev, dist, ["motion_prompt", "motion_video"])
    assert a["kimodo"]["exe"] == str(exe) and a["kimodo"]["origin"] == "installé avec Atelier 3D", a["kimodo"]
    assert a["mediapipe"]["python"].endswith("venv-motion\Scripts\python.exe") or "venv-motion" in a["mediapipe"]["python"], a["mediapipe"]
    # composants non cochés : la configuration précédente (moteur existant) est conservée
    b = detect_animation(home, runtime, prev, dist, [])
    assert b["kimodo"]["origin"] == "existant", b["kimodo"]


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except Exception as e:  # noqa: BLE001
                import traceback

                fails += 1
                print(f"ÉCHEC {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)
