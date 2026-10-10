# Compatibilité — Atelier 3D

Ce document distingue trois statuts, sans arrondi optimiste :

- **testé** : exécuté réellement sur la machine décrite ci-dessous, avec résultat vérifié ;
- **attendu** : rien ne s'y oppose dans le code ni dans la documentation amont, mais **non exécuté** ;
- **non supporté** : connu pour ne pas fonctionner, ou volontairement exclu.

Aucune compatibilité 6 Go / 12 Go / 16 Go n'est revendiquée : seule la carte 8 Go ci-dessous a été testée.

## 1. Machine de test (seule configuration « testée »)

| Élément | Valeur relevée |
|---|---|
| Système | Windows 11 Pro 23H2, build 22631, x64 |
| Processeur | AMD Ryzen 5 5600 (6 cœurs) |
| Mémoire vive | 32 Go (31,9 Go visibles) |
| Carte graphique | NVIDIA GeForce RTX 3060 Ti, **8 Go** (8192 Mio) |
| Pilote NVIDIA | 610.62 (relevé par `nvidia-smi`) |
| Disques | installation sur E:, Blender existant sur F: |
| Chemins | `E:\App\Gpasdenon` (distribution), `E:\Atelier 3D Données` (données : espaces + accent), jonction ASCII `E:\Atelier3D_runtime` |
| Logiciels déjà présents | ComfyUI portable 0.30.2 de l'utilisateur (`E:\AI\VidGen`, MiniMax H3), Blender 5.2.1 LTS (`F:\Blender`), autres ComfyUI personnels (non modifiés) |

Pendant une partie des mesures, d'autres applications de l'utilisateur utilisaient le GPU (Unity, un autre ComfyUI personnel en cours de calcul). Les durées ci-dessous sont donc **réalistes pour un poste de travail partagé**, pas des mesures de banc d'essai isolé ; voir TEST_REPORT.md pour le détail par exécution.

## 2. Systèmes et matériel

| Configuration | Statut | Remarque |
|---|---|---|
| Windows 11 x64 23H2 | **testé** | installation complète, deuxième installation depuis un chemin accentué, reprise de téléchargement interrompu |
| Windows 10 x64 22H2 | attendu | PowerShell 5.1 et `curl.exe` sont présents depuis Windows 10 1803 ; aucune fonction propre à Windows 11 n'est utilisée. Non exécuté. |
| Windows ARM64, Windows 32 bits | non supporté | ComfyUI portable NVIDIA et PyTorch CUDA sont x64 uniquement |
| NVIDIA RTX, 8 Go de VRAM | **testé** (RTX 3060 Ti) | chaîne complète image → 3D → rig → export, et vidéo MiniMax |
| NVIDIA, 12 Go ou plus | attendu | même logiciel ; plus rapide (moins de déchargement). Non mesuré. |
| NVIDIA, 6 Go | non vérifié | Krea 2 (12,5 Go de poids) fonctionne en VRAM dynamique avec déchargement vers la RAM ; à 6 Go, rien n'a été mesuré. Le profil réduit automatique (après un manque de mémoire) existe mais n'a pas été validé à 6 Go. |
| NVIDIA série GTX 9xx / 10xx (Maxwell, Pascal) | **non supporté** | CUDA 13 (PyTorch du ComfyUI dédié) ne prend en charge que Turing et plus récent (notes de version CUDA 13.0) ; l'installateur le détecte et ne coche pas les composants IA |
| NVIDIA GTX 16xx / RTX 20xx (Turing) | attendu, non testé | génération prise en charge par CUDA 13 ; VRAM souvent 6 Go (limite basse) |
| Pilote NVIDIA < 580 | **non supporté** | CUDA 13 exige le pilote 580 ou plus récent ; l'installateur le détecte (« CUDA max » < 13) et demande la mise à jour avant d'installer les composants IA |
| AMD / Intel (GPU) | non supporté | ComfyUI portable « nvidia », MIA et le pré-calcul Blender Cycles GPU utilisent CUDA |
| 32 Go de RAM | **testé** | Krea 2 est en partie placé en RAM (« 12530MB Staged ») |
| 16 Go de RAM | non vérifié | probable saturation pendant Krea 2 et MiniMax (encodeur de texte 32B) ; non mesuré |
| Espace disque | **testé** | voir § 6 |

## 3. Environnements d'exécution (aucun n'est partagé avec vos logiciels existants)

| Composant | Version | Origine | Statut |
|---|---|---|---|
| Application (FastAPI, worker, MCP, CLI) | CPython 3.12.15 portable (uv 0.12.23) | `venv-app`, dépendances verrouillées par hachage | **testé** |
| ComfyUI dédié (image, détourage, 3D) | ComfyUI v0.38.0 (commit `6b747c04`), Python embarqué 3.13.14, PyTorch 2.14.0+cu130, frontend 1.53.6 | archive portable officielle (GitHub Releases), SHA-256 vérifié, port 8190 | **testé** |
| Nodes `comfyui-krea2edit` | commit `86f886da` | archive GitHub figée, empreintes git vérifiées | **testé** |
| Rigging Make-It-Animatable | code commit `8fb51382` (MIT), poids Apache-2.0 ; CPython 3.11.17, PyTorch 2.8.0+cu128, torch-cluster, pytorch3d | `venv-mia` séparé | **testé** |
| Blender | 5.2.1 LTS (le vôtre, réutilisé tel quel) | `F:\Blender\blender.exe` | **testé** |
| Blender ≥ 4.2 et < 5.2 | — | téléchargé ou réutilisé | attendu (scripts écrits pour l'API 4.2+ ; non exécuté sur ces versions) |
| Blender absent | 5.2.1 portable | download.blender.org, SHA-256 verrouillé | attendu (chemin de code présent, non exécuté sur cette machine qui avait déjà Blender) |
| ComfyUI vidéo (le vôtre) | ComfyUI 0.30.2, Python 3.13.12, PyTorch 2.11.0+cu130 | `E:\AI\VidGen\ComfyUI_windows_portable` — **non modifié** | **testé** (lancé ou rattaché sur 8188) |

L'installateur ne modifie ni votre ComfyUI ni son PyTorch : il lit ses workflows, en copie les versions converties dans les données de l'Atelier, et le lance avec ses propres arguments (plus le profil basse mémoire) uniquement s'il n'est pas déjà ouvert.

## 4. Modèles, quantifications et licences

| Rôle | Fichier | Taille | Quantification | Statut |
|---|---|---|---|---|
| Image (création) | `krea2_turbo_fp8_scaled.safetensors` | 12,2 Gio | fp8 scaled | **testé** |
| Encodeur de texte Krea 2 | `qwen3vl_4b_fp8_scaled.safetensors` | 4,9 Gio | fp8 scaled | **testé** |
| VAE image | `qwen_image_vae.safetensors` | 0,24 Gio | — | **testé** |
| Édition avec référence | `Krea2/krea2_identity_edit_v1_2.safetensors` (LoRA) | 1,7 Gio | — | **testé** (1 référence) |
| LoRA exigée par le template officiel | `krea2_darkbrush.safetensors` | 0,44 Gio | — | présente (validation du template) ; non utilisée par défaut |
| Détourage | `birefnet.safetensors` | 0,41 Gio | — | **testé** (alpha réel vérifié) |
| Upscale (détails) | `seedvr2_3b_int8_convrot.safetensors` + `seedvr2_ema_vae_fp16.safetensors` | 3,22 + 0,47 Gio | int8 convrot / fp16 | **testé** (2K et 4K, alpha conservé) |
| Upscale (rapide) | `upscale_models/RealESRGAN_x4plus.safetensors` | 0,06 Gio | fp32 | **testé** (2K et 4K, alpha conservé) |
| Écriture de prompts | `qwen3vl_4b_fp8_scaled.safetensors` (même fichier que l'encodeur Krea 2) | — | fp8 scaled | **testé** (4 prompts en 23 à 44 s) |
| Analyse d'image (props, local) | même fichier, workflow avec entrée image | — | fp8 scaled | **testé** (12 à 24 s par analyse) |
| Image → 3D | `pixal3d_int8_convrot.safetensors` + `trellis_2_int8_convrot` + VAE TRELLIS.2 forme/texture bf16 + DINOv3 L fp32 + MoGe-2 ViT-L fp16 | ≈ 13,7 Gio au total | int8 convrot / bf16 / fp16 | **testé** |
| Rigging | poids Make-It-Animatable (Mixamo 52 os) | — | fp32 | **testé** |
| Vidéo | vos fichiers MiniMax H3 : FL2VA int8 convrot, Ref2VA int8 convrot, encodeur Qwen3-VL 32B int8 / nvfp4_awq, VAE vidéo fp16, VAE audio fp32, LoRA turbo 4 steps | déjà présents | int8 / nvfp4 | image → vidéo, références → vidéo (2 sujets), texte → vidéo et première/dernière image **testés** ; références → vidéo à 1 sujet : non exécuté |

Licences et obligations : voir RESEARCH.md. En particulier, Krea 2 (Community License v1) n'autorise l'usage commercial que sous le seuil de chiffre d'affaires prévu par la licence et impose un filtrage de contenu (dont une revue humaine) ainsi que la mention du caractère généré par IA. Hunyuan3D 2.1 n'est pas utilisé (licence excluant l'UE, le Royaume-Uni et la Corée du Sud).

## 5. Moteurs de rigging

| Moteur | Statut | Remarque |
|---|---|---|
| Make-It-Animatable + liaison Blender | **testé** | humanoïdes ; squelette Mixamo 52 os ; validation automatique par poses (le verdict honnête a été « rig à corriger » sur le PNJ testé) |
| Squelette MIA + poids automatiques Blender | **testé** | repli automatique sur un double remaillé quand la diffusion de chaleur échoue (cas des maillages générés) ; verdict obtenu « rig à corriger » |
| Rig articulé (objets rigides) | **testé** (mode automatique) | un maillage généré est souvent fragmenté : un seul os et demande de pivots ; avec pivots fournis : non exécuté |
| SkinTokens | non supporté sur ce profil | le dépôt amont annonce ≥ 14 Go de VRAM |
| UniRig | non intégré en V1 | un seul moteur retenu (MIA) |
| Quadrupèdes, serpents, créatures non humanoïdes | non supporté | le squelette MIA est humanoïde |

## 6. Durées et mémoire mesurées (RTX 3060 Ti 8 Go, 32 Go RAM)

Durées d'étape mesurées par le worker (début → fin, y compris chargement des modèles quand ils n'étaient pas déjà en mémoire) :

| Étape | Exécutions réelles | Durée mesurée |
|---|---|---|
| Krea 2 Turbo, création 1024², 8 steps | 3 | 41 à 50 s (dont chargement) ; < 1 s en cache exact |
| Krea 2 Identity Edit, 1 référence, 10 steps | 10 | 129 s médiane, 185 s max |
| Détourage BiRefNet | 3 | 3 à 4 s |
| Pose A / T (« Préparer l'image », Krea 2 Identity Edit, 10 steps) | 2 | 136 s (T → A), 144 s (A → T) |
| Upscale SeedVR2 3B int8 | 2 | 1024 → 2048 : 13 s ; 1024 → 4096 : 41 s (121 tuiles VAE, aucun manque de mémoire sur 8 Go) |
| Upscale Real-ESRGAN x4 (+ réduction lanczos) | 2 | 1024 → 2048 : 16 s ; 1024 → 4096 : 15 s |
| Prompts aléatoires (Qwen3-VL 4B, 4 prompts) | 9 | 23 à 27 s en anglais ; 30 et 44 s en français |
| Extraction de props : analyse locale (Qwen3-VL 4B, image) | 3 | 12 à 24 s |
| Extraction de props : une pièce (Krea 2 Identity Edit) | 9 | 110 à 132 s par pièce |
| TRELLIS.2 image → modèle texturé (humanoïde, standard) | 1 (utilisateur) | > 3 h 15 : décodage de forme 20 min, maillage intermédiaire 31,7 M faces, remaillage 2 h 12 (CPU) — déconseillé sur 8 Go |
| Pixal3D image → modèle texturé (PBR 2048) | 2 | 223 s (GPU libre), 297 k triangles ; 1723 s pendant qu'un autre ComfyUI calculait sur le même GPU |
| Réparation du maillage (Blender) | 1 | 13 s |
| Simplification à 40 k triangles | 1 | 12 s |
| Pré-calcul des textures (Cycles GPU) | 1 | 18 s |
| Squelette + poids MIA | 1 | 29 s, pic VRAM 3,7 Go (processus MIA) |
| Liaison Blender | 1 | 14 s |
| Validation par poses (GIF) | 1 | 8 s |
| Export .blend / .glb / .fbx + vérification par réouverture | 1 | 10 s |
| Vidéo MiniMax H3 image → vidéo, 5 s, 480×864, 24 i/s, audio | 1 | 306 s (dont démarrage de votre ComfyUI en profil basse mémoire) |
| Vidéo MiniMax H3 références → vidéo (2 sujets, Ref2VA), 5 s | 1 | 266 s (ComfyUI vidéo déjà lancé) |
| Vidéo MiniMax H3 texte → vidéo, 3 s | 1 | 162 s |
| Vidéo MiniMax H3 première + dernière image, 3 s | 1 | 141 s |
| Rig poids automatiques Blender (squelette MIA + liaison + validation + export) | 1 | 39 s |
| Rig articulé automatique + export | 1 | 12 s |

Mémoire : ComfyUI v0.38.0 utilise la VRAM dynamique ; sur 8 Go, Krea 2 (12,5 Go de poids) et Pixal3D tournent en
déchargeant une partie des poids en RAM. Pics d'occupation **de la carte entière** relevés par `nvidia-smi` toutes les
2 s pendant 1 h 30 de tests (ils incluent le bureau Windows et tout processus resté chargé, d'où des valeurs hautes pour
des étapes légères quand un ComfyUI garde ses modèles en mémoire) :

| Étape | Pic carte relevé |
|---|---|
| Krea 2 création / édition | 7,2 Go / 7,7 Go |
| Pixal3D | 8,0 Go (carte pleine) |
| Vidéo MiniMax H3 (votre ComfyUI, profil basse mémoire) | 7,6 Go |
| Pré-calcul Cycles GPU | 2,1 Go |
| Squelette MIA | 1,7 Go (pic du processus MIA mesuré par PyTorch : 3,7 Go — l'échantillonnage à 2 s peut manquer un pic bref) |
| Étapes Blender (réparation, liaison, validation, export) | < 0,8 Go |

Un seul processus lourd utilise le GPU à la fois : l'application libère l'autre ComfyUI (`/free`) avant de changer de
moteur. Un autre logiciel qui calcule en même temps sur le GPU ralentit fortement les étapes (constaté : ×10 sur Pixal3D).

Durées complémentaires : édition Krea 2 Identity Edit mesurée sur 29 exécutions au total, de 103 à 185 s (103–118 s sur
les 19 dernières, GPU libre).

Espace disque : modèles téléchargés ≈ 33,7 Gio, ComfyUI portable ≈ 1,9 Gio (archive) — voir TEST_REPORT.md pour l'occupation mesurée après installation. Les fichiers identiques déjà présents ailleurs (même taille et même SHA-256) sont réutilisés par lien physique, sans recopie.

## 7. Réseau et chemins

| Cas | Statut |
|---|---|
| Téléchargement interrompu puis repris (fichier `.part`, vérification taille + SHA-256) | **testé** (263 / 424 Mio repris) |
| Ressource Hugging Face protégée sans jeton | **testé** : refus explicite « Accès refusé (401) … ressource protégée », aucun fichier partiel laissé |
| Port déjà occupé | **testé** : port libre suivant choisi |
| Chemins avec espaces et accents | **testé** (`E:\Atelier 3D Données`, deuxième installation dans un dossier accentué) |
| Proxy d'entreprise (`HTTPS_PROXY`) | attendu : transmis à curl et à uv ; non testé derrière un vrai proxy |
| Hors ligne après installation | attendu pour image / 3D / rig / vidéo locale (tous les poids sont locaux) ; non testé câble débranché |
| OpenAI texte / vision (analyse des props, prompts aléatoires) | **intégré, testé avec une API simulée** (corps de requête Responses, image en data URL, JSON strict, refus) ; **appel réel non testé** (pas de clé) |
| Moteur OpenAI Images (facultatif, service externe) | **intégré, testé avec une API OpenAI simulée** (requêtes, multipart, erreurs, nouvelle tentative, file `net`, clé absente de la base) ; refus sans clé vérifié dans l'interface ; **appel réel non testé** (aucune clé disponible pendant le développement). Nécessite Internet ; Windows DPAPI pour la clé (profil utilisateur Windows courant : la clé n'est pas lisible depuis un autre compte ni une autre machine, à ressaisir après copie de l'installation) |
| Accès à distance par le VPN WireGuard de la Freebox | **testé sur le PC** (écoute réseau, refus d'une adresse du réseau local hors VPN, connexion par code, blocage après 5 essais, actions réservées au PC, en-têtes Host/Origin) ; **non testé depuis un vrai appareil connecté au VPN** ; la règle de pare-feu et la route (`ACCES_VPN.bat`) sont à créer par l'utilisateur |
| Droits administrateur | non requis (aucune élévation demandée pendant les tests) ; seul `ACCES_VPN.bat` (facultatif) en demande, pour le pare-feu et la route |
| Docker | non requis, non utilisé |
