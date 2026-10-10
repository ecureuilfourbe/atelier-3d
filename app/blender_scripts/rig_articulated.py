"""Objet articulé : armature simple à pivots choisis, liaison rigide (poids 1 vers l'os de la pièce).

Pivots (repère Blender, mètres) : [{"name", "head": [x,y,z], "tail": [x,y,z], "parent": nom|None, "objects": [noms]|None}]
Sans pivots fournis : un os racine + un os par pièce séparée du maillage (pivot au centre bas de la pièce).
Les morphologies serpent/quadrupède et rigs mécaniques avancés ne sont pas intégrés.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
import mathutils  # noqa: E402

import a3d_common as A  # noqa: E402

MAX_AUTO_PARTS = 64


def main(args):
    A.reset_scene()
    objs = A.import_any(args["mesh_path"])
    meshes = A.mesh_objects(objs)
    A.clear_parents_keep_transform(meshes)
    for o in [x for x in bpy.data.objects if x.type == "EMPTY"]:
        bpy.data.objects.remove(o, do_unlink=True)
    A.apply_transforms(meshes)
    merged = 0
    if len(meshes) == 1 and args.get("split_loose", True):
        bpy.context.view_layer.objects.active = meshes[0]
        bpy.ops.object.select_all(action="DESELECT")
        meshes[0].select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        # l'import glTF garde les sommets dupliqués aux coutures UV : sans fusion, chaque îlot UV deviendrait une
        # « pièce » (mesuré : 4 166 pièces sur un coffre Pixal3D). Les UV restent par coin de face, donc intactes.
        n0 = len(meshes[0].data.vertices)
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.remove_doubles(threshold=1e-6)
        bpy.ops.object.mode_set(mode="OBJECT")
        merged = n0 - len(meshes[0].data.vertices)
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.separate(type="LOOSE")
        bpy.ops.object.mode_set(mode="OBJECT")
        meshes = A.mesh_objects(bpy.context.scene.objects)
    pivots = args.get("pivots") or []
    mn, mx = A.world_bbox(meshes)
    size = max(mx - mn)
    fragments = len(meshes)
    fragmented = False
    root_c = mathutils.Vector(((mn.x + mx.x) / 2, (mn.y + mx.y) / 2, mn.z))
    if not pivots:
        pivots = [{"name": "root", "head": list(root_c), "tail": list(root_c + mathutils.Vector((0, 0, size * 0.1))), "parent": None, "objects": []}]
        if fragments > MAX_AUTO_PARTS:
            # maillage généré d'un seul tenant visuel mais morcelé (mesuré : 2 005 fragments sur un coffre Pixal3D) :
            # « un os par pièce » n'aurait aucun sens -> un seul os, pivots à fournir par l'utilisateur
            fragmented = True
            A.emit("warning", message=f"Maillage fragmenté ({fragments} morceaux disjoints) : pivots automatiques impossibles, "
                                      "objet lié à un seul os. Indiquez les pivots (mode guidé) pour l'articuler.")
            pivots.append({"name": "body", "head": list(root_c), "tail": list(root_c + mathutils.Vector((0, 0, max(mx.z - mn.z, size * 0.1)))),
                           "parent": "root", "objects": [o.name for o in meshes]})
        else:
            big = sorted(meshes, key=lambda o: -len(o.data.vertices))
            for i, o in enumerate(big[:32]):
                a, b = A.world_bbox([o])
                c = mathutils.Vector(((a.x + b.x) / 2, (a.y + b.y) / 2, a.z))
                pivots.append({"name": f"part_{i:02d}", "head": list(c), "tail": list(c + mathutils.Vector((0, 0, max(b.z - a.z, size * 0.02)))),
                               "parent": "root", "objects": [o.name]})
        auto = True
    else:
        auto = False
    arm = bpy.data.objects.new("Armature", bpy.data.armatures.new("A3D_Armature"))
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    ebs = {}
    for p in pivots:
        eb = arm.data.edit_bones.new(p["name"])
        eb.head, eb.tail = mathutils.Vector(p["head"]), mathutils.Vector(p["tail"])
        ebs[p["name"]] = eb
    for p in pivots:
        if p.get("parent") and p["parent"] in ebs:
            ebs[p["name"]].parent = ebs[p["parent"]]
    bpy.ops.object.mode_set(mode="OBJECT")
    by_owner: dict = {}
    for o in meshes:
        owner = next((p["name"] for p in pivots if o.name in (p.get("objects") or [])), None)
        if owner is None:  # pièce non attribuée : os dont la tête est la plus proche du centre de la pièce
            a, b = A.world_bbox([o])
            c = (a + b) / 2
            owner = min(pivots, key=lambda p: (mathutils.Vector(p["head"]) - c).length)["name"]
        by_owner.setdefault(owner, []).append(o)
    # une pièce = un objet par os (les fragments d'un même os sont réunis : export rapide, scène lisible)
    parts = []
    assignments = {}
    for owner, objs in by_owner.items():
        bpy.ops.object.select_all(action="DESELECT")
        for o in objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = objs[0]
        if len(objs) > 1:
            bpy.ops.object.join()
        o = bpy.context.view_layer.objects.active
        o.name = f"{owner}_mesh"
        g = o.vertex_groups.new(name=owner)
        g.add(list(range(len(o.data.vertices))), 1.0, "REPLACE")
        o.parent = arm
        m = o.modifiers.new("Armature", "ARMATURE")
        m.object = arm
        assignments[o.name] = {"bone": owner, "fragments": len(objs)}
        parts.append(o)
    meshes = parts
    out_dir = args["out_dir"]
    blend = os.path.join(out_dir, "rigged.blend")
    glb = os.path.join(out_dir, "rigged.glb")
    A.export_glb(glb, objs=meshes + [arm])
    A.save_blend(blend)
    previews = A.render_views(meshes, out_dir, "rig", views=("front", "three_quarter"), res=512)
    label = ("armature à un os (maillage fragmenté : pivots à fournir)" if fragmented else "armature à pivots (liaison rigide)")
    A.write_result(args, {"mode": "articulated_rigid", "status_label": label, "auto_pivots": auto, "merged_seam_vertices": merged,
                          "fragments": fragments, "fragmented": fragmented,
                          "blend": blend, "glb": glb, "previews": previews, "pivots": pivots, "assignments": assignments,
                          "armature": {"bones": len(pivots)}, "mesh": A.mesh_stats(meshes),
                          "weights": {"vertices": sum(len(o.data.vertices) for o in meshes), "unweighted_vertices": 0}})


if __name__ == "__main__":
    A.run(main)
