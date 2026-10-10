# RESEARCH.md — sources vérifiées, licences et choix des moteurs

Vérifications effectuées le **5 octobre 2026** sur les sources primaires (API GitHub, API Hugging Face, fichiers de
licence téléchargés et lus, code installé). Les versions retenues sont figées dans `installer/versions.lock.json`
(URL, taille, SHA-256 ; SHA-1 git par fichier pour le code tiers) — généré par `installer/tools/generate_lock.py`.

## 1. Vidéos de référence

| Vidéo | Titre et date (oEmbed + page) | Contenu exploité |
|---|---|---|
| https://www.youtube.com/watch?v=Ff_tdMsndzQ | *This Open-Source 3D AI Workflow is Insane! (6GB VRAM)* — PixelArtistry, 24/08/2026 | Description : concept Krea2 Turbo → A-pose et séparation (Flux2 Klein + LoRA de séparation) → Trellis 2 / Pixal3D → projection multivues + baking → AutoRemesher (+ pont Blender) → baking haute/basse → assemblage Blender → rigging SkinTokens → UE5 |
| https://www.youtube.com/watch?v=o7VJnIN31Ds | *Best 3D AI Generator Now Runs Natively in ComfyUI! (Free&Local)* — PixelArtistry, 26/08/2026 | **Récupérée** (description publique) : Trellis 2 et Pixal3D intégrés nativement dans ComfyUI (PR #14718, réimplémentation PyTorch sans dépendance) ; 6–8 Go → Pixal3D int8 convrot ; steps 30–50 pour la qualité (12 par défaut) ; textures 4K si la VRAM le permet ; décimation ~300 000 faces (700 000 par défaut, « très élevé ») ; dépliage UV « Adaptive vs PEC » ; cartes exportées couleur, metallic/roughness, normal, AO ; astuce : décharger les modèles |

Le titre « 6GB » ne valide pas toute la chaîne : voir `COMPATIBILITY.md` pour ce qui a réellement tourné sur 8 Go.

## 2. Composants retenus

| Rôle | Retenu | Version figée | Licence (vérifiée) | Pourquoi |
|---|---|---|---|---|
| Moteur ComfyUI dédié | ComfyUI portable NVIDIA officiel | **v0.38.0** (tag `6b747c04…`), 7z SHA-256 `8f137eac…` (digest GitHub) | GPL-3.0 (code) | Contient nativement Krea 2, BiRefNet (v0.35), Pixal3D/TRELLIS.2 (v0.34, PR #14718 fusionnée le 22/08/2026), MoGe ; Python 3.13.14 + PyTorch 2.14.0 cu130 embarqués ; aucune extension CUDA à compiler |
| Image → 3D | **Pixal3D int8 convrot** via template officiel Comfy | `pixal3d_int8_convrot` SHA-256 `4621eac3…` (Comfy-Org/Pixal3D) | MIT (dépôt Comfy-Org/Pixal3D et TencentARC/Pixal3D ; composants tiers selon leur NOTICE) | Seule variante recommandée 6–8 Go ; TRELLIS.2 int8 présent pour le switch du template (validation ComfyUI) |
| Génération d'image | **Krea 2 Turbo fp8_scaled** (template officiel) | SHA-256 `eb4dd8c6…` (Comfy-Org/Krea-2, non protégé) | **Krea 2 Community License v1** (lue) : usage commercial seulement si CA annuel total < 1 M$ (sinon licence entreprise), **filtrage de contenu obligatoire** pour un déploiement (§4.2, revue humaine manuelle citée parmi les mesures possibles), divulgation IA si la loi l'exige, Acceptable Use Policy | Turbo pour l'inférence ; **Raw** (modèle d'entraînement) non retenu |
| Édition avec référence | **Krea 2 Identity Edit v1.2** (LoRA communautaire) + nodes `comfyui-krea2edit` | LoRA SHA-256 `6adf9a69…` ; nodes commit `86f886da` (Apache-2.0) | Poids : Krea 2 Community License (même texte, NOTICE : dérivé non officiel) | Seul profil local qui conditionne réellement par l'image (latents + Qwen3-VL) ; Turbo, 10 steps, CFG 1 (README du modèle) |
| Génération / édition par service externe (facultatif) | **API OpenAI Images** (`/v1/images/generations`, `/v1/images/edits`), appel HTTP direct (httpx, TLS vérifié via le magasin Windows) | modèles `gpt-image-*` listés à l'exécution avec la clé de l'utilisateur ; aucun SDK ajouté | Conditions d'utilisation d'OpenAI et facturation du compte de l'utilisateur (non reproduites ici) | Demande explicite de l'utilisateur ; désactivé tant qu'aucune clé n'est saisie ; uniquement sur les nodes réglées sur OpenAI. Documentation officielle consultée : guide « Image generation », références `images.generate` / `images.edit`, fiche du modèle GPT Image 2 (developers.openai.com, octobre 2026) |
| Upscale 2K / 4K (détails) | **SeedVR2 3B int8** (ByteDance Seed) via le template officiel « SeedVR2 3B Int8: Upscale Image » | `seedvr2_3b_int8_convrot` SHA-256 `c3dec8bc…` + VAE `seedvr2_ema_vae_fp16` `20678548…` (Comfy-Org/SeedVR2@df488797) | Apache-2.0 (dépôt Comfy-Org/SeedVR2) | Recherche du 06/10/2026 : restauration par diffusion en une étape classée parmi les plus fidèles (peu d'hallucinations, à la différence de Topaz) ; intégrée nativement à ComfyUI v0.38 ; seule variante prévue pour 8 Go (7B : 8,3 Go, non retenu ; SUPIR : plus lourd, nodes tierces) |
| Upscale 2K / 4K (rapide) | **Real-ESRGAN x4plus** (nodes du template officiel « GAN Upscaler ») | `RealESRGAN_x4plus.safetensors` `37f9a931…` (Comfy-Org/Real-ESRGAN_repackaged@5fd49b7b) | BSD-3-Clause | Quelques secondes, n'invente pas de détails ; 4x-UltraSharpV2 (meilleur rendu) écarté : licence CC-BY-NC-SA (usage commercial interdit) |
| Écriture de prompts | **Qwen3-VL 4B** (encodeur de Krea 2, déjà installé) via le template officiel « Qwen3-VL text generation » (node `TextGenerate` native) | même fichier que l'encodeur Krea 2 | Apache-2.0 (Qwen3-VL) | Local, sans téléchargement ; consigne en liste numérotée (une demande JSON échoue avec ce modèle, cf. WORKFLOWS.md § 2 quinquies) |
| Analyse d'image / écriture de texte (OpenAI, facultatif) | **API OpenAI Responses** (`POST /v1/responses`, `input_image` en data URL, `text.format` json_schema strict) | modèle lu avec la clé (`GET /v1/models`) ; préférence : `gpt-6.1-sol`, puis `gpt-6-luna`, `gpt-6-astra` (documentation officielle, octobre 2026 : Astra le plus capable, Sol équilibre coût/performance, Luna le plus économique) | conditions et facturation OpenAI | Demande explicite de l'utilisateur (« envoyer l'image à ChatGPT ») ; sortie structurée fiable |
| Détourage | **BiRefNet** natif (`RemoveBackground`) | SHA-256 `9ab37426…` (Comfy-Org/BiRefNet) | modèle publié par Comfy-Org (licence BiRefNet upstream MIT) | Vrai masque alpha, recalculable, sans dépendance |
| Rigging + skinning | **Make-It-Animatable (MIA) v1** | code commit `8fb51382` | code **MIT**, poids **Apache-2.0** (carte HF) | Seul moteur de rigging/skinning testé fonctionnel sur 8 Go (pic VRAM mesuré 3,7 Go) |
| Préparation, UV, baking, assemblage, tests, export | **Blender** (réutilisé : 5.2.1 LTS ; sinon 5.2.1 portable officiel, SHA-256 `0e631dad…`) | `--factory-startup`, scripts `app/blender_scripts` | GPL (Blender) ; scripts du projet | Baking Cycles plutôt qu'une dépendance de projection (cf. § 4) |
| Vidéo | **votre** ComfyUI MiniMax H3 (0.30.2, poids locaux FL2VA/Ref2VA int8, LoRA turbo) | workflow importé (copie) | conditions des poids MiniMax H3 non revérifiées ici (déjà installés par vous) | Exigence : réutiliser l'accès existant sans le remplacer |
| Interface | React 19.3 + **@xyflow/react 12.12** + three 0.186, construit par Vite 8.3 | `app/frontend/package-lock.json` | MIT | Servi en statique par le backend, aucun CDN à l'exécution |
| API / MCP | FastAPI 0.142, SQLite, SDK MCP Python **2.3.0** (`MCPServer`) | `app/backend/requirements.lock` (hachures) | MIT / BSD | Même logique pour UI, API, MCP |
| Runtimes | uv 0.12.23 (SHA-256 `75d05de6…`), CPython 3.12.15 / 3.11.17 portables, 7zr 26.03 (SHA-256 `ad4c82fa…`) | lock | MIT/Apache, PSF, LGPL | Environnements isolés, aucun Python global modifié |

Environnement MIA (`app/engines/mia_runner/requirements.lock`, hachures) : PyTorch **2.8.0+cu128** (index officiel
PyTorch), torch-cluster 1.6.3 (roues officielles PyG), pytorch3d 0.7.9 (index *miropsota/torch_packages_builder*, celui
que cite `requirements.txt` de MIA ; pytorch3d ne publie pas de roue Windows officielle), shapely, trimesh, timm, einops.
Le requirements upstream (torch 2.1.2/cu121) a été adapté à des roues Windows réellement publiées ; la combinaison a été
installée et exécutée sur la machine cible.

**Adaptation MIA (documentée dans `app/engines/mia_runner/`)** : `util/dataset_mixamo.py` charge à l'import
`data/Mixamo/bones.fbx` (jeu de données HF à accès protégé `jasongzy/Mixamo`) via `bpy`, seulement pour l'arbre
cinématique. L'inférence n'utilise de cet arbre que les relations parent/enfant et la profondeur (masques d'attention,
indépendants de l'ordre des enfants) : `mia_compat.py` reconstruit la hiérarchie Mixamo standard (52 os, mêmes noms et
index) sans le fichier protégé. Les poids et le squelette produits ont été vérifiés (positions anatomiques plausibles,
déformations réelles). Le modèle de pose (retour en T-pose) n'est pas utilisé : la pose du maillage livré reste la rest
pose. Écart volontaire : renormalisation des poids après le seuil des 4 influences (exigée par glTF).

## 3. Évalués et non retenus

| Composant | Constat | Décision |
|---|---|---|
| SkinTokens (VAST-AI-Research, MIT, commit `273b691d`) | README : ≥ 14 Go de VRAM | indisponible sur ce profil (8 Go), annoncé comme tel |
| UniRig (MIT, commit `6793c664`) | README : ≥ 8 Go ; dépendances spconv/flash-attn sous Windows ; wrapper ComfyUI-UniRig GPL-3.0 et recommande MIA pour les humanoïdes | non intégré (un seul moteur de rigging en V1) |
| MIA v2 (branche v2) | backbone **Hunyuan3D 2.1 ShapeVAE** : la licence Hunyuan3D-2.1 exclut notamment l'UE (territoire de l'utilisateur) | non retenu ; MIA v1 (MIT/Apache) retenu |
| Hunyuan3D-2.1 comme repli universel | restrictions territoriales | exclu |
| AutoRemesher (MIT, commit `3cb2012c`) | application graphique, pas de CLI documentée pour l'automatisation | non intégré ; QuadriFlow (Blender) proposé comme retopologie quads libre, décimation par défaut |
| ComfyUI-Trellis2 (visualbruno), ComfyUI-Trellis2-GGUF (Aero-Ex) | intégration native ComfyUI disponible depuis la v0.34 | inutiles ; aucun wrapper ni roue tierce installé |
| Projection multivues / baking ComfyUI (Aero-Ex) | dépendance supplémentaire, conditions non vérifiées | baking haute → basse fait dans Blender (Cycles) |
| ComfyUI-Easy-Install | portable officiel suffisant ; pas de dépendance aux espaces/accents constatée pour le portable (testé dans `E:\Atelier 3D Données`) ; jonction ASCII créée par prudence | non utilisé |
| 3DGenStudio (visualbruno) | studio existant étudié ; architecture propre retenue (graphe métier + adapters) | aucune réutilisation de code |
| Krea 2 Raw | modèle de base pour l'entraînement | non utilisé pour l'inférence |
| API MiniMax (platform.minimax.io v2) | votre accès est un ComfyUI **local** (aucun appel API) | non utilisée ; aucune clé demandée |

## 4. Votre accès « MiniMax H3 Ultra »

Constat sur la machine : `E:\AI\VidGen\ComfyUI_windows_portable` (ComfyUI 0.30.2, Python 3.13.12, PyTorch 2.11.0 cu130),
nodes natives `MiniMaxH3ImageToVideo` (FL2VA) et `MiniMaxH3ReferenceToVideo` (Ref2VA), poids locaux
`minimax_h3_fl2va_pruned_int8_convrot`, `minimax_h3_ref2va_pruned_int8_convrot`, encodeurs Qwen3-VL 32B int8 / nvfp4,
VAE vidéo/audio, LoRA `minimax_h3_turbo_4step_ckpt500_pruned_comfyui`. Workflow « MINIMAX H3 ULTRA + TURBO LORA »
(pack Aitrepreneur) : « Ultra » est le nom du workflow, pas un modèle. **Exécution locale, hors ligne une fois lancé.**
Un reçu de génération du 30/09/2026 (Kaelith) documente un premier essai en échec (`hostbuf_file_reader_read failed`)
puis une réussite avec un profil mémoire réduite, que l'application réutilise.

## 5. Remarques de vérification

- Les « 6 Go » annoncés par la vidéo concernent Pixal3D int8 ; la chaîne complète n'est revendiquée que pour ce qui a
  été exécuté (8 Go, voir `TEST_REPORT.md`).
- Certificat de download.blender.org refusé par l'OpenSSL de Python (chaîne jugée expirée) mais accepté par Windows :
  les outils utilisent `truststore` (magasin Windows) ; la validation TLS n'est jamais désactivée.
- Les fichiers Krea d'un miroir tiers présents sur la machine (`Aitrepreneur/FLX`) ne sont réutilisés que si leur SHA-256
  est identique au fichier officiel : c'était le cas de l'encodeur Qwen3-VL et du VAE, pas du modèle Krea Turbo
  (retéléchargé depuis Comfy-Org).
- Aucune mention « utilisation commerciale autorisée » n'est faite sans condition : Krea 2 et la LoRA d'édition sont
  soumises au seuil de 1 M$ et à l'obligation de filtrage de contenu ; vérifiez aussi les conditions des poids MiniMax H3
  que vous avez installés.
