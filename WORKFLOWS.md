# WORKFLOWS.md — moteurs, workflows ComfyUI, foreach, variantes et cache

## 1. Workflows livrés (`app/workflows/comfy/<id>/`)

Chaque dossier contient `workflow.api.json` (exécutable, issu de **l'export API officiel** `app.graphToPrompt()` du frontend
ComfyUI 1.53.6, sous-graphes aplatis par le frontend lui-même), `workflow.visual.json` (copie éditeur) et `bindings.json`
(entrées/sorties exposées, modèles et classes requis, limites, provenance). Avant chaque exécution l'adapter interroge
`/object_info` et `/models/<dossier>` du ComfyUI réellement lancé et refuse le workflow si une classe, un input ou un
modèle manque. Alias résolus par l'installateur (`config/atelier3d.json` → `workflows.aliases`) uniquement vers ces
templates validés ; un alias non résolu fait échouer la planification avant toute génération.

| Alias | Template | Source | Entrées exposées | Sorties |
|---|---|---|---|---|
| `image_create_default` | `krea2_turbo_t2i` | template officiel Comfy `image_krea2_turbo_t2i` | prompt `30:19.value`, seed `30:3.seed`, largeur/hauteur `30:5`, affinage Qwen3-VL `30:24.value` | image `29`, prompt final `30:20` |
| `image_edit_default` | `krea2_identity_edit` | `comfyui-krea2edit@86f886da` `workflows/krea2_identity_edit.json` | instruction `84.prompt`, image `72.image`, seed/steps `53`, ref_boost `79`, grounding_px `84/85`, dimensions `82` | image `29` |
| `remove_background_default` | `birefnet_remove_background` | template officiel Comfy `utility_birefnet_remove_background` | image `17.image` | RGBA `18`, masque `20` |
| `image_to_mesh_default` | `pixal3d_image_to_model` | template officiel Comfy `3d_pixal3d_trellis2_image_to_model` | image `122`, seeds `3/18/23/12`, texture `288`, décimation `186`, Pixal3D/TRELLIS.2 `316`, détourage `248` | GLB `322`, cartes `164/207/208/226/235`, atlas UV `262` |
| `upscale_seedvr2_default` | `seedvr2_3b_upscale_image` | template officiel Comfy `utility_seedvr2_3b_int8_upscale_image` (sous-graphe aplati) | image `1.image`, multiplicateur `66:57.resize_type.multiplier`, seed `66:54`, correction des couleurs `66:59` | image `9` |
| `upscale_esrgan_default` | `realesrgan_upscale_image` | nodes `UpscaleModelLoader` + `ImageUpscaleWithModel` du template officiel `utility-gan_upscaler` (vidéo), entrées/sorties remplacées par `LoadImage`/`SaveImage` dans le frontend | image `13.image` | image ×4 `14` |
| `text_generate_default` | `qwen3vl_text_generate` | template officiel Comfy `llm_qwen3vl_text_gen` (image d'exemple retirée) | consigne `6.prompt`, longueur `6.max_length`, température/seed `6.sampling_mode.*` | texte `7` |
| `vision_generate_default` | `qwen3vl_vision_generate` | template officiel Comfy `llm_qwen3vl_text_gen`, tel quel (avec son entrée image) | image `8.image`, consigne `6.prompt`, longueur, température/seed | texte `7` |
| `minimax_user` | `minimax_h3_ultra_user_<mode>` | **votre** workflow « MINIMAX H3 ULTRA + TURBO LORA » (copie importée), converti par le frontend de **votre** ComfyUI | voir § 3 | MP4 |

Ajustements documentés (aucune modification des workflows sources) :
- valeurs de liste écrites avec `/` alors que ComfyUI Windows liste `\` (LoRA `Krea2/…`) : remplacées par la valeur **exacte**
  annoncée par `/object_info` (journalisé dans la tâche) ;
- entrées RGBA vers un workflow qui n'exploite pas l'alpha (`LoadImage` de l'édition) : composées sur blanc avant envoi,
  sinon l'ancien fond réapparaît ;
- le template Krea référence une LoRA de style désactivée par un switch : ComfyUI la valide quand même, elle est donc
  installée (472 Mo, source officielle) plutôt que de supprimer la branche.

## 2. Krea 2

- **Créer** : Krea 2 Turbo `krea2_turbo_fp8_scaled` + Qwen3-VL 4B fp8 + VAE Qwen-Image, **8 steps, CFG 1, euler/simple**
  (preset du template officiel ; les exemples Diffusers utilisent guidance 0 : non transposé). Texte seul : un branchement
  d'image sur une opération Créer est refusé à la planification.
- **Éditer** : LoRA Krea 2 Identity Edit v1.2 (communautaire, licence Krea 2 Community) sur Turbo, **10 steps, CFG 1,
  ref_boost 4, fit, grounding_px 768**. Double conditionnement réel : `Krea2EditModelPatch` (latents de la référence,
  `target_latent` câblé sur la latente vide → encodage avant échantillonnage) et `Krea2EditGroundedEncode` (Qwen3-VL avec
  l'image). Une seule référence exposée en V1 ; le mode deux références du workflow (ordre fixe scène puis sujet) n'est pas
  exposé. Limites constatées : identité « proportion-conservative », suppressions peu fiables, instruction appliquée à
  toute l'image (une planche à trois vues reste une planche).
- Le prompt utilisateur et le prompt final réellement envoyé sont enregistrés (`outputs/prompts.json`).
- **Détourage** : BiRefNet natif ; le PNG RGBA est vérifié (présence et proportion de transparence), sinon l'étape échoue.
  `postprocess.remove_background` d'une node « Générer une image » = macro de deux sous-étapes relançables séparément.
- **Deux nodes distinctes** : « Générer une image » (`image.generate`, texte seul) et « Éditer une image » (`image.edit`,
  port Image requis). Les anciens graphes (`image.generate` + `operation: "edit"`) sont convertis à la lecture, avec la
  même empreinte : leurs résultats restent « À jour » (vérifié sur les graphes réels de l'installation de test).

## 2 bis. OpenAI Images (facultatif, service externe)

- Moteur `openai` des deux nodes image, choisi node par node dans l'inspecteur (`krea2` reste le défaut). Appels directs à
  l'API OpenAI Images : `POST /v1/images/generations` (JSON) pour Générer, `POST /v1/images/edits` (multipart, champ
  `image`, ou `image[]` avec la seconde référence `image_b`) pour Éditer ; réponse `b64_json` enregistrée en PNG.
- Clé saisie dans **⚙ Réglages → OpenAI** (champ masqué), chiffrée Windows DPAPI (`config\secrets.dat`), jamais renvoyée
  au navigateur, jamais écrite dans les journaux, les tâches, les variantes ni `run.json`. « Tester la clé » interroge
  `GET /v1/models` et ne propose que les modèles `gpt-image-*` réellement accessibles au compte.
- Modèle : celui de la node, sinon celui des Réglages, sinon recommandation automatique (édition : `gpt-image-2.5-sunburst`,
  génération : `gpt-image-2.5-flare`, si disponibles). Qualité et modération : node, sinon Réglages. Taille : personnalisée
  (multiple de 16) pour GPT Image 2 et suivants, taille fixe la plus proche pour `gpt-image-1*`.
- Exécution sur la file **net** (ni GPU ni CPU local) : une génération OpenAI peut tourner pendant un calcul Pixal3D.
- Clé de cache : entrées + paramètres + valeurs par défaut des Réglages (changer le modèle par défaut recalcule).
- Rapport d'étape : modèle, taille, qualité, consommation (`usage`) renvoyée par OpenAI, prompt éventuellement révisé.
- Erreurs expliquées : clé absente/refusée, vérification d'organisation requise, consigne refusée par la modération,
  quota épuisé, limite de débit (une nouvelle tentative automatique). Pas de seed : « Régénérer » refait un appel.

## 2 ter. Préparer l'image : pose A / T

- `pose: a_pose | t_pose` ajoute une sous-étape « pose » AVANT le détourage : édition Krea 2 Identity Edit (local, 10 steps,
  `ref_boost` 2 par défaut, réglable) ou OpenAI (`engine: openai`, service externe). Consigne anglaise fixe (bras à 45° /
  à l'horizontale, mains ouvertes, corps entier, mêmes visage, coiffure, tenue et accessoires, fond gris clair), complétée
  par `pose_details`. Image au format de la source (~1 Mpx). C'est une **nouvelle image dessinée par l'IA** : mains, doigts,
  visage et accessoires sont à vérifier ; « Régénérer » propose une autre tentative (candidate).
- `remove_background: false` : pas de détourage ; le cadrage, qui s'appuie sur le canal alpha, transmet alors l'image telle
  quelle (avertissement dans le rapport).
- Sans pose et avec détourage (défauts), les sous-étapes et leurs réglages sont exactement ceux d'avant : les résultats déjà
  calculés restent « À jour » (vérifié sur les graphes réels).

## 2 quater. Upscale 2K / 4K

- **SeedVR2 3B int8** (défaut, « détails ») : restauration par diffusion en une étape (template officiel, VAE en tuiles
  512/128). Atelier 3D calcule le multiplicateur `taille cible / côté le plus long` (≤ 8), corrige l'arrondi au pixel près
  (lanczos) et choisit la correction de couleurs `lab` (la plus fidèle d'après l'infobulle de la node ; le template utilise
  `none`). Le canal alpha passe par `LoadImage → JoinImageWithAlpha` ; s'il manque au résultat, le masque d'origine agrandi
  est réappliqué.
- **Real-ESRGAN x4plus** (« rapide ») : ×4 puis réduction lanczos à la taille cible (conseil de la note du template officiel),
  alpha réappliqué (entrée composée sur gris avant envoi).
- Image source déjà ≥ cible : simple réduction lanczos, sans IA (avertissement).
- Sorties : PNG à la taille exacte, comparaison côte à côte et zoom 1:1 au centre (source agrandie sans IA ↔ résultat).

## 2 quinquies. Prompts aléatoires (Qwen3-VL 4B local)

- Workflow `qwen3vl_text_generate` : même fichier de poids que l'encodeur de Krea 2, aucun téléchargement. La consigne demande
  N paragraphes de 40 à 80 mots (sujet, décor, cadrage, lumière, couleurs, matières, ambiance), différents entre eux et
  fidèles à l'univers nommé dans le contexte, **en liste numérotée** : constat du 06/10/2026, une demande de réponse JSON fait
  répondre au modèle un faux objet `{"error": …}` (6 essais sur 6) alors que la liste numérotée réussit à chaque essai.
  Réponse incomplète → une seconde tentative (seed + 1), puis échec explicite avec la réponse brute.
- Les prompts sont stockés dans les métadonnées de l'artefact `prompts.json` de la variante : affichés dans la node,
  résolus en texte pour chaque item ; l'empreinte d'un item est celle de SON texte. Régénérer la liste puis « Utiliser » ne
  rend « À recalculer » que les items dont le prompt a changé.
- Sortie `prompts` (liste) sur l'entrée `prompts` d'un générateur « pour chaque prompt » : un item par prompt, clés stables
  `<clé de la noodle>_1 … _N` (changer `count` ajoute ou archive des items). Sortie `prompt:<n>` : un seul prompt (entrée
  simple, « Composer prompt »…). Un « Composer prompt » alimenté par une liste écrite dans le même run est composé par le
  worker au moment de l'exécution.

## 2 sexies. Extraction de props

- **Analyse** (étape `analyze`, une variante par analyse) :
  - OpenAI : API Responses, image en data URL (`detail: high`, réduite à 1536 px, alpha composé sur gris), sortie JSON stricte
    `{character, parts[{name, kind, symmetric, description}]}` ;
  - local : workflow `qwen3vl_vision_generate`, réponse ligne par ligne `1. nom | body/prop | yes/no | description`.
  Normalisation déterministe dans les deux cas : clé = nom en minuscules sans accents ; seules « head » et « body » sont du
  corps ; bras, jambes et mains isolés retirés (la partie « body » les couvre) ; paires gauche/droite fusionnées en « right … »
  marquée symétrique si demandé. Constat sur Qwen3-VL 4B : il liste les pièces correctement mais classe mal leur catégorie et
  ne fusionne pas les paires, d'où ces règles.
- **Écriture des parties** : à la réussite (variante devenue la sortie utilisée), le worker écrit `character` et `parts` dans la
  node en gardant les choix de l'utilisateur pour les clés déjà connues (coché, nom) et les parties ajoutées à la main.
  Choisir une autre variante d'analyse réécrit sa liste.
- **Groupe « Props »** : créé une fois (`props_<id>`, `image.edit` « pour chaque prompt », `ref_boost` 1,5, moteur OpenAI si
  l'analyse est OpenAI, sinon Krea 2), entrée `prompts` = une consigne par partie cochée, entrée `image` = image source
  transmise. Consigne d'extraction (anglais) : « Show only the <nom> from this character (<personnage>), alone and not worn,
  complete…, plain light grey background… » ; tête : vue de face isolée ; corps : même pose, combinaison neutre grise.
- **Run** : si l'analyse est (re)calculée, les étapes qui en dépendent sont retirées du run et relancées automatiquement après
  l'écriture des parties (`continue_after_analysis`, événements `props_analyzed` puis `run_continued`).

## 3. MiniMax H3 « Ultra » — votre accès existant

Constaté sur la machine cible : ComfyUI portable **0.30.2** local (`E:\AI\VidGen\ComfyUI_windows_portable`), nodes natives
`MiniMaxH3ImageToVideo` (FL2VA : texte, première image, dernière image) et `MiniMaxH3ReferenceToVideo` (Ref2VA : jusqu'à 9
images, 3 vidéos, 3 audios ; tags `<Picture i>` dans le prompt), poids **locaux** int8 (FL2VA, Ref2VA), encodeur Qwen3-VL
32B, LoRA turbo 4 steps. Exécution **locale, hors ligne une fois lancé** ; aucun appel à l'API MiniMax. « Ultra » est le
nom du workflow (pack Aitrepreneur), pas un identifiant de modèle.

- Atelier 3D **se rattache** à `http://127.0.0.1:8188` si l'instance tourne ; sinon il peut la lancer (`video.may_launch`)
  avec son Python et ses arguments, + le profil mémoire réduite qui a réussi le 30/09/2026 (`--fast-disk
  --disable-async-offload --disable-pinned-memory --reserve-vram 1`). Ce processus est arrêté par `STOP.bat` ; une instance
  lancée par vous n'est jamais arrêtée ni interrompue (interruption ciblée sur nos `prompt_id` uniquement).
- Avant une vidéo, le ComfyUI dédié décharge ses modèles (`POST /free`) ; après, si la file de votre instance est vide,
  ses modèles sont déchargés.
- Durée → images : `n = max(5, round(s × 24))` arrondi à la grille `17k + 5` (formule de votre workflow).
- Références : slots ordonnés (rôle, sujet, description) → entrées `ref_image_1…` dans le même ordre + préfixe de prompt
  `<Picture i> : Nom (rôle) — description`. Première/dernière image et références d'identité sont des modes distincts :
  le mélange est refusé à la planification. Pas de collage d'images vers une entrée unique.
- Sorties : MP4 (piste audio du workflow conservée si présente), miniature, 5 frames horodatées, `metadata_video.json`
  (fournisseur, mode, workflow, prompt original et final, paramètres, références).

## 4. Foreach, items et previews automatiques

- Modes : `single` et `foreach_prompts` **intégrés** ; `foreach_images`, `zip`, `cartesian` annoncés mais désactivés avec
  leur raison (refus explicite à la planification).
- Chaque noodle vers l'entrée `prompts` porte un `item_key` immuable et un `order`. La planification attribue un `item_id`
  persistant par `item_key`, crée une node `image.preview` par item (`auto: {owner_node_id, item_id, item_key}`) reliée au
  port stable `image:<item_key>`, et l'enregistre dans le graphe (nouvelle révision). Renommer ou réordonner un prompt ne
  change ni `item_id`, ni seed, ni preview ; retirer un prompt archive son item et sa preview (connexions conservées et
  signalées).
- Un item = une tâche indépendante : un prompt + la même variante de référence. Aucun prompt concaténé, aucune planche.
  `max_items` est vérifié avant toute génération. Une erreur d'item n'arrête pas les autres (`on_item_error: continue`).
- Seed effective d'item : `(seed_base + int(sha256("item:" + item_key)[:8], 16)) mod 2^32` ; `params_patch.seed` d'une
  relance est utilisée telle quelle. Les seeds effectives sont dans `run.json`, le cache et chaque variante.
- « Régénérer ce résultat » (preview) = `POST …/items/{item_id}/rerun` avec `scope: item_only` sur le seul item producteur.
- L'état « amont à recalculer » d'une preview ou d'une node branchée sur `image:<item_key>` suit **cet item seulement** :
  un autre item à recalculer ne la touche pas.
- Prompts des parties (constats réels, TEST_REPORT.md § 3) : avec Krea 2 Identity Edit, les consignes françaises
  « Isole … » du cahier des charges et `ref_boost` 4 ont gardé le personnage entier (pas de gros plan) et ont parfois
  modifié la partie demandée ; `ref_boost` 4 avec un gros plan a même recopié la référence entière en miniature dans
  l'image. Le préréglage utilise donc des consignes de cadrage explicites (« Close-up portrait… », « product photo…,
  no person ») et `ref_boost` 1,5 pour le nœud « Parties » ; l'exemple `examples/pnj_concept_apose_parts.graph.json`
  garde les prompts du cahier des charges (`spec_prompts: true` pour le préréglage). L'effet de la langue seule n'a pas
  été isolé.

## 5. Variantes, liens et obsolescence

- Unité de calcul : (node, sous-étape, item). Chaque réussite crée une **variante immuable** (sorties, entrées résolues,
  paramètres, seed, moteur, auteur, statistiques). Rien n'est supprimé.
- Politique d'activation sérialisée : `activate_first_success_only` (défaut) — une variante ne devient active que si aucune
  sortie active n'existe ; une relance ne remplace jamais la sortie active (`activate_output: true` pour le forcer).
  Distinction **relance / actualisation** :
  - *relance* (« Régénérer », « Forcer le recalcul », `rerun_node` / `rerun_item`) d'un résultat : la nouvelle variante est
    une **candidate**, la sortie utilisée ne change qu'avec « Utiliser » (règle du cahier des charges) ;
  - *actualisation* (« Exécuter », « Exécuter le graphe », `run_graph`) d'une étape dont la sortie est **absente ou
    périmée** (« À recalculer » : prompt, réglage ou amont changé) : le nouveau résultat **devient la sortie utilisée**, ainsi
    que les étapes aval recalculées dans le même run. Avant de calculer, si une variante déjà produite avec exactement les
    entrées et réglages actuels existe (même empreinte), la plus récente est **réutilisée sans recalcul**.
  Défaut corrigé le 06/10 : une étape périmée exécutée restait candidate, donc « À recalculer » ; chaque « Exécuter » en
  aval la recalculait — avec une nouvelle seed tirée au hasard si la node n'en avait pas — et toute la chaîne suivait.
- Seed : une node générative reçoit une seed fixe à sa création (ou à la première planification si elle n'en a pas) ;
  « Exécuter » est donc reproductible et profite du cache ; « Régénérer » utilise une nouvelle seed pour cette relance.
- Liens : `follow_active` (défaut) ou `pinned` sur un `variant_id`. Changer une sortie active rend « À recalculer »
  uniquement les slots dont les **entrées effectives** changent (empreinte des références + paramètres pertinents de la
  sous-étape, seed exclue) ; un lien épinglé reste valide. « Utiliser + restaurer la chaîne » resélectionne les variantes
  descendantes dont la provenance correspond.
- Chaque run fige le graphe (révision + snapshot) ; une édition pendant un calcul crée une révision sans affecter le run.

## 6. Cache

Clé = type de node, sous-étape, moteur (workflow + SHA-256 du workflow + modèles requis, version Blender, commit MIA),
paramètres effectifs **dont la seed**, **contenu** des entrées (SHA-256 des fichiers, empreinte du texte), schéma.
Un succès en cache crée une variante marquée « réutilisé du cache » (`from_cache_of`). « Forcer le recalcul »
(`force_recompute: true`) ignore ce cache et réexécute réellement l'étape ; sur le ComfyUI dédié, l'éventuel cache interne de
ComfyUI n'est jamais purgé pour d'autres utilisateurs. Même seed ≠ reproductibilité bit à bit entre runtimes : le contexte
(versions, workflow, modèles) est enregistré.

## 7. Modes de références disponibles

| Mode | Entrées | Disponibilité |
|---|---|---|
| Édition image (Krea) | 1 image + instruction | intégré |
| Édition deux références (scène, sujet) | 2 images, ordre fixe | présent dans le workflow source, **non exposé** en V1 |
| Vidéo image → vidéo | prompt + première image | selon conversion de votre workflow (voir `COMPATIBILITY.md`) |
| Vidéo début/fin | prompt + première + dernière image | idem |
| Vidéo références | prompt + jusqu'à 9 images ordonnées | idem |
| Images clés temporelles | — | non intégré (aucun workflow multiframe compatible installé) |
