"""Transfert d'un mouvement (atelier3d-motion/1 : positions d'articulations nommées) sur un modèle riggé Mixamo (rig MIA).

Méthode : pour chaque os mappé, rotation minimale qui aligne l'os (axe Y) sur le segment source correspondant, puis rotation
autour de l'os pour le bassin, le buste, la tête et les mains (référence latérale : hanches, épaules, oreilles/yeux, pouce).
Indépendant des proportions : seule la translation du bassin est mise à l'échelle (rapport des longueurs de jambe).
Calcul direct en espace armature (pas de contraintes) ; la rest pose n'est pas modifiée.

Sorties : .blend animé (action unique), .glb avec l'animation, .fbx, aperçu MP4 rendu (Workbench) + quelques images.
"""
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
import mathutils  # noqa: E402
from mathutils import Matrix, Quaternion, Vector  # noqa: E402

import a3d_common as A  # noqa: E402

P = "mixamorig:"
# os Mixamo → (articulation de départ, articulation d'arrivée, référence latérale (gauche, droite) ou None)
MAP = {
    "Hips": ("hips", "spine", ("l_hip", "r_hip")),
    "Spine": ("spine", "chest", ("l_hip", "r_hip")),
    "Spine1": ("spine", "chest", ("l_shoulder", "r_shoulder")),
    "Spine2": ("chest", "neck", ("l_shoulder", "r_shoulder")),
    "Neck": ("neck", "head", ("l_face", "r_face")),
    "Head": ("head", "head_top", ("l_face", "r_face")),
}
for s, S in (("l", "Left"), ("r", "Right")):
    MAP.update({
        f"{S}Shoulder": (f"{s}_clavicle", f"{s}_shoulder", None),
        f"{S}Arm": (f"{s}_shoulder", f"{s}_elbow", None),
        f"{S}ForeArm": (f"{s}_elbow", f"{s}_wrist", None),
        f"{S}Hand": (f"{s}_wrist", f"{s}_hand_end", None),
        f"{S}UpLeg": (f"{s}_hip", f"{s}_knee", None),
        f"{S}Leg": (f"{s}_knee", f"{s}_ankle", None),
        f"{S}Foot": (f"{s}_ankle", f"{s}_toe", None),
    })
# extrémité anatomique du segment sur le modèle (tête de l'os suivant) : l'axe propre d'un os n'est pas forcément orienté
# le long du segment (bassin, mains) ; candidats dans l'ordre, sinon queue de l'os
SEG_END = {"Hips": ("Spine1", "Spine"), "Spine": ("Spine2", "Spine1"), "Spine1": ("Neck", "Spine2"), "Spine2": ("Neck",),
           "Neck": ("Head",), "Head": ()}
for S in ("Left", "Right"):
    SEG_END.update({f"{S}Shoulder": (f"{S}Arm",), f"{S}Arm": (f"{S}ForeArm",), f"{S}ForeArm": (f"{S}Hand",),
                    f"{S}Hand": (f"{S}HandMiddle1",), f"{S}UpLeg": (f"{S}Leg",), f"{S}Leg": (f"{S}Foot",), f"{S}Foot": (f"{S}ToeBase",)})
# référence latérale au repos sur le modèle (têtes d'os), pour les os à rotation axiale
REST_SIDE = {"Hips": ("LeftUpLeg", "RightUpLeg"), "Spine": ("LeftUpLeg", "RightUpLeg"), "Spine1": ("LeftArm", "RightArm"),
             "Spine2": ("LeftArm", "RightArm"), "Neck": ("LeftArm", "RightArm"), "Head": ("LeftArm", "RightArm")}


def bone(arm, name):
    return arm.data.bones.get(P + name) or arm.data.bones.get(name)


def v3(a):
    return Vector((float(a[0]), float(a[1]), float(a[2])))


def perp(v, axis):
    return v - axis * v.dot(axis)


def signed_angle(a, b, axis):
    a, b = a.normalized(), b.normalized()
    ang = a.angle(b, 0.0)
    return ang if axis.dot(a.cross(b)) >= 0 else -ang


def main(args):
    A.reset_scene()
    path = args["rig_path"]
    if path.lower().endswith(".blend"):
        bpy.ops.wm.open_mainfile(filepath=path)
    else:
        A.import_any(path)
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    if arm is None:
        raise RuntimeError("Aucune armature : le modèle n'est pas riggé")
    if bone(arm, "Hips") is None or bone(arm, "LeftUpLeg") is None:
        raise RuntimeError("Squelette non humanoïde Mixamo (rig MIA attendu) : animation par mouvement humain non prise en charge")
    meshes = [o for o in bpy.data.objects if o.type == "MESH" and any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers)]
    with open(args["motion_path"], encoding="utf-8") as f:
        mo = json.load(f)
    names = mo["joints"]
    J = {n: i for i, n in enumerate(names)}
    frames = mo["positions"]
    T = len(frames)
    fps = float(mo.get("fps", 30))
    src = [[v3(p) for p in fr] for fr in frames]
    S = lambda t, n: src[t][J[n]]  # noqa: E731

    A.stage("analyse", f"Mouvement {mo.get('source')} : {T} images à {fps:g} i/s ; squelette cible {len(arm.data.bones)} os")
    # --- repère cible (espace armature) : gauche, haut, avant d'après la rest pose
    head = lambda n: bone(arm, n).head_local.copy()  # noqa: E731
    left_t = (head("LeftUpLeg") - head("RightUpLeg")).normalized()
    up_t = perp(head("Neck") - head("Hips"), left_t).normalized() if bone(arm, "Neck") else perp(Vector((0, 0, 1)), left_t).normalized()
    fwd_t = left_t.cross(up_t).normalized()
    M = Matrix((left_t, up_t, fwd_t)).transposed()  # colonnes : source x (gauche), y (haut), z (avant) → cible

    # --- échelle : longueurs de jambe
    def leg_t():
        return (head("LeftUpLeg") - head("LeftLeg")).length + (head("LeftLeg") - head("LeftFoot")).length

    leg_s = sorted((S(t, "l_hip") - S(t, "l_knee")).length + (S(t, "l_knee") - S(t, "l_ankle")).length for t in range(T))[T // 2]
    ratio = leg_t() / max(leg_s, 1e-6)
    # sol : point le plus bas des maillages au repos (cible) / des pieds sur tout le clip (source)
    inv_mw = arm.matrix_world.inverted()
    ground_t = min((up_t.dot(inv_mw @ (o.matrix_world @ v.co)) for o in meshes for v in o.data.vertices), default=0.0)
    ground_s = min(S(t, k).y for t in range(T) for k in ("l_toe", "r_toe", "l_ankle", "r_ankle"))
    H0 = head("Hips")
    standing_t = up_t.dot(H0) - ground_t
    ref = Vector((S(0, "hips").x, ground_s + standing_t / ratio, S(0, "hips").z))
    in_place = bool(args.get("in_place")) or not mo.get("root_motion", True)

    # --- ordre parents → enfants, données de repos
    order = []

    def walk(b):
        order.append(b)
        for c in b.children:
            walk(c)

    for b in arm.data.bones:
        if b.parent is None:
            walk(b)
    rest = {b.name: b.matrix_local.copy() for b in arm.data.bones}
    short = {b.name: (b.name[len(P):] if b.name.startswith(P) else b.name) for b in arm.data.bones}
    seg_local = {}  # direction du segment au repos, dans le repère local de l'os
    for b in arm.data.bones:
        sn = short[b.name]
        if sn not in MAP:
            continue
        end = next((bone(arm, e).head_local for e in SEG_END.get(sn, ()) if bone(arm, e)), b.tail_local)
        v = end - b.head_local
        seg_local[b.name] = (rest[b.name].to_3x3().inverted() @ v).normalized() if v.length > 1e-6 else Vector((0, 1, 0))
    side_local = {}
    for bn, (a, c) in REST_SIDE.items():
        b = bone(arm, bn)
        if b and bone(arm, a) and bone(arm, c):
            s = head(a) - head(c)
            side_local[b.name] = (rest[b.name].to_3x3().inverted() @ s)
    # mains : pouce au repos si le rig a des doigts
    for side in ("Left", "Right"):
        b, th, mid = bone(arm, f"{side}Hand"), bone(arm, f"{side}HandThumb1"), bone(arm, f"{side}HandMiddle1")
        if b and th and mid:
            side_local[b.name] = rest[b.name].to_3x3().inverted() @ perp(th.head_local - b.head_local, (mid.head_local - b.head_local).normalized())
            MAP[f"{side}Hand"] = (MAP[f"{side}Hand"][0], MAP[f"{side}Hand"][1], ((f"{side[0].lower()}_thumb"), f"{side[0].lower()}_wrist"))

    # --- action
    if arm.animation_data is None:
        arm.animation_data_create()
    act = bpy.data.actions.new(name=args.get("action_name") or f"A3D_{mo.get('source', 'motion')}")
    arm.animation_data.action = act
    sc = bpy.context.scene
    sc.render.fps = int(round(fps))
    sc.render.fps_base = sc.render.fps / fps if fps else 1.0
    sc.frame_start, sc.frame_end = 1, T
    for pb in arm.pose.bones:
        pb.rotation_mode = "QUATERNION"
    driven = [b.name for b in order if short[b.name] in MAP and all(x in J for x in MAP[short[b.name]][:2])]
    A.stage("transfert", f"Transfert sur {len(driven)} os (échelle {ratio:.2f}, {'sur place' if in_place else 'avec déplacement'})")
    prev_q = {}
    hips_name = bone(arm, "Hips").name
    for t in range(T):
        posed = {}
        for b in order:
            name = b.name
            par = b.parent
            base = (posed[par.name] @ rest[par.name].inverted() @ rest[name]) if par else rest[name].copy()
            base_rot = base.to_3x3().normalized()
            pb = arm.pose.bones[name]
            spec = MAP.get(short[name])
            loc = Vector((0, 0, 0))
            O = base_rot
            if spec and spec[0] in J and spec[1] in J:
                d = (M @ (S(t, spec[1]) - S(t, spec[0])))
                if d.length > 1e-6:
                    d.normalize()
                    y0 = (base_rot @ seg_local.get(name, Vector((0, 1, 0)))).normalized()
                    O = y0.rotation_difference(d).to_matrix() @ base_rot
                    if spec[2] and name in side_local and spec[2][0] in J and spec[2][1] in J:
                        want = perp(M @ (S(t, spec[2][0]) - S(t, spec[2][1])), d)
                        cur = perp(O @ side_local[name], d)
                        if want.length > 1e-6 and cur.length > 1e-6:
                            O = Quaternion(d, signed_angle(cur, want, d)).to_matrix() @ O
            if name == hips_name:
                delta = S(t, "hips") - ref
                if in_place:
                    delta = Vector((0.0, delta.y, 0.0))
                target = H0 + M @ (delta * ratio)
                loc = base_rot.inverted() @ (target - base.to_translation())
            q = (base_rot.inverted() @ O).to_quaternion()
            if name in prev_q and prev_q[name].dot(q) < 0:
                q.negate()
            prev_q[name] = q
            if spec or name == hips_name:
                pb.rotation_quaternion = q
                pb.keyframe_insert("rotation_quaternion", frame=t + 1, group=short[name])
                if name == hips_name:
                    pb.location = loc
                    pb.keyframe_insert("location", frame=t + 1, group=short[name])
            posed[name] = Matrix.Translation(base.to_translation() + base_rot @ loc) @ O.to_4x4()
        if t % 30 == 0:
            A.emit("progress", done=t, total=T)

    # seules les images clés de notre action restent (les poses de contrôle de la validation ne sont pas exportées)
    for a in list(bpy.data.actions):
        if a != act:
            bpy.data.actions.remove(a)
    act.use_fake_user = True

    out = args["out_dir"]
    stem = args.get("stem") or "modele_anime"
    A.stage("export", "Export .blend, .glb et .fbx animés")
    blend = os.path.join(out, f"{stem}.blend")
    glb = os.path.join(out, f"{stem}.glb")
    fbx = os.path.join(out, f"{stem}.fbx")
    sc.frame_set(1)
    A.export_glb(glb, None, with_animations=True)
    fbx_ok = True
    try:
        bpy.ops.export_scene.fbx(filepath=fbx, use_selection=False, add_leaf_bones=False, bake_anim=True, bake_anim_use_all_actions=False,
                                 bake_anim_use_nla_strips=False, path_mode="COPY", embed_textures=True)
    except Exception as e:  # noqa: BLE001
        fbx_ok = False
        A.emit("warning", message=f"export FBX : {e}")

    # --- aperçu : rendu Workbench, caméra 3/4 qui suit le personnage, sol en damier (le déplacement reste visible)
    A.stage("apercu", "Rendu de l'aperçu animé")
    res = int(args.get("preview_res", 540))
    A.setup_render(res, textured=True)
    sc.render.film_transparent = False
    if sc.world is None:
        sc.world = bpy.data.worlds.new("A3D_World")
    sc.world.color = (0.93, 0.93, 0.95)
    hips_pb = arm.pose.bones[hips_name]
    mw = arm.matrix_world
    path = []
    for t in range(1, T + 1):
        sc.frame_set(t)
        path.append(mw @ hips_pb.head)
    sc.frame_set(1)
    mn, mx = A.world_bbox(meshes)
    body_h = max(mx - mn) or 1.7
    ground_z = mn.z
    # caméra lissée (moyenne glissante d'une demi-seconde) pour éviter les secousses du bassin
    win = max(1, int(fps // 2))
    follow = []
    for i in range(T):
        seg = path[max(0, i - win): i + win + 1]
        follow.append(Vector((sum(q.x for q in seg) / len(seg), sum(q.y for q in seg) / len(seg), ground_z + body_h * 0.5)))
    xs, ys = [q.x for q in path], [q.y for q in path]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) + body_h * 3
    bpy.ops.mesh.primitive_plane_add(size=1, location=((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, ground_z))
    plane = bpy.context.active_object
    plane.scale = (span, span, 1)
    img = bpy.data.images.new("A3D_Damier", 2, 2)  # damier 2×2 répété, sans filtrage
    c1, c2 = (0.86, 0.86, 0.88, 1.0), (0.66, 0.67, 0.70, 1.0)
    img.pixels = list(c1 + c2 + c2 + c1)
    mat = bpy.data.materials.new("A3D_Sol")
    mat.use_nodes = True
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.interpolation = "Closest"
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    plane.data.materials.append(mat)
    uv = plane.data.uv_layers.active.data if plane.data.uv_layers.active else None
    if uv:  # une case du damier = 0,5 m
        rep = span / (2 * 0.5)
        for d in uv:
            d.uv = (d.uv[0] * rep, d.uv[1] * rep)
    cam = bpy.data.objects.new("A3D_Camera", bpy.data.cameras.new("A3D_Camera"))
    sc.collection.objects.link(cam)
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = body_h * 1.35
    fw = (mw.to_3x3() @ fwd_t).normalized()
    lw = (mw.to_3x3() @ left_t).normalized()
    d = (fw * 0.8 - lw * 0.6 + Vector((0, 0, 0.35))).normalized()
    cam.data.clip_end = body_h * 40
    sc.camera = cam
    frames_dir = os.path.join(out, "apercu_frames")
    os.makedirs(frames_dir, exist_ok=True)
    sc.render.image_settings.color_mode = "RGB"
    pngs = []
    for t in range(1, T + 1):
        sc.frame_set(t)
        cam.location = follow[t - 1] + d * body_h * 6
        cam.rotation_euler = (follow[t - 1] - cam.location).to_track_quat("-Z", "Y").to_euler()
        p = os.path.join(frames_dir, f"f{t:04d}.png")
        sc.render.filepath = p
        bpy.ops.render.render(write_still=True)
        pngs.append(p)
        if t % 20 == 0:
            A.emit("progress", done=t, total=T)
    video = os.path.join(out, f"{stem}_apercu.mp4")
    r = subprocess.run([args["ffmpeg"], "-v", "error", "-y", "-framerate", f"{fps:g}", "-i", os.path.join(frames_dir, "f%04d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", video],
                       capture_output=True, text=True)
    if r.returncode != 0:
        A.emit("warning", message="encodage MP4 : " + r.stderr[-300:])
        video = None
    keep = [pngs[0], pngs[len(pngs) // 2], pngs[-1]]
    sc.frame_set(1)
    bpy.data.objects.remove(cam)
    bpy.data.objects.remove(plane)
    bpy.data.materials.remove(mat)
    bpy.data.images.remove(img)
    A.save_blend(blend, pack=True)
    A.write_result(args, {"blend": blend, "glb": glb, "fbx": fbx if fbx_ok else None, "video": video, "frames_png": keep,
                          "frames": T, "fps": fps, "duration_s": round(T / fps, 2), "source": mo.get("source"), "scale_ratio": round(ratio, 3),
                          "in_place": in_place, "bones_driven": [short[n] for n in driven],
                          "not_driven": "doigts, orteils, yeux : laissés au repos",
                          "warnings": ["Animation transférée automatiquement : vérifier visuellement les mains, les pieds (glissements) et les "
                                       "interpénétrations ; les rotations axiales des bras et des jambes ne sont pas transférées."]})


A.run(main)
