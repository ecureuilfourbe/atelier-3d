# kimodo.cpp — binaires Windows fournis avec Atelier 3D

Moteur de la node « Animer le modèle 3D » quand le mouvement vient d'un **prompt** : `kmd-generate.exe` génère un mouvement
humain (squelette SOMA-30, 30 images/s) à partir d'un texte, sur la carte graphique (Vulkan).

| | |
|---|---|
| Source | <https://github.com/localai-org/kimodo.cpp> (Apache-2.0), commit `5679ff19ba0a522c0b0516e9a9d402fe1af2c027` |
| Compilation | Windows x64, MSVC (Release), options : `KIMODO_ENABLE_VULKAN=ON`, `GGML_VULKAN=ON`, `KIMODO_BUILD_TESTS=OFF` |
| Bibliothèques | ggml (MIT, `LICENSE-ggml.txt`) ; runtime Visual C++ 2015-2022 (`msvcp140.dll`, `vcruntime140*.dll`, fichiers redistribuables Microsoft, déploiement local) |
| Prérequis machine | Windows 10/11 64 bits ; pilote graphique avec Vulkan (`vulkan-1.dll`, fourni par tout pilote NVIDIA récent) |
| Modèles | téléchargés par l'installateur (composant « Animation : prompt → mouvement ») depuis Hugging Face : `LocalAI-io/Kimodo-SOMA-RP-v1.1-GGML` (NVIDIA Open Model License) et `LocalAI-io/Llama-3-Kimodo-GGML` (Q8_0, Meta Llama 3 Community License) |

Licences : `LICENSE-kimodo.cpp.txt`, `NOTICE-kimodo.cpp.txt`, `LICENSE-ggml.txt`.

Utilisation (faite par Atelier 3D, `app/backend/atelier3d/motion.py`) :

```
kmd-generate.exe MOTION.gguf TEXT_BUNDLE.gguf PROMPT.txt FRAMES STEPS SEED OUTPUT_DIR
```

`tokenizer.gguf` doit se trouver dans le même dossier que `TEXT_BUNDLE.gguf`. Variables : `KIMODO_BACKEND=vulkan`,
`KIMODO_TEXT_LAYER_CHUNK=8` (encodeur texte par blocs de 8 couches : tient dans 8 Go de VRAM).
