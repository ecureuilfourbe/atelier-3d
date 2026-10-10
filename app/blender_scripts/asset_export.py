"""Export final : .blend autonome (textures intégrées), .glb, .fbx, textures séparées.

Mise à l'échelle et centrage appliqués de façon cohérente au maillage ET aux os (rest pose conservée),
puis tracés dans le résultat. La vérification par réouverture est faite par verify_export.py dans un
nouveau processus Blender.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
import mathutils  # noqa: E402

import a3d_common as A  # noqa: E402


def main(args):
    src = args["input_path"]
    out_dir = args["out_dir"]
    name = (args.get("name") or "Asset").strip() or "Asset"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    formats = args.get("formats") or ["blend", "glb", "fbx"]
    A.stage("load", "Chargement")
    if src.lower().endswith(".blend"):
        bpy.ops.wm.open_mainfile(filepath=src)
        objs = list(bpy.context.scene.objects)
    else:
        A.reset_scene()
        objs = A.import_any(src)
        meshes = A.mesh_objects(objs)
        A.clear_parents_keep_transform(meshes)
        for o in [x for x in bpy.data.objects if x.type == "EMPTY"]:
            bpy.data.objects.remove(o, do_unlink=True)
        A.apply_transforms(meshes)
        objs = list(bpy.context.scene.objects)
    for o in [x for x in bpy.data.objects if x.name.startswith("A3D_Camera")]:
        bpy.data.objects.remove(o, do_unlink=True)
    arm = next((o for o in bpy.context.scene.objects if o.type == "ARMATURE"), None)
    meshes = A.mesh_objects(bpy.context.scene.objects)
    if not meshes:
        raise RuntimeError("Aucun maillage à exporter")

    # ---------------------------------------------------------------- échelle et centrage
    A.stage("scale", "Dimensions, centrage et unités")
    mn, mx = A.world_bbox(meshes)
    height = mx.z - mn.z
    target = float(args.get("target_height_m") or 0)
    factor = target / height if (target > 0 and height > 1e-6) else 1.0
    offset = mathutils.Vector((-(mn.x + mx.x) / 2, -(mn.y + mx.y) / 2, -mn.z))
    M = mathutils.Matrix.Scale(factor, 4) @ mathutils.Matrix.Translation(offset)
    for o in meshes:
        o.data.transform(o.matrix_world.inverted() @ M @ o.matrix_world) if o.parent is None else None
    if arm is not None:
        # maillages liés : transformer les données dans le repère monde puis rétablir l'identité
        for o in meshes:
            if o.parent == arm:
                mw = o.matrix_world.copy()
                o.data.transform(mw)
                o.data.transform(M)
                o.matrix_parent_inverse = mathutils.Matrix.Identity(4)
                o.matrix_basis = mathutils.Matrix.Identity(4)
        bpy.context.view_layer.objects.active = arm
        arm.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        amw = arm.matrix_world.copy()
        for eb in arm.data.edit_bones:
            eb.head = M @ (amw @ eb.head)
            eb.tail = M @ (amw @ eb.tail)
        bpy.ops.object.mode_set(mode="OBJECT")
        arm.matrix_world = mathutils.Matrix.Identity(4)
        arm.name = f"{safe}_Armature"
    bpy.context.view_layer.update()
    for i, o in enumerate(meshes):
        o.name = f"{safe}_mesh" if len(meshes) == 1 else f"{safe}_part{i:02d}"
    mn2, mx2 = A.world_bbox(meshes)
    dims = mx2 - mn2
    bpy.context.scene.unit_settings.system = "METRIC"
    bpy.context.scene.unit_settings.scale_length = 1.0

    files = {}
    # ---------------------------------------------------------------- textures séparées
    A.stage("textures", "Textures séparées")
    tex_dir = os.path.join(out_dir, "textures")
    os.makedirs(tex_dir, exist_ok=True)
    textures = []
    for img in A.images_of(meshes):
        if not img.has_data:
            try:
                img.reload()
            except Exception:
                pass
        if img.size[0] == 0:
            continue
        p = os.path.join(tex_dir, "".join(c if c.isalnum() or c in "-_." else "_" for c in img.name) + ".png")
        img_copy = img.copy()
        img_copy.filepath_raw = p
        img_copy.file_format = "PNG"
        try:
            img_copy.save()
            textures.append({"name": img.name, "file": p, "resolution": list(img.size), "colorspace": img.colorspace_settings.name})
        except RuntimeError as e:
            A.emit("warning", message=f"texture {img.name} non enregistrée : {e}")
        bpy.data.images.remove(img_copy)
    files["textures"] = textures

    # ---------------------------------------------------------------- exports
    if "glb" in formats:
        A.stage("glb", "Export .glb")
        p = os.path.join(out_dir, f"{safe}.glb")
        A.export_glb(p, objs=meshes + ([arm] if arm else []), with_animations=False)  # pose de repos, sans animation
        files["glb"] = p
        if arm and bpy.data.actions:
            p2 = os.path.join(out_dir, f"{safe}_poses_test.glb")
            A.export_glb(p2, objs=meshes + [arm], with_animations=True)  # aperçu : animations de contrôle séparées
            files["glb_poses_test"] = p2
    if "fbx" in formats:
        A.stage("fbx", "Export .fbx")
        p = os.path.join(out_dir, f"{safe}.fbx")
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for o in meshes + ([arm] if arm else []):
                o.select_set(True)
            bpy.ops.export_scene.fbx(filepath=p, use_selection=True, add_leaf_bones=False, bake_anim=False,
                                     path_mode="COPY", embed_textures=True, apply_unit_scale=True,
                                     axis_forward="-Z", axis_up="Y", object_types={"ARMATURE", "MESH"})
            files["fbx"] = p
        except Exception as e:  # l'exporteur FBX peut manquer selon la version
            A.emit("warning", message=f"export FBX indisponible : {e}")
            files["fbx_error"] = str(e)
    A.stage("blend", "Enregistrement .blend autonome")
    p = os.path.join(out_dir, f"{safe}.blend")
    A.save_blend(p, pack=True)
    files["blend"] = p
    previews = A.render_views(meshes, out_dir, "export", views=("front", "three_quarter"), res=640)
    A.write_result(args, {
        "files": files, "previews": previews, "name": safe,
        "dimensions_m": [round(dims.x, 4), round(dims.y, 4), round(dims.z, 4)],
        "units": "mètres (système métrique, échelle 1.0)",
        "orientation": "Blender : Z haut, avant -Y ; glTF/FBX : Y haut, avant +Z",
        "transform": {"scale_factor": round(factor, 6), "translation_before_scale_m": [round(c, 5) for c in offset],
                      "origin": "pieds au sol (z=0), centré en x/y", "applied_to": "maillages et os (rest pose)"},
        "armature": {"name": arm.name, "bones": len(arm.data.bones)} if arm else None,
        "actions": [a.name for a in bpy.data.actions],
        "mesh": A.mesh_stats(meshes),
        "material_maps": A.material_maps(meshes),
    })


if __name__ == "__main__":
    A.run(main)
