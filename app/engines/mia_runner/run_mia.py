"""Exécuteur d'inférence Make-It-Animatable pour Atelier 3D (environnement venv-mia).

Reprend la séquence de l'application de démonstration upstream (app.py : prepare_input → preprocess →
infer → vis, licence MIT) sans Gradio ni Blender, et écrit un fichier .npz :
  verts (N,3)     sommets du maillage d'entrée, dans SON repère d'origine
  bw (N,52)       poids de skinning (post-traités, 4 influences max, normalisés)
  joints_head/joints_tail (52,3)  têtes/queues d'os dans le repère d'origine
  names, parents  noms Mixamo et index parents
  global_transform (4,4)  transformation repère d'origine → repère normalisé MIA (tracée)

Progression : lignes « @@A3D {json} » sur stdout. Les modèles sont chargés un par un puis libérés
pour limiter la VRAM (carte 8 Go). Le modèle de pose (retour en T-pose) n'est pas utilisé :
la pose du maillage livré reste la pose de repos.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import traceback

import numpy as np


def emit(kind: str, **data) -> None:
    print("@@A3D " + json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mia-dir", required=True)
    ap.add_argument("--weights-dir", required=True)
    ap.add_argument("--input", required=True, help="maillage .glb/.obj/.ply (Y vers le haut)")
    ap.add_argument("--output", required=True, help="fichier .npz de sortie")
    ap.add_argument("--no-fingers", action="store_true")
    ap.add_argument("--input-normal", action="store_true", help="utilise aussi le modèle de poids avec normales")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--points", type=int, default=32768)
    args = ap.parse_args()

    t0 = time.time()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.abspath(args.mia_dir))
    import mia_compat

    mia_compat.install()

    import torch
    import torch.nn.functional as F
    import trimesh
    from pytorch3d.transforms import Transform3d

    # Points de contrôle officiels vérifiés par SHA-256 à l'installation : chargement complet autorisé.
    _orig_load = torch.load

    def _trusted_load(*a, **k):
        k.setdefault("weights_only", False)
        return _orig_load(*a, **k)

    torch.load = _trusted_load

    from model import PCAE
    from util.utils import get_normalize_transform, sample_mesh, fix_random

    BONES = mia_compat.BONES_IDX_DICT
    P = mia_compat.MIXAMO_PREFIX
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    if device.type != "cuda":
        emit("warning", message="CUDA indisponible : exécution CPU (lente)")
    fix_random()
    N = args.points
    hands_resample_ratio = 0.5
    hierarchical_ratio = hands_resample_ratio

    def wpath(name: str) -> str:
        return os.path.join(args.weights_dir, name)

    def run_model(factory, ckpt: str, fn):
        model = factory()
        model.load(wpath(ckpt))
        model.to(device).eval()
        try:
            with torch.no_grad():
                return fn(model)
        finally:
            del model
            gc.collect()
            if device.type == "cuda":
                torch.cuda.empty_cache()

    def vram():
        if device.type != "cuda":
            return None
        return round(torch.cuda.max_memory_allocated() / 1024**2)

    # ---------------------------------------------------------------- 1. entrée
    emit("stage", stage="load_input", message="Lecture du maillage")
    mesh = trimesh.load(args.input, force="mesh", process=False)
    verts_np = np.asarray(mesh.vertices, dtype=np.float32)
    faces_np = np.asarray(mesh.faces)
    verts_normal_np = np.asarray(mesh.vertex_normals, dtype=np.float32)
    pts = sample_mesh(mesh, N, get_normals=True).astype(np.float32)
    pts = torch.from_numpy(pts).unsqueeze(0)
    pts, pts_normal = torch.chunk(pts, 2, dim=-1)
    verts = torch.from_numpy(verts_np).unsqueeze(0)
    verts_normal = torch.from_numpy(verts_normal_np).unsqueeze(0)
    emit("info", vertices=int(verts_np.shape[0]), faces=int(faces_np.shape[0]))

    # ---------------------------------------------------------------- 2. articulations grossières + repère hanches
    emit("stage", stage="coarse_joints", message="Localisation grossière des articulations")
    norm = get_normalize_transform(pts, keep_ratio=True, recenter=True)
    pts_n = norm.transform_points(pts)
    verts_n = norm.transform_points(verts)

    coarse = run_model(
        lambda: PCAE(N=N, input_normal=False, deterministic=True, output_dim=mia_compat.JOINTS_NUM,
                     predict_bw=False, predict_joints=True, predict_joints_tail=True),
        "joints_coarse.pth",
        lambda m: m(pts_n.to(device)).joints.cpu(),
    )
    cj, cj_tail = coarse[..., :3], coarse[..., 3:]
    hips = cj[:, BONES[f"{P}Hips"]]
    rul = cj[:, BONES[f"{P}RightUpLeg"]]
    lul = cj[:, BONES[f"{P}LeftUpLeg"]]
    rotate = Transform3d(matrix=mia_compat.get_hips_transform(hips, rul, lul).transpose(-1, -2))
    global_transform = norm.compose(rotate)
    verts_r = rotate.transform_points(verts_n)
    verts_normal_r = F.normalize(rotate.transform_normals(verts_normal), dim=-1)
    mesh_r = trimesh.Trimesh(vertices=verts_r.squeeze(0).numpy(), faces=faces_np, process=False)
    tail_hips = rotate.transform_points(cj_tail).squeeze(0).numpy()
    centers = [tail_hips[BONES[f"{P}LeftHand"]], tail_hips[BONES[f"{P}RightHand"]]]
    pts2 = sample_mesh(mesh_r, N, get_normals=True, attn_ratio=hands_resample_ratio, attn_centers=centers).astype(np.float32)
    pts2 = torch.from_numpy(pts2).unsqueeze(0)
    pts2, pts2_normal = torch.chunk(pts2, 2, dim=-1)

    # ---------------------------------------------------------------- 3. inférence principale
    norm2 = get_normalize_transform(pts2, keep_ratio=True, recenter=False)
    pts2 = norm2.transform_points(pts2)
    verts2 = norm2.transform_points(verts_r)
    global_transform = global_transform.compose(norm2)

    emit("stage", stage="skin_weights", message="Prédiction des poids de skinning")

    def bw_fn(m):
        out = []
        for chunk in torch.split(verts2, 100000, dim=-2):
            out.append(m(pts2.to(device), chunk.to(device)).bw.cpu())
        return torch.cat(out, dim=-2)

    bw = run_model(
        lambda: PCAE(N=N, input_normal=False, deterministic=True, hierarchical_ratio=hierarchical_ratio,
                     output_dim=mia_compat.JOINTS_NUM),
        "bw.pth", bw_fn,
    )
    if args.input_normal:
        emit("stage", stage="skin_weights_normal", message="Affinage des poids avec normales")

        def bwn_fn(m):
            out = []
            vn_chunks = torch.split(verts_normal_r, 100000, dim=-2)
            for chunk, nchunk in zip(torch.split(verts2, 100000, dim=-2), vn_chunks):
                out.append(m(torch.cat([pts2, pts2_normal], dim=-1).to(device), torch.cat([chunk, nchunk], dim=-1).to(device)).bw.cpu())
            return torch.cat(out, dim=-2)

        bw_n = run_model(
            lambda: PCAE(N=N, input_normal=True, input_attention=True, deterministic=True,
                         hierarchical_ratio=hierarchical_ratio, output_dim=mia_compat.JOINTS_NUM),
            "bw_normal.pth", bwn_fn,
        )
        mask = _conflict_mask(torch.argmax(bw, dim=-1), lambda k: True,
                              lambda k: any(x in k for x in ("Spine", "Shoulder", "Arm")), BONES)
        bw_n[mask] = bw[mask]
        bw = bw_n

    emit("stage", stage="joints", message="Prédiction du squelette")
    joints = run_model(
        lambda: PCAE(N=N, input_normal=False, deterministic=True, hierarchical_ratio=hierarchical_ratio,
                     output_dim=mia_compat.JOINTS_NUM, kinematic_tree=mia_compat.KINEMATIC_TREE,
                     predict_bw=False, predict_joints=True, predict_joints_tail=True, joints_attn_causal=True),
        "joints.pth",
        lambda m: m.forward(pts2.to(device)).joints.cpu(),
    )

    # ---------------------------------------------------------------- 4. post-traitement des poids (upstream « vis »)
    emit("stage", stage="postprocess", message="Post-traitement des poids")

    def bone_loc(name, dim_idx, kind="tail"):
        b = joints[:, [BONES[f"{P}{name}"]], :]
        return (b[..., 3:] if kind == "tail" else b[..., :3])[..., dim_idx]

    above_head = verts2[..., 1] >= bone_loc("Head", 1)
    bw = _bw_post_process(bw, BONES, above_head_mask=above_head, no_fingers=args.no_fingers)

    # ---------------------------------------------------------------- 5. retour au repère d'origine
    inv = global_transform.inverse()
    head = inv.transform_points(joints[..., :3]).squeeze(0).numpy()
    tail = inv.transform_points(joints[..., 3:]).squeeze(0).numpy()
    bw_np = bw.squeeze(0).numpy().astype(np.float32)
    names = np.array([n[len(P):] for n in mia_compat.MIXAMO_JOINTS])
    np.savez_compressed(
        args.output,
        verts=verts_np, faces=faces_np, bw=bw_np, joints_head=head.astype(np.float32), joints_tail=tail.astype(np.float32),
        names=names, parents=np.array(mia_compat.PARENT_INDICES, dtype=np.int32),
        global_transform=global_transform.get_matrix().squeeze(0).numpy().T.astype(np.float32),
    )
    unweighted = int((bw_np.sum(-1) < 1e-6).sum())
    emit("result", output=args.output, seconds=round(time.time() - t0, 1), peak_vram_mb=vram(),
         vertices=int(verts_np.shape[0]), unweighted_vertices=unweighted, bones=int(len(names)),
         no_fingers=bool(args.no_fingers), input_normal=bool(args.input_normal))
    return 0


def _conflict_mask(dominant_idx, fn1, fn2, bones_idx_dict):
    import torch

    idx1 = torch.tensor(tuple(v for k, v in bones_idx_dict.items() if fn1(k))).to(dominant_idx)
    idx2 = torch.tensor(tuple(v for k, v in bones_idx_dict.items() if fn2(k))).to(dominant_idx)
    mask2 = torch.isin(torch.arange(len(bones_idx_dict), device=dominant_idx.device), idx2)
    m = torch.isin(dominant_idx, idx1).unsqueeze(-1).expand([*dominant_idx.shape, len(bones_idx_dict)]).clone()
    m[:, :, ~mask2] = False
    return m


def _bw_post_process(bw, bones_idx_dict, above_head_mask=None, no_fingers=False):
    """Copie de `bw_post_process` upstream (MIT), sans les cas oreilles/queue (squelette Mixamo seul)."""
    import torch

    P = "mixamorig:"

    def _edit(mask, bw, target, value=1e5):
        mask = mask.unsqueeze(-1).tile(bw.shape[-1])
        hm = torch.zeros_like(mask)
        hm[..., bones_idx_dict[f"{P}{target}"]] = True
        mask &= hm
        bw[mask] = value
        return bw

    if above_head_mask is not None:
        bw = _edit(above_head_mask, bw, "Head")
    hands = {"Left", "Right"}
    fingers = {"Thumb", "Index", "Middle", "Ring", "Pinky"}
    if no_fingers:
        dom = torch.argmax(bw, dim=-1)
        for hand in hands:
            bw[_conflict_mask(dom, lambda k: hand in k and any(x in k for x in fingers), lambda k: k.endswith(f"{hand}Hand"), bones_idx_dict)] = 1e5
        bw[_conflict_mask(dom, lambda k: True, lambda k: any(x in k for x in fingers), bones_idx_dict)] = 0
    dom = torch.argmax(bw, dim=-1)
    if not no_fingers:
        for hand in hands:
            other = next(iter(hands - {hand}))
            bw[_conflict_mask(dom, lambda k: hand in k, lambda k: other in k, bones_idx_dict)] = 0
            for finger in fingers:
                others = fingers - {finger}
                bw[_conflict_mask(dom, lambda k: hand in k and finger in k, lambda k: hand in k and any(x in k for x in others), bones_idx_dict)] = 0
    def hand_bones(s):
        return tuple(f"{s}Hand{f}{i}" for f in ("Thumb", "Index", "Middle", "Ring", "Pinky") for i in (1, 2, 3))

    # Ensembles d'exclusion repris tels quels d'upstream (les parties oreilles/queue n'ont aucun os ici).
    conflict_sets = (
        {
            ("Neck", "Head", "RabbitEar"),
            ("LeftLeg", "LeftFoot", "LeftToeBase"),
            ("RightLeg", "RightFoot", "RightToeBase"),
            ("LeftArm", "LeftForeArm", "LeftHand") + hand_bones("Left"),
            ("RightArm", "RightForeArm", "RightHand") + hand_bones("Right"),
        },
        {
            ("Hips", "Spine", "Spine1", "Spine2", "Neck", "Head", "LeftShoulder", "RightShoulder", "RightUpLeg", "RightLeg",
             "RightFoot", "RightToeBase", "LeftUpLeg", "LeftLeg", "LeftFoot", "LeftToeBase", "RabbitEar", "FoxTail"),
            ("RightForeArm", "RightHand") + hand_bones("Right") + ("LeftForeArm", "LeftHand") + hand_bones("Left"),
        },
        {("LRabbitEar",), ("RRabbitEar",)},
        {("FoxTail",), ("Neck", "Head", "RabbitEar"), ("RightFoot", "RightToeBase", "LeftFoot", "LeftToeBase")},
    )
    for parts in conflict_sets:
        for part in parts:
            other_parts = parts - {part}
            bw[_conflict_mask(dom, lambda k: any(x in k for x in part), lambda k: any(any(x in k for x in p) for p in other_parts), bones_idx_dict)] = 0
    bw = bw / (bw.sum(dim=-1, keepdim=True) + 1e-10)
    thr = torch.topk(bw, k=4, dim=-1, sorted=True).values[..., -1:]
    bw[bw < thr] = 0
    # Écart volontaire à upstream : renormalisation après le seuil des 4 influences (exigée par glTF).
    return bw / (bw.sum(dim=-1, keepdim=True) + 1e-10)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # erreur structurée pour l'adapter
        msg = str(exc)
        oom = "out of memory" in msg.lower() or exc.__class__.__name__ == "OutOfMemoryError"
        emit("error", error_type=exc.__class__.__name__, message=msg, oom=oom, traceback=traceback.format_exc()[-4000:])
        sys.exit(3 if oom else 2)
