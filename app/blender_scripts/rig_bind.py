"""Assemblage du rig : armature à partir des articulations prédites, poids de skinning, modificateur Armature.

Modes :
  mia_weights   poids prédits par Make-It-Animatable, transférés par correspondance exacte de position
  auto_weights  poids automatiques Blender (diffusion de chaleur) à partir des articulations — « rig automatique à vérifier »
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
import mathutils  # noqa: E402
import numpy as np  # noqa: E402
from mathutils.kdtree import KDTree  # noqa: E402

import a3d_common as A  # noqa: E402

PREFIX = "mixamorig:"


def build_armature(names, parents, heads, tails):
    arm_data = bpy.data.armatures.new("A3D_Armature")
    arm = bpy.data.objects.new("Armature", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    ebs = []
    for i, n in enumerate(names):
        eb = arm_data.edit_bones.new(PREFIX + n)
        h, t = heads[i], tails[i]
        if (t - h).length < 1e-4:
            t = h + mathutils.Vector((0, 0, 0.01))
        eb.head, eb.tail = h, t
        ebs.append(eb)
    for i, p in enumerate(parents):
        if p >= 0:
            ebs[i].parent = ebs[p]
            ebs[i].use_connect = False
    # roulis cohérent : axe Z local vers l'avant du personnage (-Y Blender)
    for eb in ebs:
        try:
            eb.align_roll(mathutils.Vector((0, -1, 0)))
        except Exception:
            pass
    bpy.ops.object.mode_set(mode="OBJECT")
    arm_data.display_type = "STICK"
    arm.show_in_front = True
    return arm


def assign_mia_weights(meshes, arm, npz, names):
    verts_gltf = npz["verts"]
    bw = npz["bw"]
    pts = np.stack([verts_gltf[:, 0], -verts_gltf[:, 2], verts_gltf[:, 1]], axis=1)  # glTF → Blender
    kd = KDTree(len(pts))
    for i, p in enumerate(pts):
        kd.insert(p, i)
    kd.balance()
    size = float(np.max(pts.max(0) - pts.min(0))) or 1.0
    max_dist = 0.0
    far = 0
    for o in meshes:
        groups = [o.vertex_groups.get(PREFIX + n) or o.vertex_groups.new(name=PREFIX + n) for n in names]
        mw = o.matrix_world
        idx_by_vertex = []
        for v in o.data.vertices:
            co, idx, dist = kd.find(mw @ v.co)
            idx_by_vertex.append(idx)
            max_dist = max(max_dist, dist)
            if dist > 1e-4 * size:
                far += 1
        # écriture groupée par os (beaucoup plus rapide que par sommet)
        idx_arr = np.array(idx_by_vertex, dtype=np.int64)
        w = bw[idx_arr]  # (V, 52)
        for j, g in enumerate(groups):
            col = w[:, j]
            nz = np.nonzero(col > 1e-6)[0]
            if nz.size == 0:
                continue
            # regrouper par valeur de poids arrondie pour limiter les appels
            vals = np.round(col[nz], 4)
            for val in np.unique(vals):
                sel = nz[vals == val]
                g.add(sel.tolist(), float(val), "REPLACE")
    return {"max_match_distance_m": round(max_dist, 6), "unmatched_vertices": far,
            "match_tolerance_m": round(1e-4 * size, 6)}


def _parent_auto(objs, arm):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")


def _apply_modifier(o, name):
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.modifier_apply(modifier=name)


def _weights_via_proxy(o, arm):
    """Diffusion de chaleur sur un double remaillé étanche (voxel), puis transfert des poids vers le maillage d'origine.
    Les maillages générés (surfaces ouvertes, pièces internes) font souvent échouer le bone heat directement ; un double
    trop fin échoue aussi (mesuré : ÷160 échoue, ÷100 ≈ 10 k sommets réussit), d'où des résolutions décroissantes."""
    for div in (100, 80, 60):
        r = _weights_via_proxy_at(o, arm, div)
        if r is not None:
            return {**r, "voxel_div": div}
    return None


def _weights_via_proxy_at(o, arm, voxel_div):
    proxy = o.copy()
    proxy.data = o.data.copy()
    bpy.context.scene.collection.objects.link(proxy)
    proxy.parent = None
    proxy.matrix_world = o.matrix_world.copy()
    proxy.modifiers.clear()
    proxy.vertex_groups.clear()
    voxel = max(proxy.dimensions) / voxel_div
    rm = proxy.modifiers.new("A3D_Remesh", "REMESH")
    rm.mode = "VOXEL"
    rm.voxel_size = voxel
    _apply_modifier(proxy, rm.name)
    _parent_auto([proxy], arm)
    if not any(_group_has_weights(proxy, g) for g in proxy.vertex_groups):
        bpy.data.objects.remove(proxy, do_unlink=True)
        return None
    for g in proxy.vertex_groups:
        if g.name not in o.vertex_groups:
            o.vertex_groups.new(name=g.name)
    dt = o.modifiers.new("A3D_WeightTransfer", "DATA_TRANSFER")
    dt.object = proxy
    dt.use_vert_data = True
    dt.data_types_verts = {"VGROUP_WEIGHTS"}
    dt.vert_mapping = "POLYINTERP_NEAREST"
    dt.layers_vgroup_select_src = "ALL"
    dt.layers_vgroup_select_dst = "NAME"
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.modifier_move_to_index(modifier=dt.name, index=0)
    _apply_modifier(o, dt.name)
    # 4 influences au plus (limite glTF / moteurs de jeu) : le .blend et le .glb gardent ainsi les mêmes poids
    bpy.ops.object.vertex_group_limit_total(group_select_mode="ALL", limit=4)
    bpy.ops.object.vertex_group_normalize_all(lock_active=False)
    n_proxy = len(proxy.data.vertices)
    bpy.data.objects.remove(proxy, do_unlink=True)
    return {"voxel_size_m": round(voxel, 5), "proxy_vertices": n_proxy}


def assign_auto_weights(meshes, arm):
    _parent_auto(meshes, arm)
    empty = [o for o in meshes if not any(len(o.vertex_groups) and _group_has_weights(o, g) for g in o.vertex_groups)]
    if not empty:
        return {"method": "ARMATURE_AUTO (bone heat)"}
    A.emit("warning", message="Diffusion de chaleur impossible sur le maillage d'origine (" + ", ".join(o.name for o in empty)
           + ") : poids calculés sur un double remaillé étanche puis transférés")
    proxies = {}
    for o in empty:
        r = _weights_via_proxy(o, arm)
        if r is None:
            raise RuntimeError(f"Échec des poids automatiques Blender (diffusion de chaleur), même sur un double remaillé : {o.name}")
        proxies[o.name] = r
    still = [o.name for o in meshes if not any(_group_has_weights(o, g) for g in o.vertex_groups)]
    if still:
        raise RuntimeError(f"Échec des poids automatiques Blender (diffusion de chaleur) sur : {', '.join(still)}")
    return {"method": "ARMATURE_AUTO sur double remaillé (voxel) + transfert des poids (Data Transfer)", "proxies": proxies}


def _group_has_weights(o, g):
    gi = g.index
    for v in o.data.vertices:
        for ge in v.groups:
            if ge.group == gi and ge.weight > 0:
                return True
    return False


def weight_report(meshes, names):
    total = unweighted = not_normalized = 0
    max_inf = 0
    for o in meshes:
        valid = {o.vertex_groups[PREFIX + n].index for n in names if o.vertex_groups.get(PREFIX + n)}
        for v in o.data.vertices:
            total += 1
            ws = [g.weight for g in v.groups if g.group in valid and g.weight > 0]
            max_inf = max(max_inf, len(ws))
            s = sum(ws)
            if s <= 1e-6:
                unweighted += 1
            elif abs(s - 1.0) > 0.02:
                not_normalized += 1
    return {"vertices": total, "unweighted_vertices": unweighted, "not_normalized_vertices": not_normalized,
            "max_influences": max_inf}


def bone_proxies(arm):
    """Petites pyramides colorées matérialisant les os pour le rendu (le rendu de fond n'affiche pas les overlays)."""
    mat = bpy.data.materials.new("A3D_BoneProxy")
    mat.diffuse_color = (1.0, 0.25, 0.1, 1.0)
    proxies = []
    for b in arm.data.bones:
        h = arm.matrix_world @ b.head_local
        t = arm.matrix_world @ b.tail_local
        length = (t - h).length
        if length < 1e-5:
            continue
        bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=length * 0.12, radius2=0.0, depth=length, location=(h + t) / 2)
        c = bpy.context.active_object
        c.rotation_euler = (t - h).to_track_quat("Z", "Y").to_euler()
        c.data.materials.append(mat)
        proxies.append(c)
    return proxies


def main(args):
    A.reset_scene()
    A.stage("import", "Import du maillage")
    objs = A.import_any(args["mesh_path"])
    meshes = A.mesh_objects(objs)
    if not meshes:
        raise RuntimeError("Aucun maillage dans le fichier d'entrée")
    A.clear_parents_keep_transform(meshes)
    for o in [x for x in objs if x.type == "EMPTY"]:
        bpy.data.objects.remove(o, do_unlink=True)
    A.apply_transforms(meshes)

    npz = np.load(args["npz_path"])
    names = [str(n) for n in npz["names"]]
    parents = [int(p) for p in npz["parents"]]
    heads = [A.gltf_to_blender(p) for p in npz["joints_head"]]
    tails = [A.gltf_to_blender(p) for p in npz["joints_tail"]]
    overrides = args.get("joint_overrides") or {}
    applied = []
    for k, v in overrides.items():
        key = k.replace(PREFIX, "")
        if key not in names:
            continue
        i = names.index(key)
        old = heads[i].copy()
        heads[i] = mathutils.Vector(v)
        p = parents[i]
        if p >= 0 and (tails[p] - old).length < 0.02 * max((tails[p] - heads[p]).length, 1e-3) + 1e-3:
            tails[p] = heads[i].copy()  # la queue du parent suit la nouvelle tête
        applied.append(key)

    A.stage("armature", "Création de l'armature")
    arm = build_armature(names, parents, heads, tails)

    mode = args.get("mode", "mia_weights")
    A.stage("weights", "Poids de skinning")
    if mode == "auto_weights":
        info = assign_auto_weights(meshes, arm)
        status_label = "rig automatique à vérifier"
    else:
        info = assign_mia_weights(meshes, arm, npz, names)
        for o in meshes:
            o.parent = arm
            o.matrix_parent_inverse = arm.matrix_world.inverted()
            mod = o.modifiers.new("Armature", "ARMATURE")
            mod.object = arm
            mod.use_vertex_groups = True
        status_label = "rig Make-It-Animatable"
        if info["unmatched_vertices"]:
            A.emit("warning", message=f"{info['unmatched_vertices']} sommets sans correspondance exacte avec le maillage analysé par MIA")

    wr = weight_report(meshes, names)
    arm.data.pose_position = "REST"
    arm.data.pose_position = "POSE"
    for pb in arm.pose.bones:
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.location = (0, 0, 0)
        pb.scale = (1, 1, 1)

    out_dir = args["out_dir"]
    A.stage("save", "Enregistrement .blend et .glb riggé")
    blend_path = os.path.join(out_dir, "rigged.blend")
    glb_path = os.path.join(out_dir, "rigged.glb")
    A.export_glb(glb_path, objs=meshes + [arm])
    A.save_blend(blend_path)

    A.stage("previews", "Aperçus avec os")
    previews = A.render_views(meshes, out_dir, "rig", views=("front", "three_quarter"), res=640)
    proxies = bone_proxies(arm)
    s = bpy.context.scene
    A.setup_render(640)
    s.display.shading.show_xray = True
    s.display.shading.xray_alpha = 0.35
    s.display.shading.color_type = "MATERIAL"
    for v in ("front", "left"):
        A.ortho_camera(meshes, v)
        p = os.path.join(out_dir, f"rig_bones_{v}.png")
        s.render.filepath = p
        bpy.ops.render.render(write_still=True)
        previews.append(p)
    for c in proxies:
        bpy.data.objects.remove(c, do_unlink=True)

    bones = [{"name": PREFIX + n, "parent": (PREFIX + names[p]) if p >= 0 else None,
              "head": [round(x, 5) for x in heads[i]], "tail": [round(x, 5) for x in tails[i]]} for i, (n, p) in enumerate(zip(names, parents))]
    A.write_result(args, {
        "mode": mode, "status_label": status_label, "blend": blend_path, "glb": glb_path, "previews": previews,
        "armature": {"name": arm.name, "bones": len(bones), "deform_bones": len(bones)}, "bones": bones,
        "weights": wr, "transfer": info, "joint_overrides_applied": applied,
        "mesh": A.mesh_stats(meshes),
        "transforms": {
            "engine_space": "MIA : recentrage + normalisation [-1,1] + rotation repère hanches (matrice fournie), puis inverse appliquée",
            "mia_global_transform": npz["global_transform"].round(6).tolist() if "global_transform" in npz else None,
            "axis_conversion": "glTF (Y haut, avant +Z) → Blender (Z haut, avant -Y) : (x, y, z) → (x, -z, y)",
            "applied_to": "maillage, têtes et queues d'os dans le même repère ; transformations d'objets appliquées avant liaison",
        },
    })


if __name__ == "__main__":
    A.run(main)
