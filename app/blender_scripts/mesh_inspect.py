"""Inspection d'un maillage généré : statistiques, cartes de matériaux réellement présentes, aperçus multi-vues."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402

import a3d_common as A  # noqa: E402


def main(args):
    A.reset_scene()
    A.stage("import", "Import du maillage généré")
    objs = A.import_any(args["mesh_path"])
    meshes = A.mesh_objects(objs)
    if not meshes:
        raise RuntimeError("Le fichier ne contient aucun maillage")
    out_dir = args["out_dir"]
    A.stage("previews", "Rendu des aperçus (vues synthétiques du modèle généré)")
    previews = A.render_views(meshes, out_dir, args.get("prefix", "mesh"), views=("front", "three_quarter", "left", "back"),
                              res=int(args.get("res", 640)), wireframe=True)
    stats = A.mesh_stats(meshes)
    warnings = []
    if stats["triangles"] > 1_500_000:
        warnings.append("Maillage très dense : préparez-le (budget de triangles) avant rigging/jeu.")
    A.write_result(args, {"mesh": stats, "material_maps": A.material_maps(meshes), "previews": previews,
                          "uv_layers": {o.name: len(o.data.uv_layers) for o in meshes}, "warnings": warnings})


if __name__ == "__main__":
    A.run(main)
