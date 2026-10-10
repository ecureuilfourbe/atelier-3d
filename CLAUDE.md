# CLAUDE.md — piloter Atelier 3D depuis Claude Code

Atelier 3D est un studio local (Windows) : graphe de nodes métier → moteurs réels (ComfyUI dédié : Krea 2 Turbo, Krea 2
Identity Edit, BiRefNet, Pixal3D ; Make-It-Animatable ; Blender ; vidéo MiniMax H3 — workflows MiniMax H3 Ultra exécutés par
le ComfyUI dédié si la vidéo a été installée avec Atelier 3D (`video.provider` = `comfyui_dedicated`), sinon par le ComfyUI
MiniMax existant de l'utilisateur (`comfyui_attached`) ; en option, OpenAI
Images — service externe — si l'utilisateur a enregistré sa clé). Claude Code est un client **facultatif** : il utilise exactement la même API et le même historique que l'interface (origine « Claude Code »).

## Accès

- **MCP (recommandé)** : serveur stdio `mcp/atelier3d_mcp.py` (config prête : `<installation>\config\claude_code.mcp.json`).
- **API HTTP** : `http://127.0.0.1:8765/api/v1`, en-tête `Authorization: Bearer <installation>\config\api_token`,
  en-tête `X-Atelier3D-Author: Claude Code`. OpenAPI : `/docs`.
- **Client Python** : `clients/python/atelier3d_client` (`Atelier3D(author="Claude Code")`), CLI `python -m atelier3d_client`.
- **À distance** (si l'utilisateur a activé l'accès par le VPN WireGuard de sa Freebox) : `http://<IP du PC>:<port>/api/v1` depuis
  une adresse du VPN (192.168.27.64/27 par défaut), avec le même jeton Bearer. Les réglages d'accès, l'ouverture d'un dossier
  ou de Blender et l'import par chemin disque y sont refusés (403 `host_only`) : ils restent réservés au PC.

Ne jamais cliquer dans ComfyUI, ne jamais envoyer un graph.json à `/prompt`, ne jamais réécrire un workflow ComfyUI :
l'adapter clone le `workflow.api.json` validé et ne modifie que les entrées mappées dans `bindings.json`.

## Trois JSON distincts

1. **graph.json** (contrat du studio, schéma `app/schemas/graph-1.1.schema.json`) : nodes métier, positions, noodles typées,
   paramètres, révision, items foreach, previews automatiques, liens épinglés (`link: {mode: "pinned", variant_id}`).
2. **workflow.api.json** (`app/workflows/comfy/<id>/`) : graphe exécutable ComfyUI (classes réelles, `[node_id, index]`),
   issu de l'export API officiel du frontend ComfyUI. Jamais modifié en place.
3. **run.json** (`GET /api/v1/runs/{run_id}/run.json`) : snapshot exécuté — graphe figé, révision, seeds effectives,
   `prompt_id`, workflow et empreinte, événements, artefacts avec SHA-256.

## Catégories de nodes (`list_node_types` pour les schémas exacts)

| Type | Rôle | Ports |
|---|---|---|
| `text.prompt` | texte réutilisable | → `text` (Prompt) |
| `text.compose` | fragments → UN prompt affiché | `fragments` (PromptList ×n) → `text` |
| `text.randomize` | « Prompts aléatoires » : `context` + `count` (1-16) → N prompts écrits en local par Qwen3-VL 4B (`language` en/fr, `subject`, `extra`, `creativity`, seed) ; branchée sur `prompts` d'un générateur foreach = un item par prompt (clés `<clé>_1…N`) | `context` (Prompt, facultatif) → `prompts` (PromptList) et `prompt:<n>` |
| `image.import` / `mesh.import` | fichier importé (`upload_id`) | → `image` / `mesh` |
| `image.generate` | « Générer une image » : texte seul. `engine` : `krea2` (Krea 2 Turbo, local, défaut) ou `openai` ; `postprocess.remove_background` ajoute une sous-étape de détourage BiRefNet | `prompt` ou `prompts` (×n, foreach) → `image` ou `images` + `image:<item_key>` |
| `image.edit` | « Éditer une image » : image + consigne. `engine` : `krea2` (Krea 2 Identity Edit, local) ou `openai` ; `image_b` (seconde référence) : OpenAI seulement | `image` (requis), `image_b`, `prompt` ou `prompts` (×n, foreach) → idem |
| `image.prepare` | `pose` (`none` \| `a_pose` \| `t_pose`, édition Krea 2 ou `engine: openai`, `pose_details`), `remove_background` (défaut vrai), cadrage + avertissements | `image` → `image`, `mask` |
| `image.props` | « Extraction de props » : analyse de l'image (`engine` `openai` = ChatGPT vision, défaut, ou `local` = Qwen3-VL 4B) → `character` + `parts` [{key, name, kind body\|prop, symmetric, description, enabled, manual}] écrites dans les réglages (`include` `props`\|`props_body`, `detail` `minimal`\|`standard`\|`detailed`, `merge_symmetric`) ; crée UNE fois un groupe « Props » (node `image.edit` foreach `props_<id>` + previews, une image isolée par partie cochée, clé d'item = clé de la partie) | `image` → `prompts` (PromptList), `image` (transmise) |
| `image.upscale` | `resolution` `2k` \| `4k` (côté le plus long) ; `engine` `seedvr2` (détails, défaut) \| `esrgan` (rapide) ; alpha conservé | `image` → `image` |
| `compare.before_after` | « Avant / après » : affichage seul (aucune étape) ; `mode` `slider` \| `side_by_side` \| `difference`, `label_before`, `label_after` ; l'état expose `refs.before` / `refs.after` | `before`, `after` (type `Media` : Image, Video, Mesh ou RiggedMesh) |
| `video.import` | vidéo importée (`upload_id`, MP4/WebM/MOV ; depuis la médiathèque : `POST /api/v1/uploads/from-artifact` accepte aussi les vidéos) | → `video` |
| `image.preview` / `mesh.preview` / `video.preview` | affichage, sélection ; sortie = variante sélectionnée, aucun calcul | |
| `mesh.generate` | Pixal3D (ou TRELLIS.2) → GLB texturé (couleur, metallic, roughness, normal, AO) | `image` → `mesh` |
| `mesh.prepare` | macro : `repair`, `simplify` (décimation ou QuadriFlow), `bake` (UV + baking Cycles) | `mesh` → `mesh`, `report` |
| `mesh.rig` | MIA (squelette Mixamo 52 os + poids) puis assemblage Blender ; `engine: blender_auto` = poids automatiques Blender (« rig automatique à vérifier ») ; `profile: articulated` = armature à pivots | `mesh` → `rigged_mesh` |
| `mesh.validate` | poses de test, mesures, images, GIF ; actions de contrôle séparées ; rest pose restaurée | `rigged_mesh` → `report`, `rigged_mesh` |
| `asset.export` | `.blend` autonome, `.glb`, `.fbx`, textures, réouverture/réimport vérifiés | `asset` → `files`, `report` |
| `reference.collection` | slots nommés (rôle, sujet, description) | `image:<slot>` → `references` |
| `video.generate` | MiniMax H3 (workflows MiniMax H3 Ultra) : `text_to_video`, `image_to_video`, `first_last_to_video`, `reference_to_video` selon les modes installés (`video.minimax.modes`, raison dans `mode_reasons`) | `prompt`, `image`, `last_image`, `references` → `video` |

## Moteur OpenAI (service externe, facultatif)

- `engine: "openai"` sur `image.generate` / `image.edit` envoie le prompt — et, pour l'édition, les images — à l'API OpenAI
  Images (facturé sur le compte de l'utilisateur). **Ne l'utiliser que si l'utilisateur le demande explicitement** ; par
  défaut, rester sur `krea2` (local).
- La clé est saisie **par l'utilisateur** dans ⚙ Réglages (chiffrée Windows DPAPI dans `config\secrets.dat`). Aucune API ne
  la renvoie (seulement `configured` et 4 derniers caractères via `GET /api/v1/settings`) : ne jamais la demander, la lire
  ou l'écrire soi-même.
- Sans clé, `get_capabilities` donne `image.openai: indisponible` et `plan_graph` refuse (`engine_unavailable`).
- Paramètres de node : `openai_model` (vide = modèle par défaut des Réglages, choisi parmi les modèles `gpt-image-*` réellement
  accessibles), `quality`, `moderation` (vides = Réglages), `background` (`transparent` = PNG avec alpha, si le modèle l'accepte ; sinon erreur OpenAI explicite), `width`/`height`
  (taille personnalisée pour GPT Image 2+, sinon taille fixe la plus proche). Pas de seed côté OpenAI (la seed n'est qu'un
  identifiant de variante).
- Erreurs typées : `openai_key_missing`, `openai_auth` (clé refusée), `openai_forbidden` (vérification d'organisation),
  `openai_moderation` (consigne refusée : reformuler), `openai_quota`, `openai_rate_limit` (une nouvelle tentative
  automatique). Ces étapes passent par la file `net` : elles ne bloquent pas la file GPU.
- Anciens JSON : `image.generate` avec `operation: "edit"` est converti en `image.edit` à la lecture (résultats conservés).
- Autres nodes avec moteur OpenAI : pose de `image.prepare` (`engine`), `text.randomize` (`engine: "openai"`, modèle texte),
  `image.props` (analyse vision, **OpenAI par défaut** à la demande de l'utilisateur ; `engine: "local"` pour rester sur le PC).
  Modèle texte / vision : `openai_model` de la node, sinon `model_text` des Réglages (API Responses, sortie JSON stricte).

## Extraction de props

1. Créer `image.import` → `image.props` : le groupe « Props » (node `props_<id>`, moteur d'extraction `openai` ou `krea2`
   selon le moteur d'analyse) est créé automatiquement.
2. `run_graph(scope="until", node_id="props_<id>")` : si l'analyse manque ou n'est plus à jour, le run ne contient QUE
   l'analyse (`continue_after_analysis: true`) ; à sa réussite le worker écrit `character` / `parts` (en gardant les choix déjà
   faits : coché, nom ; parties `manual` conservées), émet `props_analyzed`, puis lance l'extraction (`run_continued`).
3. Ajuster la liste par `update_graph` (`params.parts` : `enabled`, `name`, suppression, ajout `manual: true`) : seuls les items
   concernés changent (un renommage ne rend « à recalculer » que cette partie). Relancer une partie : `rerun_item` sur
   `props_<id>` avec l'`item_id` dont `item_key` = clé de la partie.

## Procédure standard

1. `get_capabilities` : vérifier `status` (`disponible` = testé ici, `à tester`, `indisponible` + `reason`) pour chaque
   capacité utilisée (`image.create`, `image.edit`, `image.openai`, `image.remove_background`, `image.upscale.seedvr2`,
   `image.upscale.esrgan`, `text.generate`, `text.openai`, `vision.local`, `vision.openai`, `mesh.generate`, `rig.mia`, `rig.blender_auto`, `rig.articulated`, `blender`, `video.minimax` — et `video.minimax.modes.<mode>`, chaque mode ayant son propre statut).
2. `create_graph` avec le JSON (1.0 est migré en 1.1) ou partir d'un préréglage (`POST /api/v1/presets/{nom}`).
3. `plan_graph` : erreurs bloquantes (types, cycles, moteur texte seul + image, `max_items`, modes vidéo incompatibles),
   dépendances, items foreach et moteurs choisis — **aucune génération**. Seules les erreurs des nodes concernées bloquent :
   une node incomplète ailleurs donne un avertissement `outside_scope` ; en portée `graph`, une node à entrée manquante et
   ses descendantes sont ignorées (`skipped_invalid`).
4. `run_graph` (`scope`: `graph` | `until` | `branch` + `node_id`) avec `idempotency_key`. Le run fige la révision.
5. Suivre `get_job_status(job_id)` (ou les événements SSE `/api/v1/events?run_id=…`). Ne pas interroger en boucle serrée :
   une génération Krea ≈ 40 s, une édition ≈ 2,5 min, Pixal3D plusieurs minutes sur 8 Go.
6. Inspecter : `list_node_items` / `list_variants`, `get_preview` (images de contrôle réduites), `get_validation_report` ;
   `list_media` (ou `GET /api/v1/media?kind=image|video|mesh&q=…`) retrouve n'importe quel résultat produit, tous graphes confondus ;
   pour réutiliser un résultat comme source : `POST /api/v1/uploads/from-artifact {artifact_id}` puis `update_graph`
   (`params.upload_id` d'une node `image.import` / `mesh.import`).
   `delete_graph` / `restore_graph` : corbeille réversible — uniquement sur demande explicite de l'utilisateur.
7. Corriger **seulement** ce qui doit l'être (`rerun_item`, `rerun_node` avec portée explicite), puis `select_item_variant` /
   `select_variant` si la candidate est retenue.
8. `download_artifact(artifact_id, dossier_autorisé)` : chemin + taille + SHA-256 (jamais de base64 pour .blend/.mp4).

## Flow foreach (sections 17-18 du cahier des charges)

```python
from atelier3d_client import Atelier3D
c = Atelier3D(author="Claude Code")
doc = json.load(open("examples/pnj_concept_apose_parts.graph.json", encoding="utf-8"))
g = c.create_graph(doc)["graph"]
plan = c.plan(g["id"])                      # plan["foreach"]["parts"]["summary"] == "5 prompts × 1 référence commune → 5 résultats"
run = c.run_graph(g["id"], idempotency_key=f"pnj-{g['id']}")
c.wait_run(g["id"], run["run_id"])
hair = c.item_by_key(g["id"], "parts", "cheveux")          # item_id réel, jamais un index
rr = c.rerun_item(g["id"], "parts", hair["item_id"], scope="item_only", params_patch={"seed": 53002},
                  force_recompute=True, activate_output=False, expected_graph_revision=c.graph(g["id"])["graph"]["revision"],
                  idempotency_key=f"pnj-cheveux-candidate-2-{g['id']}")
job = c.wait(rr["jobs"][0]["job_id"]); vid = job["result"]["variant_id"]   # candidate, non active
c.select_item_variant(g["id"], "parts", hair["item_id"], vid)             # seuls les descendants de « Cheveux » deviennent à recalculer
```

Script complet : `examples/pnj_foreach_claude.py run` puis `… fix-hair <graph_id>`.

Règles : un foreach exige le mode `foreach_prompts` explicite (plusieurs prompts en mode `single` = erreur) ; la seed
effective d'un item = `(seed_base + int(sha256("item:" + item_key)[:8], 16)) mod 2^32`, stable au réordonnancement ;
`params_patch.seed` sur une relance d'item est utilisée telle quelle ; une même `idempotency_key` avec une autre charge
utile est refusée (409).

## Exemples exécutables

| But | Fichier |
|---|---|
| Image Krea 2, comparer deux variantes, choisir | `examples/claude_image_variants.py` |
| Humanoïde texturé → rig → .blend téléchargé | `examples/claude_character_pipeline.py` |
| Image choisie + prompt de mouvement → vidéo MiniMax | `examples/claude_video_from_image.py` |
| Corriger seulement le rig / régénérer seulement la vidéo | `examples/claude_partial_fixes.py` |
| ComfyUI direct (bas niveau) Krea / MiniMax importé | `examples/comfy_direct_krea2.py`, `examples/comfy_direct_minimax.py` |
| Test MCP stdio | `examples/mcp_smoke_test.py` |

## Conditions d'acceptation à vérifier avant d'annoncer un résultat

- Un statut final ne vaut que s'il est accompagné de ses preuves : « export Blender vérifié » (réouverture `.blend` +
  réimport `.glb`/`.fbx` OK), « rig à corriger » (poses de test avec avertissements ou sommets non pondérés), « échec ».
- Ne jamais déclarer « prêt pour production ». Doigts, visage, cheveux, vêtements longs : à vérifier visuellement.
- Une image générée de dos ou une pose IA est une hypothèse, pas une référence authentique.
- La vidéo produit un MP4, jamais une animation de squelette.

## Bonnes pratiques de reprise

- Une tâche interrompue (crash, arrêt) est réconciliée au démarrage du worker : le `prompt_id` ComfyUI enregistré **avant**
  soumission est vérifié ; un calcul terminé est collecté sans resoumission ; un calcul encore en cours est rattaché.
- `POST /api/v1/jobs/{id}/resume` ne remet en file que les étapes échouées/bloquées/interrompues ; les étapes validées
  sont conservées. `resume_item` pour un seul item.
- Un timeout côté client ne supprime jamais la tâche.
- Limiter les itérations (2-3 relances ciblées), conserver toutes les variantes (aucune suppression), s'arrêter sur
  erreur systémique (moteur indisponible, OOM répété, workflow invalide) et la signaler.
