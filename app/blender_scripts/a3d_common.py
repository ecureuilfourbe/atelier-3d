"""Fonctions communes aux scripts Blender d'Atelier 3D (exécutés en arrière-plan : blender -b --factory-startup).

Protocole : arguments JSON (fichier passé après « -- »), marqueurs « @@A3D {json} » sur stdout,
résultat final écrit dans args["result_path"].
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback

import bpy
import bmesh
import mathutils

T0 = time.time()


def emit(kind: str, **data) -> None:
    print("@@A3D " + json.dumps({"type": kind, **data}, ensure_ascii=False, default=str), flush=True)


def stage(name: str, message: str) -> None:
    emit("stage", stage=name, message=message, t=round(time.time() - T0, 2))


def load_args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    with open(argv[0], "r", encoding="utf-8") as f:
        return json.load(f)


def write_result(args: dict, result: dict) -> None:
    result.setdefault("blender_version", bpy.app.version_string)
    result.setdefault("seconds", round(time.time() - T0, 2))
    with open(args["result_path"], "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    emit("result", path=args["result_path"])


def run(main) -> None:
    try:
        args = load_args()
        main(args)
    except Exception as exc:  # erreur structurée
        emit("error", error_type=exc.__class__.__name__, message=str(exc), traceback=traceback.format_exc()[-4000:])
        sys.exit(2)


# ---------------------------------------------------------------------------- scène

def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    s = bpy.context.scene
    s.unit_settings.system = "METRIC"
    s.unit_settings.scale_length = 1.0
    s.render.fps = 24


def import_any(path: str) -> list:
    before = set(bpy.data.objects)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return list(bpy.data.objects)
    else:
        raise ValueError(f"format non pris en charge : {ext}")
    return [o for o in bpy.data.objects if o not in before]


def mesh_objects(objs=None) -> list:
    objs = objs if objs is not None else bpy.context.scene.objects
    return [o for o in objs if o.type == "MESH"]


def world_bbox(objs) -> tuple:
    mins = mathutils.Vector((math.inf,) * 3)
    maxs = mathutils.Vector((-math.inf,) * 3)
    for o in objs:
        if o.type != "MESH":
            continue
        mw = o.matrix_world
        for v in o.data.vertices:
            w = mw @ v.co
            mins = mathutils.Vector(map(min, mins, w))
            maxs = mathutils.Vector(map(max, maxs, w))
    return mins, maxs


def apply_transforms(objs) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def clear_parents_keep_transform(objs) -> None:
    for o in objs:
        if o.parent is not None:
            mw = o.matrix_world.copy()
            o.parent = None
            o.matrix_world = mw


def mesh_stats(objs) -> dict:
    tris = verts = 0
    for o in mesh_objects(objs):
        me = o.data
        verts += len(me.vertices)
        me.calc_loop_triangles()
        tris += len(me.loop_triangles)
    mn, mx = world_bbox(objs) if objs else (mathutils.Vector(), mathutils.Vector())
    dims = mx - mn
    return {"objects": len(mesh_objects(objs)), "vertices": verts, "triangles": tris,
            "dimensions_m": [round(dims.x, 4), round(dims.y, 4), round(dims.z, 4)],
            "bbox_min": [round(c, 4) for c in mn], "bbox_max": [round(c, 4) for c in mx]}


def images_of(objs) -> list:
    out = []
    for o in mesh_objects(objs):
        for slot in o.material_slots:
            mat = slot.material
            if mat and mat.use_nodes:
                for n in mat.node_tree.nodes:
                    if n.type == "TEX_IMAGE" and n.image and n.image not in out:
                        out.append(n.image)
    return out


def material_maps(objs) -> list:
    """Cartes réellement présentes (connectées au Principled BSDF)."""
    found = []
    for o in mesh_objects(objs):
        for slot in o.material_slots:
            mat = slot.material
            if not (mat and mat.use_nodes):
                continue
            for n in mat.node_tree.nodes:
                if n.type != "BSDF_PRINCIPLED":
                    continue
                for inp in ("Base Color", "Metallic", "Roughness", "Normal", "Alpha"):
                    sock = n.inputs.get(inp)
                    if sock and sock.is_linked:
                        img = _upstream_image(sock)
                        found.append({"material": mat.name, "input": inp, "image": img.name if img else None,
                                      "resolution": list(img.size) if img else None})
    return found


def _upstream_image(sock, depth=0):
    if depth > 6 or not sock.is_linked:
        return None
    node = sock.links[0].from_node
    if node.type == "TEX_IMAGE":
        return node.image
    for i in node.inputs:
        if i.is_linked:
            r = _upstream_image(i, depth + 1)
            if r:
                return r
    return None


# ---------------------------------------------------------------------------- rendu

def setup_render(res=640, engine="BLENDER_WORKBENCH", textured=True):
    s = bpy.context.scene
    s.render.engine = engine
    s.render.resolution_x = res
    s.render.resolution_y = res
    s.render.film_transparent = True
    s.render.image_settings.file_format = "PNG"
    s.render.image_settings.color_mode = "RGBA"
    if engine == "BLENDER_WORKBENCH":
        s.display.shading.light = "STUDIO"
        s.display.shading.color_type = "TEXTURE" if textured else "MATERIAL"
        s.display.shading.show_cavity = True
    return s


def ortho_camera(target_objs, view: str = "front", margin: float = 1.15):
    mn, mx = world_bbox(target_objs)
    center = (mn + mx) / 2
    size = max(mx - mn) or 1.0
    cam = bpy.data.objects.get("A3D_Camera")
    if cam is None:
        cam = bpy.data.objects.new("A3D_Camera", bpy.data.cameras.new("A3D_Camera"))
        bpy.context.scene.collection.objects.link(cam)
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = size * margin
    dist = size * 4
    dirs = {"front": (0, -1, 0), "back": (0, 1, 0), "left": (-1, 0, 0), "right": (1, 0, 0),
            "three_quarter": (0.7, -0.7, 0.25)}
    d = mathutils.Vector(dirs[view]).normalized()
    cam.location = center + d * dist
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam.data.clip_end = dist * 4
    bpy.context.scene.camera = cam
    return cam


def render_views(target_objs, out_dir: str, prefix: str, views=("front", "three_quarter", "left", "back"), res=640,
                 textured=True, wireframe=False) -> list:
    setup_render(res, textured=textured)
    s = bpy.context.scene
    s.display.shading.show_xray = False
    paths = []
    for v in views:
        ortho_camera(target_objs, v)
        p = os.path.join(out_dir, f"{prefix}_{v}.png")
        s.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
    if wireframe:
        for o in mesh_objects(target_objs):
            o.show_wire = True
            o.show_all_edges = True
        s.display.shading.color_type = "SINGLE"
        s.display.shading.single_color = (0.85, 0.85, 0.88)
        ortho_camera(target_objs, "front")
        p = os.path.join(out_dir, f"{prefix}_argile.png")
        s.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
        for o in mesh_objects(target_objs):
            o.show_wire = False
    return paths


def export_glb(path: str, objs=None, with_animations=False) -> None:
    if objs is not None:
        bpy.ops.object.select_all(action="DESELECT")
        for o in objs:
            o.select_set(True)
    kw = dict(filepath=path, export_format="GLB", use_selection=objs is not None, export_yup=True, export_apply=False,
              export_skins=True, export_animations=with_animations, export_materials="EXPORT")
    try:
        bpy.ops.export_scene.gltf(**kw)
    except TypeError:
        kw.pop("export_materials", None)
        bpy.ops.export_scene.gltf(**kw)


def save_blend(path: str, pack=True) -> None:
    if pack:
        try:
            bpy.ops.file.pack_all()
        except RuntimeError as e:
            emit("warning", message=f"pack_all : {e}")
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=True, copy=False)


def gltf_to_blender(p):
    """Repère glTF (Y haut, avant +Z) → Blender (Z haut, avant -Y), conversion de l'importeur officiel."""
    return mathutils.Vector((p[0], -p[2], p[1]))
