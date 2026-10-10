"""Préparation du maillage en trois sous-étapes relançables séparément :

  repair    diagnostic (normales, composantes, faces dégénérées, doublons, non-manifold) + réparations limitées
  simplify  décimation (budget de triangles) ou retopologie quads QuadriFlow (Blender) — haute résolution conservée à part
  bake      UV (conservées si valides, sinon dépliage) + baking haute → basse des cartes réellement présentes

Les réparations sont non destructives : la sortie est un nouveau fichier, l'entrée reste inchangée.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bmesh  # noqa: E402
import bpy  # noqa: E402

import a3d_common as A  # noqa: E402


def load(path):
    A.reset_scene()
    objs = A.import_any(path)
    meshes = A.mesh_objects(objs)
    if not meshes:
        raise RuntimeError("Aucun maillage dans le fichier d'entrée")
    A.clear_parents_keep_transform(meshes)
    for o in [x for x in bpy.data.objects if x.type == "EMPTY"]:
        bpy.data.objects.remove(o, do_unlink=True)
    A.apply_transforms(meshes)
    return meshes


def diagnose(meshes):
    out = {}
    for o in meshes:
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bm.edges.ensure_lookup_table()
        nonmanifold = sum(1 for e in bm.edges if not e.is_manifold)
        boundary = sum(1 for e in bm.edges if e.is_boundary)
        loose_v = sum(1 for v in bm.verts if not v.link_edges)
        loose_e = sum(1 for e in bm.edges if not e.link_faces)
        degenerate = sum(1 for f in bm.faces if f.calc_area() < 1e-12)
        # composantes connexes
        seen, comps = set(), 0
        for v in bm.verts:
            if v.index in seen:
                continue
            comps += 1
            stack = [v]
            seen.add(v.index)
            while stack:
                cur = stack.pop()
                for e in cur.link_edges:
                    w = e.other_vert(cur)
                    if w.index not in seen:
                        seen.add(w.index)
                        stack.append(w)
        # cohérence des normales : arêtes manifold dont les deux faces ont un ordre de sommets incompatible
        inconsistent = 0
        for e in bm.edges:
            if len(e.link_faces) == 2:
                f1, f2 = e.link_faces
                if _same_direction(e, f1) == _same_direction(e, f2):
                    inconsistent += 1
        uv_layers = len(o.data.uv_layers)
        out[o.name] = {"vertices": len(bm.verts), "faces": len(bm.faces), "components": comps,
                       "non_manifold_edges": nonmanifold, "boundary_edges": boundary, "loose_vertices": loose_v,
                       "loose_edges": loose_e, "degenerate_faces": degenerate, "inconsistent_normal_edges": inconsistent,
                       "uv_layers": uv_layers, "materials": [m.name for m in o.data.materials if m]}
        bm.free()
    return out


def _same_direction(e, f):
    for loop in f.loops:
        if loop.edge == e:
            return loop.vert == e.verts[0]
    return None


def repair(meshes, dims):
    log = []
    merge_dist = max(dims) * 1e-6
    for o in meshes:
        bm = bmesh.new()
        bm.from_mesh(o.data)
        before = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_dist)
        merged = before - len(bm.verts)
        loose = [v for v in bm.verts if not v.link_edges]
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
        degen = [f for f in bm.faces if f.calc_area() < 1e-14]
        if degen:
            bmesh.ops.dissolve_degenerate(bm, dist=merge_dist, edges=bm.edges)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(o.data)
        bm.free()
        log.append({"object": o.name, "merged_vertices": merged, "removed_loose_vertices": len(loose),
                    "degenerate_faces_dissolved": len(degen), "normals_recalculated": True, "merge_distance_m": merge_dist})
    return log


def simplify(meshes, budget, mode):
    stats = A.mesh_stats(meshes)
    total = max(stats["triangles"], 1)
    log = []
    if mode == "quadriflow":
        for o in meshes:
            target = max(500, int(budget * (A.mesh_stats([o])["triangles"] / total) / 2))
            bpy.context.view_layer.objects.active = o
            bpy.ops.object.select_all(action="DESELECT")
            o.select_set(True)
            try:
                bpy.ops.object.quadriflow_remesh(target_faces=target, use_preserve_sharp=True, use_mesh_symmetry=False)
                log.append({"object": o.name, "method": "QuadriFlow", "target_quads": target})
            except RuntimeError as e:
                A.emit("warning", message=f"QuadriFlow a échoué sur {o.name} ({e}) : décimation utilisée")
                _decimate(o, budget * (A.mesh_stats([o])["triangles"] / total))
                log.append({"object": o.name, "method": "décimation (repli après échec QuadriFlow)"})
        return log
    for o in meshes:
        share = A.mesh_stats([o])["triangles"] / total
        ratio = _decimate(o, budget * share)
        log.append({"object": o.name, "method": "décimation (collapse)", "ratio": round(ratio, 5)})
    return log


def _decimate(o, target_tris):
    tris = max(A.mesh_stats([o])["triangles"], 1)
    ratio = min(1.0, max(target_tris / tris, 0.0005))
    if ratio < 1.0:
        m = o.modifiers.new("A3D_Decimate", "DECIMATE")
        m.decimate_type = "COLLAPSE"
        m.ratio = ratio
        m.use_collapse_triangulate = True
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.modifier_apply(modifier=m.name)
    return ratio


MAPS = [("Base Color", "base_color", "sRGB"), ("Roughness", "roughness", "Non-Color"), ("Metallic", "metallic", "Non-Color")]


def bake(low_objs, high_objs, size, out_dir, keep_existing_uv):
    """Baking Cycles haute → basse : couleur/roughness/metallic via émission, normal map tangente."""
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "CUDA"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = True
        scene.cycles.device = "GPU"
    except Exception:
        scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    scene.render.bake.use_selected_to_active = True
    mn, mx = A.world_bbox(high_objs)
    extent = max(mx - mn)
    scene.render.bake.cage_extrusion = extent * 0.01
    scene.render.bake.max_ray_distance = extent * 0.03

    present = {m["input"] for m in A.material_maps(high_objs)}
    log = {"device": scene.cycles.device, "maps": [], "uv": []}
    results = {}
    for low in low_objs:
        bpy.ops.object.select_all(action="DESELECT")
        bpy.context.view_layer.objects.active = low
        low.select_set(True)
        if not (keep_existing_uv and low.data.uv_layers):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=1.15, island_margin=0.003)
            bpy.ops.object.mode_set(mode="OBJECT")
            log["uv"].append({"object": low.name, "method": "Smart UV Project"})
        else:
            log["uv"].append({"object": low.name, "method": "UV existantes conservées"})
        mat = bpy.data.materials.new(f"{low.name}_A3D")
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = nt.nodes["Principled BSDF"]
        low.data.materials.clear()
        low.data.materials.append(mat)
        images = {}
        for inp, key, cs in MAPS + [("Normal", "normal", "Non-Color")]:
            if inp != "Normal" and inp not in present:
                continue
            img = bpy.data.images.new(f"{low.name}_{key}", size, size, alpha=False, float_buffer=False)
            img.colorspace_settings.name = cs
            tex = nt.nodes.new("ShaderNodeTexImage")
            tex.image = img
            images[key] = (img, tex)
        for inp, key, cs in MAPS:
            if key not in images:
                continue
            img, tex = images[key]
            _set_emission_from(high_objs, inp)
            nt.nodes.active = tex
            _select_for_bake(low, high_objs)
            bpy.ops.object.bake(type="EMIT", margin=8)
            _restore_emission(high_objs)
            p = os.path.join(out_dir, f"{low.name}_{key}.png")
            img.filepath_raw = p
            img.file_format = "PNG"
            img.save()
            if key == "base_color":
                nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            else:
                nt.links.new(tex.outputs["Color"], bsdf.inputs[inp])
            log["maps"].append({"object": low.name, "map": key, "resolution": [size, size], "file": p, "method": "baking émission haute → basse"})
        img, tex = images["normal"]
        nt.nodes.active = tex
        _select_for_bake(low, high_objs)
        bpy.ops.object.bake(type="NORMAL", normal_space="TANGENT", margin=8)
        p = os.path.join(out_dir, f"{low.name}_normal.png")
        img.filepath_raw = p
        img.file_format = "PNG"
        img.save()
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(tex.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        # Workbench « texture » affiche le nœud image ACTIF : sans ceci, l'aperçu montrerait la carte normale (teinte bleue)
        if "base_color" in images:
            nt.nodes.active = images["base_color"][1]
        log["maps"].append({"object": low.name, "map": "normal", "resolution": [size, size], "file": p, "method": "baking normales haute → basse (tangentes)"})
        if "Roughness" not in present:
            log["maps"].append({"object": low.name, "map": "roughness", "method": "valeur constante du BSDF (aucune carte source)"})
        results[low.name] = mat.name
    return log


_SAVED = {}


def _set_emission_from(high_objs, inp):
    """Branche temporairement l'entrée `inp` du Principled BSDF sur la sortie du matériau (émission)."""
    for o in high_objs:
        for slot in o.material_slots:
            mat = slot.material
            if not (mat and mat.use_nodes):
                continue
            nt = mat.node_tree
            out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
            bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if not (out and bsdf):
                continue
            prev = out.inputs["Surface"].links[0].from_socket if out.inputs["Surface"].is_linked else None
            _SAVED[mat.name] = prev
            em = nt.nodes.new("ShaderNodeEmission")
            em.name = "A3D_TMP_EMIT"
            src = bsdf.inputs[inp]
            if src.is_linked:
                nt.links.new(src.links[0].from_socket, em.inputs["Color"])
            else:
                v = src.default_value
                em.inputs["Color"].default_value = (v[0], v[1], v[2], 1) if hasattr(v, "__len__") else (v, v, v, 1)
            nt.links.new(em.outputs["Emission"], out.inputs["Surface"])


def _restore_emission(high_objs):
    for o in high_objs:
        for slot in o.material_slots:
            mat = slot.material
            if not (mat and mat.use_nodes) or mat.name not in _SAVED:
                continue
            nt = mat.node_tree
            out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
            em = nt.nodes.get("A3D_TMP_EMIT")
            if em:
                nt.nodes.remove(em)
            prev = _SAVED.pop(mat.name)
            if prev and out:
                nt.links.new(prev, out.inputs["Surface"])


def _select_for_bake(low, high_objs):
    bpy.ops.object.select_all(action="DESELECT")
    for h in high_objs:
        h.hide_render = False
        h.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low


def main(args):
    stage = args["stage"]
    out_dir = args["out_dir"]
    meshes = load(args["mesh_path"])
    result = {"stage": stage, "input": args["mesh_path"], "before": A.mesh_stats(meshes)}
    dims = result["before"]["dimensions_m"]

    if stage == "repair":
        A.stage("diagnose", "Diagnostic du maillage")
        result["diagnosis"] = diagnose(meshes)
        if args.get("repair", True):
            A.stage("repair", "Réparations limitées")
            result["repairs"] = repair(meshes, dims)
            result["diagnosis_after"] = diagnose(meshes)
        if not args.get("keep_parts_separate", True) and len(meshes) > 1:
            bpy.ops.object.select_all(action="DESELECT")
            for o in meshes:
                o.select_set(True)
            bpy.context.view_layer.objects.active = meshes[0]
            bpy.ops.object.join()
            meshes = [bpy.context.view_layer.objects.active]
            result["joined"] = True
        warnings = []
        for name, d in result.get("diagnosis_after", result["diagnosis"]).items():
            if d["non_manifold_edges"]:
                warnings.append(f"{name} : {d['non_manifold_edges']} arêtes non-manifold (conservées)")
            if d["components"] > 50:
                warnings.append(f"{name} : {d['components']} composantes séparées (petits fragments possibles)")
        result["warnings"] = warnings
    elif stage == "simplify":
        A.stage("simplify", "Simplification")
        result["simplify"] = simplify(meshes, int(args.get("triangle_budget", 40000)), args.get("simplify_mode", "decimate"))
        result["note"] = ("Retopologie QuadriFlow : quads réguliers, mais un résultat en quads ne garantit pas un bon edge flow aux articulations."
                          if args.get("simplify_mode") == "quadriflow" else
                          "Décimation : réduit les triangles sans créer de boucles d'arêtes adaptées à l'animation.")
    elif stage == "bake":
        A.stage("bake", "UV et baking haute → basse")
        high = A.import_any(args["high_path"])
        high_meshes = A.mesh_objects(high)
        A.clear_parents_keep_transform(high_meshes)
        A.apply_transforms(high_meshes)
        for h in high_meshes:
            h.name = "HIGH_" + h.name
        keep_uv = args.get("simplify_mode", "decimate") == "decimate" and args.get("keep_uv", False)
        result["bake"] = bake(meshes, high_meshes, int(args.get("texture_size", 2048)), out_dir, keep_uv)
        for h in high_meshes:
            bpy.data.objects.remove(h, do_unlink=True)
        result["material_maps"] = A.material_maps(meshes)
    else:
        raise ValueError(f"sous-étape inconnue : {stage}")

    A.stage("export", "Écriture du résultat")
    out_glb = os.path.join(out_dir, f"mesh_{stage}.glb")
    A.export_glb(out_glb, objs=meshes)
    result["after"] = A.mesh_stats(meshes)
    result["glb"] = out_glb
    result["previews"] = A.render_views(meshes, out_dir, f"prep_{stage}", views=("front", "three_quarter"), res=512, wireframe=True)
    A.write_result(args, result)


if __name__ == "__main__":
    A.run(main)
