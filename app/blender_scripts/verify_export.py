"""Vérification d'export dans un NOUVEAU processus Blender : réouverture du .blend, réimport du .glb et du .fbx."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402

import a3d_common as A  # noqa: E402


def inspect_scene(expect_rig: bool) -> dict:
    arm = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    # formes d'affichage des os (l'importeur glTF crée un objet « Icosphere ») : ce ne sont pas des maillages de l'asset
    shapes = {pb.custom_shape for a in arm for pb in a.pose.bones if pb.custom_shape is not None}
    meshes = [o for o in A.mesh_objects(bpy.context.scene.objects) if o not in shapes]
    skinned = [o for o in meshes if any(m.type == "ARMATURE" and m.object is not None for m in o.modifiers)]
    vg = {o.name: len(o.vertex_groups) for o in meshes}
    unweighted = 0
    for o in skinned:
        for v in o.data.vertices:
            if not any(g.weight > 0 for g in v.groups):
                unweighted += 1
    imgs = A.images_of(meshes)
    img_ok = [{"name": i.name, "packed": i.packed_file is not None, "has_data": bool(i.has_data or i.size[0] > 0),
               "resolution": list(i.size)} for i in imgs]
    mn, mx = A.world_bbox(meshes)
    dims = mx - mn
    checks = {
        "meshes": len(meshes), "armatures": len(arm), "bones": sum(len(a.data.bones) for a in arm),
        "skinned_meshes": len(skinned), "vertex_groups": vg, "unweighted_vertices": unweighted,
        "images": img_ok, "dimensions_m": [round(dims.x, 4), round(dims.y, 4), round(dims.z, 4)],
    }
    ok = len(meshes) > 0 and all(i["has_data"] for i in img_ok)
    if expect_rig:
        ok = ok and len(arm) == 1 and len(skinned) == len(meshes) and unweighted == 0
    checks["ok"] = bool(ok)
    return checks


def main(args):
    expect_rig = bool(args.get("expect_rig"))
    out = {"expect_rig": expect_rig}
    files = args["files"]
    if files.get("blend"):
        A.stage("reopen_blend", "Réouverture du .blend")
        bpy.ops.wm.open_mainfile(filepath=files["blend"])
        out["blend"] = inspect_scene(expect_rig)
        out["blend"]["all_images_packed"] = all(i["packed"] for i in out["blend"]["images"])
        out["blend"]["actions"] = [a.name for a in bpy.data.actions]
    for key, imp in (("glb", "glb"), ("fbx", "fbx")):
        if files.get(key):
            A.stage(f"reimport_{key}", f"Réimport du .{key}")
            A.reset_scene()
            try:
                A.import_any(files[key])
                out[key] = inspect_scene(expect_rig)
            except Exception as e:
                out[key] = {"ok": False, "error": str(e)}
    exp = args.get("expected_dimensions_m")
    if exp:
        for key in ("blend", "glb", "fbx"):
            if key in out and "dimensions_m" in out[key]:
                d = out[key]["dimensions_m"]
                out[key]["dimensions_match"] = all(abs(a - b) <= max(0.01 * abs(b), 0.002) for a, b in zip(d, exp))
                out[key]["ok"] = out[key]["ok"] and out[key]["dimensions_match"]
    out["ok"] = all(out[k]["ok"] for k in ("blend", "glb", "fbx") if k in out)
    A.write_result(args, out)


if __name__ == "__main__":
    A.run(main)
