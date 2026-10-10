# Atelier 3D

Studio local pour Windows 10/11 x64 : une interface française en **nodes reliées par des noodles** qui transforme une image en
modèle 3D texturé, préparé et — pour un personnage — **réellement riggé et skinné**, livré en `.blend` vérifié ; génération
d'images Krea 2 Turbo, édition d'image avec référence, flow foreach (concept → A-pose → parties) et branche vidéo
MiniMax H3 (workflows MiniMax H3 Ultra fournis, installés automatiquement, ou ComfyUI MiniMax déjà présent sur le PC). En option, génération et édition d'images par OpenAI (ChatGPT Images) avec
votre propre clé d'API. Claude Code peut piloter exactement les mêmes graphes par JSON, API et MCP.

---

## 1. Démarrage rapide

### 1.0 Prérequis (rien d'autre n'est à installer)
- Windows 10 ou 11, 64 bits ; une connexion Internet pendant l'installation ; de l'espace disque selon les composants cochés
  (≈ 50 Go sans la vidéo, ≈ 130 Go avec tout : la fenêtre de l'installateur affiche le total).
- Pour les moteurs IA (image, 3D, rig, upscale) : carte **NVIDIA RTX 20xx / GTX 16xx ou plus récente**, **8 Go de VRAM**
  conseillés (6 Go minimum), **pilote NVIDIA 580 ou plus récent** (les moteurs utilisent CUDA 13 ; mettez le pilote à jour
  avant l'installation). Sans carte compatible : interface, Blender et exports seulement (l'installateur le dit et ne coche
  pas les composants IA).
- Inutile d'installer Python, Git, Node.js, CUDA, ComfyUI ou Blender : l'installateur télécharge ses propres versions
  (Blender est réutilisé s'il est déjà installé en version 4.2 ou plus). Aucun droit administrateur.
- Vidéo MiniMax H3 : 8 Go de VRAM et 32 Go de RAM conseillés (configuration testée) ; l'encodeur texte seul pèse 25 Gio.

### 1.1 Extraire le ZIP
Extrayez `Atelier3D-1.0.0.zip` où vous voulez (les chemins avec espaces et accents sont pris en charge).

### 1.2 Lancer `INSTALL.bat`
Double-cliquez sur **`INSTALL.bat`**. L'installateur :

1. diagnostique Windows, GPU/VRAM, pilote, RAM, disque et les installations existantes (ComfyUI, Blender), **sans les modifier** ;
2. ouvre une fenêtre **« Atelier 3D — installation »** : dossier d'installation (le disque le plus libre est proposé) et
   **cases à cocher** par composant, avec la taille de chacun, le total à télécharger et l'espace libre. Les composants adaptés
   au PC sont pré-cochés ; une case décochée n'est ni téléchargée ni installée. Rien n'est écrit sur le disque avant
   « Installer » ; « Annuler » quitte sans rien modifier. (Sans bureau Windows : même choix dans un menu texte.)

   | Composant | Contenu | Téléchargement |
   |---|---|---|
   | Images : génération | Krea 2 Turbo | ≈ 20 Gio (dont le ComfyUI dédié, 1,9 Gio, compté une seule fois) |
   | Images : édition + détourage | Krea 2 Identity Edit, BiRefNet | + 2 Gio |
   | Upscale 2K / 4K | SeedVR2 3B, Real-ESRGAN | ≈ 5,6 Gio |
   | Image → 3D | Pixal3D (+ TRELLIS.2, MoGe) | ≈ 16 Gio |
   | Rigging humanoïde | Make-It-Animatable + environnement PyTorch | ≈ 5 Gio |
   | Vidéo MiniMax H3 (texte, image, début/fin) | workflows MiniMax H3 Ultra fournis, FL2VA int8, Qwen3-VL 32B int8, VAE, LoRA turbo, nodes rgthree + VideoHelperSuite | ≈ 53 Gio |
   | Vidéo : mode Références | Ref2VA int8 | + 19,5 Gio |
   | Vidéo : ComfyUI MiniMax existant | proposé seulement si un ComfyUI avec MiniMax H3 **et ses poids** est trouvé : aucun téléchargement | — |

3. vérifie l'espace libre pour la sélection ;
4. télécharge depuis les sources officielles (GitHub Releases, Hugging Face, download.blender.org) avec **reprise** et
   vérification **taille + SHA-256** ; les modèles déjà présents ailleurs avec une empreinte identique sont **partagés par lien
   physique** (aucune copie) ;
5. installe des runtimes isolés : Python portable (uv), ComfyUI portable dédié v0.38.0, environnement MIA, Blender (réutilisé
   s'il est ≥ 4.2) ;
6. vidéo installée : nodes rgthree et VideoHelperSuite (commits figés, fichiers vérifiés) et leurs dépendances (OpenCV,
   ffmpeg) dans le ComfyUI dédié, poids MiniMax H3 depuis Hugging Face ; ComfyUI MiniMax existant : **copie** de ses workflows ;
7. lance le diagnostic, démarre les services, attend un vrai healthcheck et ouvre `http://127.0.0.1:8765/`.

Relancer `INSTALL.bat` ne retélécharge rien : chaque étape est enregistrée dans `<installation>\config\install-manifest.json`.
Ajouter un composant à une installation existante (ex. la vidéo) : relancez `INSTALL.bat`, la fenêtre reprend les composants
déjà installés, cochez celui à ajouter ; seuls les fichiers manquants sont téléchargés. Sans fenêtre :
`INSTALL.bat -Components image,image_edit,mesh,upscale,rig_mia,video,video_ref` (`video_attach` pour un ComfyUI MiniMax
existant). Un composant décoché plus tard n'est pas désinstallé.
Aucun droit administrateur, aucune politique PowerShell globale modifiée, aucun Docker.

### 1.3 Première image
1. Dans la bibliothèque (à gauche), cliquez **« ＋ Prompt → concept → Blender »** ;
2. sélectionnez la node **Prompt image**, écrivez votre prompt (ex. *« Full body front view of a single character in A-pose,
   plain light grey background, no text »*) ;
3. cliquez **Exécuter** sur la node **Générer une image** : le concept apparaît dans la node et dans **Preview image**.

### 1.4 Récupérer le `.blend`
Cliquez **▶ Exécuter le graphe** : image → détourage → Pixal3D → préparation → rig MIA → tests de déformation → export.
Sur la node **Export Blender**, l'inspecteur liste les fichiers : **⇩** pour télécharger le `.blend` / `.glb` / `.fbx`,
**Blender** pour l'ouvrir dans votre Blender local, 📁 pour ouvrir le dossier.
Le statut final est l'un de : **« export Blender vérifié »**, **« rig à corriger »** ou **« échec »**, avec ses preuves
(`validation.json`). « Prêt pour production » n'est jamais affiché automatiquement.

---

## 2. Utilisation

| Fichier | Rôle |
|---|---|
| `INSTALL.bat` | installation / réparation (relançable) |
| `START.bat` | démarre (ou retrouve l'instance déjà lancée), choisit des ports libres, ouvre l'interface |
| `STOP.bat` | arrête uniquement les processus lancés par Atelier 3D (PID + heure de création + exécutable vérifiés) |
| `DIAGNOSE.bat` | GPU, CUDA réellement utilisée par chaque PyTorch, Blender, ports, étapes réalisables / à tester / indisponibles |
| `UPDATE.bat` | sauvegarde, installe les versions verrouillées de ce dossier, teste, **retour arrière** si échec (`UPDATE.bat -Rollback <id>`) |

**Interface.** Graphe central (zoom, pan, cadrage, mini-carte), bibliothèque de nodes et préréglages, inspecteur de la node
sélectionnée, panneau des tâches (origine *utilisateur* ou *Claude Code*, temps écoulé, étape, progression réelle quand le
moteur la fournit, sinon « traitement en cours »), compteurs VRAM/RAM, délai mesuré des événements. Groupes repliables
(macro « Image → personnage Blender »). Sur écran étroit, bibliothèque et inspecteur deviennent des tiroirs.

**Ajouter une node en tirant une noodle.** Lâchez une noodle dans le vide : un menu liste **toutes** les nodes, celles qu'on
peut brancher sur ce port en tête (avec le port utilisé), les autres ensuite (créées sans noodle). La recherche filtre à
chaque lettre (« img » ne garde que les nodes « image », « 3d » les nodes 3D, sans tenir compte des accents ; plusieurs
mots affinent). ↑/↓ puis Entrée (ou clic) : la node est créée à cet endroit et branchée. Tirer depuis une entrée propose
en tête les nodes qui produisent ce type. Double-clic sur le fond : même menu, sans branchement.

**Résultats successifs dans la node.** Une node affiche toujours son **dernier** résultat ; ‹ › parcourt les variantes.
« ✓ utilisée en aval » marque la sortie transmise aux nodes suivantes ; une régénération apparaît tout de suite comme
« candidate » avec un bouton **Utiliser** pour la transmettre (seuls les descendants concernés passent « À recalculer »).

**Médiathèque.** **🖼 Médiathèque** (barre du haut) recense toutes les images, vidéos et modèles 3D générés, tous graphes
confondus : onglets avec compteurs, recherche (prompt, node, graphe, item), filtre « ce graphe seulement », vignettes
(rendu pour la 3D, miniature pour la vidéo), mention « utilisée ». Clic : visionneuse (zoom, lecteur vidéo avec frames,
vue 3D) ; ↗ Node ouvre le graphe sur la node d'origine ; ⇩ télécharge ; 📁 ouvre le dossier.

**Graphe vierge.** **＋ Nouveau** (barre du haut) ou **＋ Graphe vierge** (bibliothèque) crée un graphe sans node ;
renommez-le dans l'inspecteur (cliquez sur le fond du canevas).

**Supprimer un graphe.** 🗑 à côté de la liste des graphes (graphe ouvert) ou sur chaque ligne de la section **Graphes**
de la bibliothèque, sans confirmation : le message qui suit propose **Annuler**, et le graphe va dans la **Corbeille**
(bibliothèque), d'où **Restaurer** le remet en place avec ses variantes ; ses images, vidéos et modèles 3D restent dans la médiathèque. Refusé tant qu'une de ses
étapes est en attente ou en cours.

**Importer depuis la médiathèque.** Les nodes **Importer une image** et **Importer un maillage** ont deux boutons :
**Fichier…** (disque) et **🖼 Médiathèque** (également dans l'inspecteur). La médiathèque s'ouvre alors en mode « choisir »,
limitée au bon type : un clic sur un résultat l'importe (copie de l'original ; provenance — graphe, node, variante —
affichée dans l'inspecteur).

**Sélection, copier, couper, coller.** La barre d'outils en haut à gauche du canevas bascule entre **✋ Déplacer** (glisser
déplace la vue) et **⬚ Sélection** (glisser trace un rectangle ; clic molette ou droit pour déplacer la vue).

| Raccourci | Action |
|---|---|
| `V` / `H` | mode sélection / mode déplacement |
| `Maj` + glisser | rectangle de sélection dans les deux modes |
| `Ctrl` + clic | ajouter / retirer une node de la sélection |
| `Ctrl+A` · `Échap` | tout sélectionner · tout désélectionner |
| `Ctrl+C` · `Ctrl+X` · `Ctrl+V` | copier · couper · coller (à la position de la souris) |
| `Ctrl+Maj+V` | coller en gardant aussi les connexions entrantes depuis le graphe |
| `Ctrl+D` · `Suppr` | dupliquer · supprimer |
| clic sur une noodle, puis `Suppr` | supprimer cette noodle (surlignée en blanc ; `Ctrl`+clic pour en prendre plusieurs) |
| `Ctrl+Z` · `Ctrl+Y` | annuler · rétablir (copier/couper/coller compris) |

Le presse-papiers système transporte un JSON autonome : on peut coller dans un autre graphe, un autre onglet, et même
coller un `graph.json` exporté. Les noodles entre nodes copiées suivent ; seules les définitions sont copiées (types,
réglages, prompts) — les résultats et variantes restent sur les nodes d'origine. Les previews automatiques d'un générateur
foreach ne sont pas copiées : elles sont recréées pour chaque item de la copie.

**Régénération sans tout relancer.** Chaque node garde ses variantes (immuables). « Régénérer » crée une **candidate** qui
ne remplace pas la sortie utilisée en aval tant que vous ne cliquez pas « Utiliser ». Les liens peuvent suivre la sortie
active ou être **épinglés** sur une variante. Seuls les descendants réellement concernés passent « À recalculer ».
Portées : cette node · cette node et ses descendants · cette branche · jusqu'ici · cette sous-étape · cet item.

**Flow foreach (concept → A-pose → parties).** Préréglage **« PNJ · concept, A-pose et parties (foreach) »** ou
`examples/pnj_concept_apose_parts.graph.json`. Le générateur « Parties » reçoit l'A-pose sur son port **Image** et cinq
prompts sur son port **Prompts** : « 5 prompts × 1 référence commune → 5 résultats », cinq nodes **Preview image**
créées automatiquement, chacune avec **« Régénérer ce résultat »** et une sortie Image connectable.
Conseil : demandez un concept **à un seul personnage, de face, sans texte**. Le prompt de l'exemple du cahier des charges
(« Concept art d'un PNJ original… ») donne souvent une planche à trois vues légendée avec Krea 2, et l'édition A-pose
garde alors les trois vues. Le préréglage utilise une formulation directive ; choisissez le concept qui vous convient
(nouvelle seed) avant de lancer les parties.

**Générer une image / Éditer une image.** Deux nodes distinctes : **Générer une image** part d'un prompt seul ;
**Éditer une image** reçoit une image sur son port **Image** et une consigne (« mets-le en A-pose », « change la tenue »…).
Les deux acceptent un prompt unique ou plusieurs prompts (« pour chaque prompt »). Vos anciens graphes sont convertis
automatiquement, sans perdre ni recalculer leurs résultats.

**Préparer l'image : pose A / T et fond.** La node **Préparer l'image** propose :
- **Pose** : *inchangée*, *A-pose* (bras à 45°) ou *T-pose* (bras à l'horizontale). Le personnage est redessiné dans cette
  pose par Krea 2 (local) ou OpenAI (au choix), avec un champ **Précisions pour la pose** (« mains ouvertes »…). C'est une
  nouvelle image produite par l'IA : vérifiez mains, visage et accessoires ; **Régénérer** propose une autre tentative.
- **Retirer le fond** (activé par défaut) : décochez pour garder le décor (pas de recadrage dans ce cas).
- **Recadrer sur le sujet** et **Marge**, comme avant. Vos anciennes nodes gardent exactement leur comportement et leurs résultats.

**Upscale image (2K / 4K).** Node **Upscale image** : **Taille cible** 2K (2048 px) ou 4K (4096 px) sur le côté le plus long,
**Moteur** *Détaillé — SeedVR2 3B* (IA locale qui reconstruit des détails nets, fidèle à l'original) ou *Rapide —
Real-ESRGAN x4* (quelques secondes, rendu plus lisse). La transparence d'une image détourée est conservée. Le résultat est
accompagné d'une comparaison et d'un zoom 1:1 (source agrandie sans IA ↔ résultat).

**Prompts aléatoires.** Node **Prompts aléatoires** (catégorie Texte, Qwen3-VL local ou OpenAI au choix) : écrivez un **Contexte** (« village sur une planète
désertique, Star Wars »), choisissez le **Nombre de prompts** (1 à 16), la langue et le type de sujet ; Qwen3-VL 4B écrit les
prompts en local (≈ 25 s pour 4) et les affiche dans la node. Reliez sa sortie **Prompts (liste)** à l'entrée **Prompts**
d'une node « Générer une image » en mode *pour chaque prompt* : une image par prompt, chacune avec sa preview. **Régénérer**
propose une nouvelle liste (candidate) ; **Utiliser** ne fait recalculer que les images dont le prompt a changé. Les sorties
**Prompt 1…N** donnent un prompt à la fois. Préréglage : **« Idées · prompts aléatoires → images (foreach) »**.

**Avant / après.** Node **Avant / après** (catégorie Comparer) : branchez deux résultats sur **Avant** et **Après** — images,
vidéos, modèles 3D ou maillages riggés, même de natures différentes. Trois affichages : **Curseur** (glisser pour révéler),
**Côte à côte**, **Différence** (ce qui change ressort, le reste devient noir). Les vidéos se lisent ensemble (**▶ Lire
ensemble**, synchronisées) ; les modèles 3D sont comparés par leurs rendus, et **3D côte à côte** ouvre les deux modèles avec
des caméras liées. **Agrandir** ouvre la comparaison en grand. Aucun calcul. Pour comparer des résultats d'autres graphes :
nodes **Importer une image / un maillage / une vidéo** → **🖼 Médiathèque** (le rendu du résultat d'origine est repris).

**Extraction de props.** Branchez une image de personnage sur une node **Extraction de props** (catégorie Image) :
- **Inclure** *Props seuls* ou *Props + corps* (ajoute la tête et le corps « nu », en combinaison neutre), **Détail** *Minimal
  (3–6)*, *Standard (6–15)* ou *Détaillé (15+)*, **Fusionner les pièces symétriques** (une seule botte, un seul gant).
- **Analyser** envoie l'image à ChatGPT (par défaut ; service externe, clé dans ⚙ Réglages) ou à Qwen3-VL 4B en local
  (« Analyse par » dans l'inspecteur) : la node affiche la description du personnage et la liste des pièces.
- Pour chaque pièce : interrupteur (l'extraire ou non), nom modifiable, ↻ (nouvelle tentative pour cette pièce), 🗑 ;
  **Tout désélectionner**, **+ Ajouter une pièce** pour ce que l'analyse a oublié.
- La node crée un groupe **Props** : une node « Éditer une image » (une image isolée par pièce cochée, avec sa preview).
  **Extraire les props** lance l'extraction ; si l'analyse n'est pas encore faite, elle passe d'abord et l'extraction suit
  automatiquement. Le moteur des images extraites se règle sur la node « Props » (OpenAI ou Krea 2 local).
- Une pièce extraite est une image produite par l'IA à partir du personnage : formes cachées inventées, à vérifier.

**OpenAI (ChatGPT Images), facultatif.** Chaque node image a un réglage **Moteur** : *Krea 2 (local, sur ce PC)* — par
défaut — ou *OpenAI / ChatGPT Images (service externe)*.
1. **⚙ Réglages** (barre du haut) → collez votre clé d'API OpenAI (champ masqué) → **Enregistrer et tester**. Le test liste
   les modèles GPT Image accessibles à votre compte ; choisissez les modèles, la qualité et la modération par défaut.
2. Dans l'inspecteur d'une node image, réglez **Moteur** sur OpenAI (pastille « ☁ OpenAI » sur la node) ; modèle, qualité,
   fond (transparent si le modèle l'accepte) et taille sont réglables par node. En édition, une **seconde référence** (`image_b`) devient
   disponible.
3. **Exécuter** comme d'habitude. Ces étapes passent par une file réseau séparée : elles ne bloquent pas le GPU.

Confidentialité et coût : le prompt — et, pour l'édition, les images — sont envoyés à OpenAI uniquement pour les nodes
réglées sur OpenAI ; tout le reste reste local. Les appels sont facturés sur votre compte OpenAI. La clé est chiffrée sur ce
PC pour votre compte Windows (DPAPI), n'est jamais réaffichée (seuls ses 4 derniers caractères), ni écrite dans les
journaux ; **Supprimer la clé** l'efface. Sans clé, « Exécuter » est refusé avec un bouton **⚙ Réglages**.

**Vidéo.** Node **« Générer vidéo · MiniMax H3 Ultra »** : votre ComfyUI MiniMax (poids locaux) est rattaché tel quel ;
s'il n'est pas lancé, Atelier 3D peut le lancer avec son propre Python (processus arrêté par `STOP.bat`). Les travaux
lancés hors d'Atelier 3D ne sont jamais interrompus. Voir `WORKFLOWS.md`.

**Accès à distance (VPN WireGuard de la Freebox).** Utiliser Atelier 3D depuis un téléphone ou un portable hors de chez vous :
1. Freebox (application Free → Server → Paramètres réseau avancés → Serveur VPN) : activez **WireGuard**, ajoutez l'appareil,
   importez sa configuration dans l'application WireGuard ; réservez l'adresse IP du PC (bail DHCP statique).
2. Sur le PC : **⚙ Réglages → Accès à distance** → *Autoriser l'accès depuis le VPN*, puis `STOP.bat` et `START.bat`
   (si Windows demande l'autorisation réseau pour Python : « Autoriser »).
3. Lancez **`ACCES_VPN.bat`** (une autorisation administrateur) : règle de pare-feu limitée au réseau du VPN Freebox
   (192.168.27.64/27) et au port d'Atelier 3D, et route de retour par la Freebox.
4. Si un autre VPN tourne sur le PC (ProtonVPN…) : excluez 192.168.27.64/27 dans son *split tunneling* (indispensable avec un
   kill switch), sinon les réponses partent dans ce VPN.
5. Sur l'appareil connecté au VPN : ouvrez l'adresse indiquée dans les Réglages (ex. `http://192.168.1.20:8765/`) et saisissez
   le **code d'accès** affiché sur le PC (une fois par appareil).

Sécurité : seules les adresses du VPN sont servies (les autres appareils du réseau local sont refusés), code obligatoire
(5 essais ratés → 10 minutes de blocage, **Nouveau code** déconnecte tous les appareils), trafic chiffré par WireGuard. Depuis
le VPN, ouvrir un dossier ou Blender sur le PC et modifier ces réglages restent impossibles. Le panneau vérifie l'écoute, la
règle de pare-feu et la route. Tout retirer : désactivez dans les Réglages puis `ACCES_VPN.bat -Remove`.

---

## 3. Dépannage

| Symptôme | Action |
|---|---|
| Avertissement de sécurité Windows à l'ouverture d'un `.bat` | ZIP téléchargé : clic droit sur le ZIP → Propriétés → cocher « Débloquer », puis extraire de nouveau |
| Un moteur apparaît « indisponible » | `DIAGNOSE.bat` donne la raison exacte (modèle manquant, workflow non validé, VRAM…) ; relancez `INSTALL.bat` |
| Téléchargement interrompu | relancez `INSTALL.bat` : la partie reçue est reprise et vérifiée par SHA-256 |
| Ressource protégée (HTTP 401/403) | l'installateur demande alors un jeton Hugging Face (saisie masquée, jamais journalisé). Aucun modèle de la configuration actuelle n'en exige |
| Port occupé | `START.bat` choisit automatiquement le port libre suivant |
| Mémoire GPU insuffisante | l'étape libère la VRAM et refait **une** tentative avec un profil réduit (journalisé) ; sinon elle échoue avec une action corrective |
| Proxy d'entreprise | les variables `HTTPS_PROXY` ou le proxy système Windows sont utilisés, validation TLS conservée (non testé derrière un vrai proxy) |
| Libérer de l'espace | `<installation>\downloads` garde l'archive ComfyUI (1,9 Go) pour une réparation sans retéléchargement : supprimable |
| OpenAI : « clé refusée », « vérification d'organisation », « refusé par la modération », « quota » | le message de la tâche indique la cause ; vérifiez la clé et la facturation sur platform.openai.com, reformulez la consigne en cas de modération. Une limite de débit est réessayée une fois automatiquement |
| Accès à distance : l'appareil connecté au VPN n'obtient aucune réponse | ⚙ Réglages → Accès à distance → « Revérifier » : règle de pare-feu absente → `ACCES_VPN.bat` ; « Retour vers le VPN ✗ » → un autre VPN (ProtonVPN…) capte le trafic : exclure 192.168.27.64/27 ; vérifier l'adresse du PC |
| Accès à distance : « Adresse … non autorisée » | l'appareil n'est pas sur le réseau du VPN Freebox (ou la plage « Address » de sa configuration diffère : l'ajouter dans « Réseaux autorisés ») |
| Une étape reste « en attente » | la node indique ce qu'elle attend (« en attente du GPU : … depuis … ») : une seule étape GPU à la fois ; TRELLIS.2 peut occuper le GPU plus de 3 h sur 8 Go (Pixal3D ≈ 4 min) — annulez-la dans le panneau des tâches si besoin |
| Journaux | `<installation>\logs\` (install, server, worker, comfyui) et, par tâche, `data\jobs\<job>\logs\technique.log` |

---

## 4. Connexion de Claude Code

L'installateur écrit `<installation>\config\claude_code.mcp.json` (format `.mcp.json` de Claude Code) et la commande
équivalente dans `<installation>\config\claude_mcp_add.txt`, par exemple :

```bash
claude mcp add atelier3d --scope user -e ATELIER3D_HOME="D:\Atelier3D" -e PYTHONUTF8=1 -- "D:\Atelier3D\runtime\venv-app\Scripts\python.exe" "D:\Atelier3D-1.0.0\mcp\atelier3d_mcp.py"
```

Le serveur MCP (stdio, SDK `mcp` 2.3.0 verrouillé) expose 30 outils (`get_capabilities`, `create_graph`, `plan_graph`,
`run_graph`, `list_node_items`, `rerun_item`, `select_item_variant`, `list_media`, `get_preview`, `download_artifact`, …).
Procédure, exemples et règles d'itération : **`CLAUDE.md`**. API HTTP : `http://127.0.0.1:8765/docs` (OpenAPI), jeton dans
`<installation>\config\api_token`. Client Python et CLI : `clients/python` (`python -m atelier3d_client …`).
Utilisation sous WSL : non nécessaire (tout est natif Windows) ; si Claude Code tourne dans WSL, utilisez
`http://127.0.0.1:8765` via l'API (le serveur MCP doit être lancé côté Windows avec des chemins Windows).

---

## 5. Documents

- `CLAUDE.md` — pilotage par Claude Code (procédures, catégories, paramètres, conditions d'acceptation, reprise)
- `WORKFLOWS.md` — Krea 2 (création/édition), OpenAI Images (facultatif), MiniMax, foreach, previews automatiques, régénération par item, cache, variantes, références
- `RESEARCH.md` — sources vérifiées, licences, choix des moteurs
- `COMPATIBILITY.md` — matrice matériel / versions / statut réellement testé
- `TEST_REPORT.md` — ce qui a été exécuté réellement, mesures, et ce qui reste à vérifier
- `app/schemas/graph-1.1.schema.json` — JSON Schema du graphe ; `examples/` — graphes et scripts exécutables
