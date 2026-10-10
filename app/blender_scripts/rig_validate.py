"""Tests de déformation : poses procédurales courtes, mesures, images ; rest pose restaurée.

Les animations de contrôle sont rangées dans des actions distinctes (utilisateur factice), non assignées,
sans modifier la rest pose. Les mesures ne garantissent pas l'absence de défauts : le rapport liste les
zones à vérifier visuellement.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
import mathutils  # noqa: E402
import numpy as np  # noqa: E402

import a3d_common as A  # noqa: E402

P = "mixamorig:"

# (nom, [(os, axe local, degrés)]) — amplitudes modestes, adaptées à un contrôle visuel.
HUMANOID_TESTS = [
    ("bras_leves", [("LeftArm", "Z", 45), ("RightArm", "Z", -45)]),
    ("coudes_plies", [("LeftForeArm", "X", 80), ("RightForeArm", "X", 80)]),
    ("epaules", [("LeftShoulder", "Z", 20), ("RightShoulder", "Z", -20)]),
    ("genoux_plies", [("LeftLeg", "X", -70), ("RightLeg", "X", -70)]),
    ("hanches_flexion", [("LeftUpLeg", "X", 45), ("RightUpLeg", "X", -20)]),
    ("torsion_buste", [("Spine1", "Y", 30), ("Spine2", "Y", 15)]),
    ("tete", [("Neck", "X", 20), ("Head", "Y", 30)]),
]


def evaluated_coords(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    eo = obj.evaluated_get(dg)
    me = eo.to_mesh()
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    me.calc_loop_triangles()
    tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tris)
    eo.to_mesh_clear()
    return co.reshape(-1, 3), tris.reshape(-1, 3)


def tri_areas_normals(co, tris):
    a, b, c = co[tris[:, 0]], co[tris[:, 1]], co[tris[:, 2]]
    cr = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cr, axis=1)
    n = cr / (np.linalg.norm(cr, axis=1, keepdims=True) + 1e-12)
    return area, n


def set_pose(arm, spec, amplitude):
    for pb in arm.pose.bones:
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = (0, 0, 0)
    for bone, axis, deg in spec:
        pb = arm.pose.bones.get(P + bone) or arm.pose.bones.get(bone)
        if pb is None:
            continue
        e = [0.0, 0.0, 0.0]
        e["XYZ".index(axis)] = math.radians(deg * amplitude)
        pb.rotation_euler = e
    bpy.context.view_layer.update()


def main(args):
    A.reset_scene()
    bpy.ops.wm.open_mainfile(filepath=args["blend_path"])
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    if arm is None:
        raise RuntimeError("Aucune armature dans le fichier : le maillage n'est pas riggé")
    meshes = [o for o in bpy.data.objects if o.type == "MESH" and any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers)]
    if not meshes:
        raise RuntimeError("Aucun maillage lié à l'armature par un modificateur Armature")
    out_dir = args["out_dir"]
    amplitude = float(args.get("amplitude", 1.0))
    checks = {}

    # -------- contrôles structurels
    A.stage("structure", "Contrôle de la hiérarchie, des groupes et de la rest pose")
    bone_names = {b.name for b in arm.data.bones}
    for o in meshes:
        vg = {g.name for g in o.vertex_groups}
        orphan_groups = sorted(vg - bone_names)
        unweighted = sum(1 for v in o.data.vertices if not any(g.weight > 0 and o.vertex_groups[g.group].name in bone_names for g in v.groups))
        sums = [sum(g.weight for g in v.groups if o.vertex_groups[g.group].name in bone_names) for v in o.data.vertices]
        bad_norm = sum(1 for s in sums if s > 0 and abs(s - 1) > 0.02)
        checks[o.name] = {
            "armature_modifier": True, "parent": o.parent.name if o.parent else None,
            "vertex_groups": len(vg), "groups_without_bone": orphan_groups[:20],
            "unweighted_vertices": unweighted, "not_normalized_vertices": bad_norm,
            "object_transform_identity": o.matrix_world == mathutils.Matrix.Identity(4) or (o.parent is not None),
        }
    rest_ok = arm.data.pose_position in ("POSE", "REST")

    # -------- poses
    A.stage("poses", "Poses de test procédurales")
    arm.data.pose_position = "POSE"
    set_pose(arm, [], 1)
    base = {o.name: evaluated_coords(o) for o in meshes}
    mn, mx = A.world_bbox(meshes)
    height = max(mx - mn)
    results = []
    actions = []
    previews = []
    A.setup_render(480)
    s = bpy.context.scene
    humanoid = arm.pose.bones.get(P + "Hips") is not None
    tests = HUMANOID_TESTS if humanoid else [(f"pivot_{pb.name}", [(pb.name, "X", 30)]) for pb in arm.pose.bones if pb.parent is not None][:16]
    for name, spec in tests:
        present = [b for b, _, _ in spec if arm.pose.bones.get(P + b) or (not humanoid and arm.pose.bones.get(b))]
        if not present:
            continue
        set_pose(arm, spec, amplitude)
        m = {"pose": name, "bones": [(P + b) if humanoid else b for b in present]}
        flips = stretch_max = shrink_min = 0.0
        flipped = 0
        disp_max = 0.0
        for o in meshes:
            co0, tris = base[o.name]
            co1, _ = evaluated_coords(o)
            a0, n0 = tri_areas_normals(co0, tris)
            a1, n1 = tri_areas_normals(co1, tris)
            valid = a0 > 1e-12
            ratio = np.where(valid, a1 / np.maximum(a0, 1e-12), 1.0)
            dot = np.einsum("ij,ij->i", n0, n1)
            flipped += int(np.sum((dot < -0.2) & valid))
            stretch_max = max(stretch_max, float(np.percentile(ratio[valid], 99.5)) if valid.any() else 1.0)
            shrink_min = min(shrink_min or 1e9, float(np.percentile(ratio[valid], 0.5)) if valid.any() else 1.0)
            disp_max = max(disp_max, float(np.max(np.linalg.norm(co1 - co0, axis=1))))
        m.update({"flipped_triangles": flipped, "area_ratio_p99_5": round(stretch_max, 3), "area_ratio_p0_5": round(shrink_min, 3),
                  "max_displacement_rel": round(disp_max / height, 4)})
        warn = []
        if flipped > 0.002 * sum(len(base[o.name][1]) for o in meshes):
            warn.append("triangles retournés (pincement probable)")
        if stretch_max > 3.0:
            warn.append("étirement local fort")
        if shrink_min < 0.2:
            warn.append("écrasement local fort (perte de volume)")
        if disp_max / height < 1e-4:
            warn.append("aucune déformation mesurée : os sans influence ?")
        m["warnings"] = warn
        results.append(m)
        A.ortho_camera(meshes, "front")
        p = os.path.join(out_dir, f"pose_{name}.png")
        s.render.filepath = p
        bpy.ops.render.render(write_still=True)
        previews.append(p)

        # action de contrôle distincte (non assignée)
        act = bpy.data.actions.new(f"A3D_Test_{name}")
        act.use_fake_user = True
        arm.animation_data_create()
        arm.animation_data.action = act
        for frame, amp in ((1, 0.0), (12, amplitude), (24, 0.0)):
            set_pose(arm, spec, amp if amplitude else 0)
            for b in present:
                pb = arm.pose.bones.get(P + b) or arm.pose.bones[b]
                pb.keyframe_insert("rotation_euler", frame=frame, group=pb.name)
        arm.animation_data.action = None
        actions.append(act.name)

    # -------- restauration de la pose neutre
    A.stage("restore", "Restauration de la pose neutre")
    set_pose(arm, [], 1)
    for pb in arm.pose.bones:
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.location = (0, 0, 0)
        pb.scale = (1, 1, 1)
    if arm.animation_data:
        arm.animation_data.action = None
    after = {o.name: evaluated_coords(o)[0] for o in meshes}
    rest_drift = max(float(np.max(np.abs(after[o.name] - base[o.name][0]))) for o in meshes)

    # images pour l'aperçu animé (aller-retour bras + genoux)
    frames = []
    combo = (HUMANOID_TESTS[0][1] + HUMANOID_TESTS[1][1] + HUMANOID_TESTS[3][1]) if humanoid else [s for _, sp in tests for s in sp]
    for k, amp in enumerate([0, 0.33, 0.66, 1.0, 0.66, 0.33]):
        set_pose(arm, combo, amp * amplitude)
        A.ortho_camera(meshes, "three_quarter")
        p = os.path.join(out_dir, f"anim_{k:02d}.png")
        s.render.filepath = p
        bpy.ops.render.render(write_still=True)
        frames.append(p)
    set_pose(arm, [], 1)
    for pb in arm.pose.bones:
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = (1, 0, 0, 0)

    blend_out = os.path.join(out_dir, "rigged_validated.blend")
    A.save_blend(blend_out)
    glb_out = os.path.join(out_dir, "rigged_validated.glb")
    A.export_glb(glb_out, objs=meshes + [arm], with_animations=False)

    total_warn = sum(len(r["warnings"]) for r in results)
    unweighted_total = sum(c["unweighted_vertices"] for c in checks.values())
    verdict = "rig à corriger" if (unweighted_total > 0 or total_warn > 0 or rest_drift > 1e-4 * height) else "déformations contrôlées"
    A.write_result(args, {
        "verdict": verdict, "structure": checks, "rest_pose_ok": rest_ok, "rest_pose_drift_m": round(rest_drift, 6),
        "poses": results, "control_actions": actions, "previews": previews, "animation_frames": frames,
        "blend": blend_out, "glb": glb_out,
        "visual_checks": ["doigts et mains", "aisselles et épaules en élévation", "genoux et plis de l'aine",
                          "cheveux, vêtements longs et accessoires (souvent pondérés au torse ou à la tête)", "visage"],
        "limits": "Mesures indicatives (triangles retournés, étirements) : elles ne garantissent pas l'absence de défauts.",
    })


if __name__ == "__main__":
    A.run(main)
