# Atelier 3D — installateur principal (appelé par INSTALL.bat)
# Relançable : chaque étape est enregistrée dans <home>\config\install-manifest.json.
param(
    [string]$InstallDir = '',
    [string]$Components = '',          # sans fenêtre de choix, ex. "image,image_edit,mesh,upscale,rig_mia,video,video_ref"
    [string[]]$ShareModelsFrom = @(),  # dossiers "models" supplémentaires à examiner pour le partage
    [switch]$NonInteractive,
    [switch]$SkipStart,
    [switch]$SkipComfyScan
)

$DistDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\detect.ps1')
. (Join-Path $PSScriptRoot 'lib\menu.ps1')

$Lock = Read-JsonFile (Join-Path $PSScriptRoot 'versions.lock.json')
$LocationFile = Join-Path $DistDir 'install-location.json'
$UserLocationFile = Join-Path $env:LOCALAPPDATA 'Atelier3D\install-location.json'

function Ask([string]$Question, [string]$Default) {
    if ($NonInteractive) { return $Default }
    $a = Read-Host ("{0} [{1}]" -f $Question, $Default)
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    return $a.Trim()
}
function AskYesNo([string]$Question, [bool]$Default = $true) {
    if ($NonInteractive) { return $Default }
    $d = if ($Default) { 'O/n' } else { 'o/N' }
    $a = Read-Host ("{0} [{1}]" -f $Question, $d)
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    return ($a.Trim().ToLower() -in @('o', 'oui', 'y', 'yes'))
}

Write-Host ''
Write-Host '  ============================================' -ForegroundColor Cyan
Write-Host '   Atelier 3D - installation' -ForegroundColor Cyan
Write-Host '  ============================================' -ForegroundColor Cyan
Write-Host ("  Dossier du logiciel : {0}" -f $DistDir)

# ------------------------------------------------------------ 1. dossier d'installation (proposé ici, confirmé avec les composants)
# Rien n'est créé sur le disque avant la validation du choix des composants.
$previous = Read-JsonFile $LocationFile
if (-not $previous) { $previous = Read-JsonFile $UserLocationFile }
$dirLocked = [bool]$InstallDir
if (-not $InstallDir -and $previous -and $previous.home) {
    $InstallDir = $previous.home
    $dirLocked = $true
    Write-Host ("  Installation existante détectée : {0}" -f $InstallDir)
}
if (-not $InstallDir) {
    $best = $null
    foreach ($d in [System.IO.DriveInfo]::GetDrives()) {
        if ($d.DriveType -eq 'Fixed' -and $d.IsReady -and ($null -eq $best -or $d.AvailableFreeSpace -gt $best.AvailableFreeSpace)) { $best = $d }
    }
    $sysDrive = New-Object System.IO.DriveInfo($env:SystemDrive)
    $InstallDir = if ($sysDrive.AvailableFreeSpace -gt 200GB) { Join-Path $env:LOCALAPPDATA 'Atelier3D' } else { Join-Path $best.RootDirectory.FullName 'Atelier3D' }
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

# ------------------------------------------------------------ 2. diagnostic matériel
Write-Step 'Diagnostic du matériel'
$sys = Get-A3DSystemInfo
$gpus = Get-A3DGpuInfo
$ramGb = [math]::Round($sys.ram_total_mb / 1024, 1)
Write-Info ("{0} ({1}), {2}, RAM {3}" -f $sys.os, $sys.os_version, $sys.cpu, (Format-Bytes ($sys.ram_total_mb * 1MB)))
$gpuBlock = $null
$warnings = @()
$nvidia = @($gpus | Where-Object { $_.vendor -eq 'NVIDIA' -and $_.vram_total_mb })
if ($nvidia.Count -gt 0) {
    $g = $nvidia[0]
    Write-Info ("GPU : {0}, VRAM {1} (libre {2}), pilote {3}, CUDA max pilote {4}" -f $g.name, (Format-Bytes ($g.vram_total_mb * 1MB)), (Format-Bytes ($g.vram_free_mb * 1MB)), $g.driver, $g['driver_cuda_max'])
    Write-Info 'La version CUDA réellement utilisée sera vérifiée avec chaque runtime PyTorch installé.'
    # PyTorch du ComfyUI dédié = CUDA 13.0 : pilote 580 ou plus récent, cartes Turing (RTX 20xx, GTX 16xx) ou plus récentes
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $cudaMax = 0.0; [void][double]::TryParse("$($g['driver_cuda_max'])", [Globalization.NumberStyles]::Float, $inv, [ref]$cudaMax)
    $cc = 0.0; [void][double]::TryParse("$($g['compute_cap'])", [Globalization.NumberStyles]::Float, $inv, [ref]$cc)
    if ($cc -gt 0 -and $cc -lt 7.5) {
        $gpuBlock = ("Carte {0} trop ancienne (génération {1}, avant Turing) : non prise en charge par CUDA 13, utilisé par les moteurs IA. " -f $g.name, $cc) +
                    'Il faut une NVIDIA RTX 20xx / GTX 16xx ou plus récente (8 Go de VRAM conseillés).'
    } elseif ($cudaMax -gt 0 -and $cudaMax -lt 13.0) {
        $gpuBlock = ("Pilote NVIDIA {0} trop ancien (CUDA {1} au maximum) : les moteurs IA exigent CUDA 13, donc le pilote 580 ou plus récent. " -f $g.driver, $cudaMax) +
                    'Mettez-le à jour (application NVIDIA ou nvidia.com/drivers), redémarrez, puis relancez INSTALL.bat.'
    }
    if ($gpuBlock) { Write-Warn $gpuBlock; $warnings += $gpuBlock }
    $hwText = 'Ce PC : {0}, {1} de VRAM, pilote {2} · {3} de RAM · {4}' -f $g.name, (Format-A3DSize ($g.vram_total_mb * 1MB)), $g.driver, (Format-A3DSize ($sys.ram_total_mb * 1MB)), $sys.os
} else {
    $msg = 'Aucune carte NVIDIA détectée : les moteurs IA locaux ne pourront pas fonctionner (interface, Blender, exports et moteur OpenAI facultatif restent disponibles).'
    Write-Warn $msg
    $warnings += $msg
    $hwText = 'Ce PC : aucune carte NVIDIA · {0} de RAM · {1}' -f (Format-A3DSize ($sys.ram_total_mb * 1MB)), $sys.os
}
$vramGb = if ($nvidia.Count -gt 0) { [math]::Round($nvidia[0].vram_total_mb / 1024, 1) } else { 0 }

# ------------------------------------------------------------ 3. installations existantes
$RuntimeReal = Join-Path $InstallDir 'runtime'
$runtimeAlias = Join-Path ([System.IO.Path]::GetPathRoot($InstallDir)) 'Atelier3D_runtime'
$comfyInstalls = @()
if (-not $SkipComfyScan) {
    Write-Step 'Recherche des installations ComfyUI existantes (lecture seule, 60 s max)'
    $found = Find-A3DComfyInstalls -BudgetSeconds 60
    foreach ($c in $found) {
        $real = $c.comfy_dir
        try { $real = (Get-Item -LiteralPath $c.comfy_dir).FullName } catch { }
        if ($real.StartsWith($RuntimeReal, [StringComparison]::OrdinalIgnoreCase) -or $real.StartsWith($runtimeAlias, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $comfyInstalls += $c
    }
    foreach ($c in $comfyInstalls) { Write-Info ("ComfyUI {0} : {1}{2}" -f $c.version, $c.comfy_dir, $(if ((Get-A3DProp $c 'has_minimax_h3') -and @(Get-A3DProp $c 'minimax_h3_models' | Where-Object { $_ }).Count) { '  [MiniMax H3 + poids]' } else { '' })) }
    if ($comfyInstalls.Count -eq 0) { Write-Info 'Aucune installation trouvée.' }
}
$prevManifest = Read-JsonFile (Join-Path $InstallDir 'config\install-manifest.json')
$prevExisting = Get-A3DProp (Get-A3DProp $prevManifest 'components') 'existing_comfyui'
if ($SkipComfyScan -and $prevExisting) {
    # pas de nouvelle recherche : on garde la détection précédente (sinon l'accès MiniMax déjà détecté serait oublié)
    $comfyInstalls = @($prevExisting | Where-Object { $_ })
    Write-Info ("Recherche ComfyUI sautée : {0} installation(s) de la détection précédente conservée(s)" -f $comfyInstalls.Count)
}
$shareDirs = @($comfyInstalls | ForEach-Object { $_.models_dir }) + $ShareModelsFrom
$minimaxInstalls = @($comfyInstalls | Where-Object { (Get-A3DProp $_ 'has_minimax_h3') -and @(Get-A3DProp $_ 'minimax_h3_models' | Where-Object { $_ }).Count -gt 0 })

$blenders = Get-A3DBlenderCandidates
foreach ($b in $blenders) { Write-Info ("Blender {0} : {1}" -f $b.version, $b.path) }
$minVer = [version]$Lock.blender.minimum_version
$blenderOk = @($blenders | Where-Object { try { [version]$_.version -ge $minVer } catch { $false } }).Count -gt 0

# ------------------------------------------------------------ 4. choix des composants (fenêtre à cases à cocher)
$profileMap = @{ image = @('image'); image_edit = @('image_edit'); mesh = @('mesh'); upscale = @('upscale'); video = @('video'); video_ref = @('video_ref') }
$comfyComps = @('image', 'image_edit', 'mesh', 'upscale', 'video', 'video_ref')
$videoComps = @('video', 'video_ref')
$gpuNote = if ($gpuBlock) { 'Carte ou pilote non compatible (voir plus haut) : ne fonctionnera pas tant que ce point n''est pas réglé.' } elseif ($nvidia.Count -eq 0) { 'Nécessite une carte NVIDIA.' } else { $null }
$videoNote = $gpuNote
if (-not $videoNote -and ($vramGb -lt 7.5 -or $ramGb -lt 30)) {
    $videoNote = 'Testé avec 8 Go de VRAM et 32 Go de RAM ; ce PC : {0} Go de VRAM, {1} Go de RAM : manque de mémoire probable.' -f $vramGb, $ramGb
}
$catalog = @(
    [ordered]@{ key = 'image'; title = 'Images : génération (Krea 2 Turbo)'; note = $gpuNote
        detail = 'Créer une image à partir d''un texte, en local (≈ 40 s par image sur 8 Go). Nécessaire à l''édition d''image.' }
    [ordered]@{ key = 'image_edit'; title = 'Images : édition avec référence + détourage'; requires = @('image'); note = $gpuNote
        detail = 'Krea 2 Identity Edit (modifier une image en gardant le personnage, poses A/T, extraction de props) et BiRefNet (retirer le fond).' }
    [ordered]@{ key = 'upscale'; title = 'Upscale 2K / 4K'; note = $gpuNote
        detail = 'SeedVR2 3B (ajoute des détails) et Real-ESRGAN (rapide) ; la transparence est conservée.' }
    [ordered]@{ key = 'mesh'; title = 'Image → modèle 3D texturé'; note = $(if ($gpuNote) { $gpuNote } elseif ($vramGb -lt 6) { 'Moins de 6 Go de VRAM : en dessous du minimum documenté.' } else { $null })
        detail = 'Pixal3D (≈ 4 min sur 8 Go de VRAM) : modèle .glb avec textures couleur, metallic, roughness, normales.' }
    [ordered]@{ key = 'rig_mia'; title = 'Rigging humanoïde (squelette + poids)'; note = $gpuNote
        detail = 'Make-It-Animatable (squelette Mixamo 52 os) puis assemblage Blender. Environnement Python séparé.' }
    [ordered]@{ key = 'video'; title = 'Vidéo MiniMax H3 : texte, image, début/fin → vidéo'; excludes = @('video_attach'); note = $videoNote
        detail = 'Workflows MiniMax H3 Ultra fournis (LoRA turbo, son inclus), exécutés par le ComfyUI d''Atelier 3D : modèle FL2VA int8, encodeur texte Qwen3-VL 32B int8, VAE vidéo et audio, nodes rgthree et VideoHelperSuite. Licences : MiniMax H3 Community License (poids), Apache-2.0 (LoRA turbo).' }
    [ordered]@{ key = 'video_ref'; title = 'Vidéo MiniMax H3 : mode Références → vidéo'; requires = @('video'); excludes = @('video_attach'); note = $videoNote
        detail = 'Modèle Ref2VA int8 en plus : vidéo guidée par 1 ou 2 images de référence (personnage, décor…).' }
)
if ($minimaxInstalls.Count -gt 0) {
    $catalog += [ordered]@{ key = 'video_attach'; title = 'Vidéo : utiliser le ComfyUI MiniMax H3 déjà présent sur ce PC'; excludes = $videoComps
        detail = ('Aucun téléchargement : Atelier 3D lance ou rejoint {0} sans le modifier.' -f $minimaxInstalls[0].comfy_dir) }
}

# présélection : composants de l'installation existante, sinon ceux adaptés à ce PC
$prevCfg = Read-JsonFile (Join-Path $InstallDir 'config\atelier3d.json')
$recommended = @()
if (Get-A3DProp $prevCfg 'components') {
    $recommended = @((Get-A3DProp $prevCfg 'components') | Where-Object { $k = $_; @($catalog | Where-Object { $_.key -eq $k }).Count })
} else {
    if (-not $gpuBlock -and $vramGb -ge 6) { $recommended += @('image', 'image_edit', 'mesh', 'rig_mia', 'upscale') }
    if ($minimaxInstalls.Count -gt 0) { $recommended += 'video_attach' }
    elseif (-not $gpuBlock -and $vramGb -ge 7.5 -and $ramGb -ge 30) { $recommended += $videoComps }
}

# tailles (téléchargement, disque) par élément ; un élément commun à plusieurs composants n'est compté qu'une fois
$sizeOf = @{}
$compItems = @{ '_base' = @('rt:app', 'rt:blender') }
foreach ($c in $catalog) { $compItems[$c.key] = @() }
$sizeOf['rt:app'] = if (Test-Path -LiteralPath (Join-Path $RuntimeReal 'venv-app\Scripts\python.exe')) { @(0, 0) } else { @(200MB, 700MB) }
$sizeOf['rt:blender'] = if ($blenderOk -or (Test-Path -LiteralPath (Join-Path $RuntimeReal 'blender'))) { @(0, 0) } else { @([double]$Lock.blender.download.size, [double]$Lock.blender.download.size * 3) }
$sizeOf['rt:comfyui'] = if (Test-Path -LiteralPath (Join-Path $RuntimeReal 'comfyui\python_embeded\python.exe')) { @(0, 0) } else { @([double]$Lock.comfyui.portable.size, [double]$Lock.comfyui.portable.size * 4) }
$wheelBytes = 0.0; foreach ($w in @((Get-A3DProp $Lock.comfyui 'python_packages').video)) { $wheelBytes += [double]$w.size }
$sizeOf['rt:video_nodes'] = if (Test-Path -LiteralPath (Join-Path $RuntimeReal 'comfyui\ComfyUI\custom_nodes\ComfyUI-VideoHelperSuite\__init__.py')) { @(0, 0) } else { @(($wheelBytes + 5MB), 300MB) }
$miaDl = 0.0; foreach ($w in $Lock.engines.mia.weights.PSObject.Properties) { if (-not (Test-Path -LiteralPath (Join-Path $InstallDir ('models\mia\' + $w.Name)))) { $miaDl += [double]$w.Value.size } }
$sizeOf['rt:mia'] = if (Test-Path -LiteralPath (Join-Path $RuntimeReal 'venv-mia\Scripts\python.exe')) { @($miaDl, $miaDl) } else { @(($miaDl + 3GB), ($miaDl + 6GB)) }
$installRoot = [IO.Path]::GetPathRoot($InstallDir)
foreach ($p in $Lock.models.PSObject.Properties) {
    $e = $p.Value
    $id = 'model:' + $p.Name
    $dest = Join-Path (Join-Path (Join-Path $InstallDir 'models') $e.comfy_folder) $e.filename
    $sizeOf[$id] = @([double]$e.size, [double]$e.size)
    if (Test-Path -LiteralPath $dest) { $sizeOf[$id] = @(0, 0) }
    else {
        foreach ($md in $shareDirs) {  # estimation rapide (taille identique) ; l'empreinte SHA-256 est vérifiée avant tout partage
            $cand = Join-Path (Join-Path $md $e.comfy_folder) $e.filename
            if ((Test-Path -LiteralPath $cand) -and (Get-Item -LiteralPath $cand).Length -eq [long]$e.size) {
                $sizeOf[$id] = @(0, $(if ([IO.Path]::GetPathRoot($cand) -eq $installRoot) { 0 } else { [double]$e.size }))
                break
            }
        }
    }
    foreach ($comp in $profileMap.Keys) { if (@($e.profiles) | Where-Object { $profileMap[$comp] -contains $_ }) { $compItems[$comp] += $id } }
}
foreach ($comp in $comfyComps) { $compItems[$comp] += 'rt:comfyui' }
foreach ($comp in $videoComps) { $compItems[$comp] += 'rt:video_nodes' }
$compItems['rig_mia'] += 'rt:mia'
$Measure = {
    param([string[]]$Keys)
    $ids = @{}
    foreach ($k in @($Keys)) { foreach ($id in @($compItems[$k])) { if ($id) { $ids[$id] = $true } } }
    $dl = 0.0; $disk = 0.0
    foreach ($id in $ids.Keys) { $dl += $sizeOf[$id][0]; $disk += $sizeOf[$id][1] }
    return @{ download = $dl; disk = $disk }
}

if ($Components) {
    $selected = @($Components.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
} elseif ($NonInteractive) {
    $selected = $recommended
} else {
    Write-Step 'Choix des composants (fenêtre « Atelier 3D — installation »)'
    $choice = Select-A3DComponents -Catalog $catalog -Selected $recommended -InstallDir $InstallDir -DirLocked $dirLocked -Hardware $hwText -Warnings $warnings `
                                   -Measure $Measure -FreeOf { param($d) Get-FreeBytes $d }
    if (-not $choice) {
        Write-Host ''
        Write-Warn 'Installation annulée : rien n''a été installé ni modifié.'
        exit 3
    }
    $InstallDir = [System.IO.Path]::GetFullPath($choice.dir)
    $selected = @($choice.components)
}
# dépendances : l'édition utilise le modèle de base Krea 2 ; le mode Références partage encodeur, VAE et LoRA du mode principal
if ($selected -contains 'image_edit' -and $selected -notcontains 'image') { $selected += 'image' }
if ($selected -contains 'video_ref' -and $selected -notcontains 'video') { $selected += 'video' }
if ($selected -contains 'video' -and $selected -contains 'video_attach') {
    Write-Warn 'Vidéo installée ET ComfyUI MiniMax existant cochés : la vidéo installée dans Atelier 3D est retenue.'
    $selected = @($selected | Where-Object { $_ -ne 'video_attach' })
}
$known = @($catalog | ForEach-Object { $_.key }) + @('video_attach')
foreach ($k in $selected) { if ($known -notcontains $k) { Write-Warn "Composant inconnu ignoré : $k" } }
$selected = @($selected | Where-Object { $known -contains $_ })

# ------------------------------------------------------------ création du dossier d'installation
$Home3D = $InstallDir
foreach ($sub in 'runtime', 'models', 'data', 'logs', 'config', 'downloads', 'backups', 'run') {
    New-Item -ItemType Directory -Force -Path (Join-Path $Home3D $sub) | Out-Null
}
Set-A3DLogFile (Join-Path $Home3D ('logs\install-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
Initialize-A3DManifest (Join-Path $Home3D 'config\install-manifest.json')
Write-A3DLog 'INFO' "Installation dans $Home3D depuis $DistDir"
Write-A3DLog 'INFO' $hwText

# Dossier runtime : chemin ASCII garanti pour les outils tiers sensibles aux accents.
$RuntimeReal = Join-Path $Home3D 'runtime'
$Runtime = $RuntimeReal
if (-not (Test-AsciiPath $Home3D)) {
    $alias = Join-Path ([System.IO.Path]::GetPathRoot($Home3D)) 'Atelier3D_runtime'
    $existing = Get-Item -LiteralPath $alias -ErrorAction SilentlyContinue
    if ($existing -and ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        $target = @($existing.Target)[0]
        if ($target -and ([IO.Path]::GetFullPath($target).TrimEnd('\') -ne $RuntimeReal.TrimEnd('\'))) { throw "$alias pointe déjà vers $target : choisissez un autre dossier d'installation." }
    } elseif ($existing) {
        throw "$alias existe déjà et n'est pas une jonction : impossible de créer l'alias ASCII du runtime."
    } else {
        New-Item -ItemType Junction -Path $alias -Target $RuntimeReal | Out-Null
    }
    $Runtime = $alias
    Write-Info ("Chemin avec caractères non ASCII : runtimes accessibles via la jonction {0}" -f $alias)
}
$Tools = Join-Path $Runtime 'tools'
$Downloads = Join-Path $Home3D 'downloads'
$ModelsDir = Join-Path $Home3D 'models'
New-Item -ItemType Directory -Force -Path $Tools | Out-Null

$loc = [ordered]@{ home = $Home3D; runtime = $Runtime; dist = $DistDir; updated_at = (Get-Date).ToString('o') }
Write-JsonFile $LocationFile $loc
Write-JsonFile $UserLocationFile $loc
Set-ComponentRecord 'hardware' ([ordered]@{ system = $sys; gpus = $gpus })
Set-ComponentRecord 'existing_comfyui' $comfyInstalls

Write-Info ("Composants retenus : {0}" -f $(if ($selected.Count) { $selected -join ', ' } else { 'interface + Blender uniquement' }))
$needComfy = @($selected | Where-Object { $_ -in $comfyComps }).Count -gt 0
$needVideo = @($selected | Where-Object { $_ -in $videoComps }).Count -gt 0

# modèles requis par profil
$neededModels = [ordered]@{}
foreach ($p in $Lock.models.PSObject.Properties) {
    foreach ($comp in $selected) {
        if ($profileMap.ContainsKey($comp) -and (@($p.Value.profiles) | Where-Object { $profileMap[$comp] -contains $_ })) { $neededModels[$p.Name] = $p.Value }
    }
}

# ------------------------------------------------------------ 5. estimation du volume
Write-Step 'Estimation du volume et partage des modèles existants'
$plan = @()
$total = [long]0
foreach ($k in $neededModels.Keys) {
    $e = $neededModels[$k]
    $dest = Join-Path (Join-Path $ModelsDir $e.comfy_folder) $e.filename
    $state = 'download'
    $shared = $null
    if (Test-Path -LiteralPath $dest) { $state = 'present' }
    else {
        $shared = Find-A3DExistingModel -Entry $e -ModelDirs $shareDirs
        if ($shared) { $state = 'share' }
        elseif (Test-Path -LiteralPath (Join-Path $Downloads ($e.filename + '.part'))) { $state = 'resume' }
    }
    if ($state -in @('download', 'resume')) { $total += [long]$e.size }
    $plan += [ordered]@{ id = $k; entry = $e; dest = $dest; state = $state; shared = $shared }
    $label = @{ download = 'à télécharger'; resume = 'reprise'; present = 'déjà installé'; share = 'partagé (empreinte identique)' }[$state]
    Write-Info ("{0,-30} {1,10}  {2}{3}" -f $e.filename, (Format-Bytes $e.size), $label, $(if ($shared) { ' ← ' + $shared } else { '' }))
}
$extraBytes = [long]0
if ($needComfy -and -not (Test-Path -LiteralPath (Join-Path $Runtime 'comfyui\python_embeded\python.exe'))) { $extraBytes += [long]$Lock.comfyui.portable.size * 4 }
if ($needVideo -and -not (Test-Path -LiteralPath (Join-Path $Runtime 'comfyui\ComfyUI\custom_nodes\ComfyUI-VideoHelperSuite\__init__.py'))) { $extraBytes += 300MB }
if ($selected -contains 'rig_mia') {
    foreach ($w in $Lock.engines.mia.weights.PSObject.Properties) { if (-not (Test-Path -LiteralPath (Join-Path $ModelsDir ('mia\' + $w.Name)))) { $total += [long]$w.Value.size } }
    if (-not (Test-Path -LiteralPath (Join-Path $Runtime 'venv-mia\Scripts\python.exe'))) { $extraBytes += 6GB }
}
if (-not (Test-Path -LiteralPath (Join-Path $Runtime 'venv-app\Scripts\python.exe'))) { $extraBytes += 600MB }
$free = Get-FreeBytes $Home3D
Write-Info ("Téléchargements : {0} ; runtimes à décompresser/installer : ~{1} ; espace libre : {2}" -f (Format-Bytes $total), (Format-Bytes $extraBytes), (Format-Bytes $free))
if ($free -lt ($total + $extraBytes) * 1.1) { throw ("Espace insuffisant sur {0} : {1} nécessaires (+10 %), {2} libres." -f ([IO.Path]::GetPathRoot($Home3D)), (Format-Bytes ($total + $extraBytes)), (Format-Bytes $free)) }

# Jeton Hugging Face : demandé uniquement si une ressource retenue est protégée.
$hfToken = $null
$gated = @($plan | Where-Object { $_.state -in @('download', 'resume') -and $_.entry.gated })
if ($gated.Count -gt 0) {
    Write-Warn ("Ressources protégées : {0}" -f (($gated | ForEach-Object { $_.entry.filename }) -join ', '))
    if (-not $NonInteractive) {
        $sec = Read-Host 'Jeton Hugging Face (lecture seule ; saisie masquée, non enregistrée)' -AsSecureString
        $hfToken = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        Register-A3DSecret $hfToken
    }
}

# ------------------------------------------------------------ 6. outils
Write-Step 'Outils portables (uv, 7-Zip)'
$uvZip = Invoke-A3DDownload -Url $Lock.tools.uv.url -Dest (Join-Path $Downloads ('uv-' + $Lock.tools.uv.version + '.zip')) -Size $Lock.tools.uv.size -Sha256 $Lock.tools.uv.sha256 -Label "uv $($Lock.tools.uv.version)"
$uv = Join-Path $Tools 'uv\uv.exe'
if (-not (Test-Path -LiteralPath $uv) -or -not ((& $uv --version) -match [regex]::Escape($Lock.tools.uv.version))) {
    Expand-Archive -LiteralPath $uvZip -DestinationPath (Join-Path $Tools 'uv') -Force
}
$sevenZip = Invoke-A3DDownload -Url $Lock.tools.'7zr'.url -Dest (Join-Path $Tools '7zr.exe') -Size $Lock.tools.'7zr'.size -Sha256 $Lock.tools.'7zr'.sha256 -Label "7zr $($Lock.tools.'7zr'.version)"
$uvEnv = @{ UV_CACHE_DIR = (Join-Path $Runtime 'uv-cache'); UV_PYTHON_INSTALL_DIR = (Join-Path $Runtime 'python'); UV_NO_CONFIG = '1'; UV_PYTHON_PREFERENCE = 'only-managed'; UV_LINK_MODE = 'copy'; UV_NATIVE_TLS = '1' }
Set-StepState 'tools' 'done' @{ uv = $Lock.tools.uv.version; '7zr' = $Lock.tools.'7zr'.version }

# ------------------------------------------------------------ 7. environnement Python de l'application
Write-Step 'Environnement Python de l''application (isolé)'
$appPy = Join-Path $Runtime 'venv-app\Scripts\python.exe'
$appLock = Join-Path $DistDir 'app\backend\requirements.lock'
$appLockHash = Get-FileSha256 $appLock
$st = Get-StepState 'venv_app'
if (-not (Test-Path -LiteralPath $appPy) -or $null -eq $st -or $st['status'] -ne 'done' -or $st['data']['lock_sha256'] -ne $appLockHash) {
    Invoke-Native -Exe $uv -Arguments @('python', 'install', $Lock.python.app_version) -What 'uv python install' -Env $uvEnv | Out-Null
    if (-not (Test-Path -LiteralPath $appPy)) { Invoke-Native -Exe $uv -Arguments @('venv', (Join-Path $Runtime 'venv-app'), '--python', $Lock.python.app_version) -What 'uv venv (app)' -Env $uvEnv | Out-Null }
    Invoke-Native -Exe $uv -Arguments @('pip', 'sync', '--python', $appPy, '--require-hashes', $appLock) -What 'installation des dépendances de l''application' -Env $uvEnv | Out-Null
    Set-StepState 'venv_app' 'done' @{ lock_sha256 = $appLockHash; python = $Lock.python.app_version }
}
$pyv = & $appPy -c "import sys;print(sys.version.split()[0])"
Write-Ok ("Python {0} prêt ({1})" -f $pyv, $appPy)

# ------------------------------------------------------------ 8. ComfyUI dédié
$ComfyDir = Join-Path $Runtime 'comfyui'
if ($needComfy) {
    Write-Step ("ComfyUI dédié {0} (portable officiel, instance séparée de vos ComfyUI existants)" -f $Lock.comfyui.portable.version)
    $st = Get-StepState 'comfyui'
    if (-not (Test-Path -LiteralPath (Join-Path $ComfyDir 'python_embeded\python.exe')) -or $null -eq $st -or $st['status'] -ne 'done' -or $st['data']['version'] -ne $Lock.comfyui.portable.version) {
        $arch = Invoke-A3DDownload -Url $Lock.comfyui.portable.url -Dest (Join-Path $Downloads ('ComfyUI_windows_portable_nvidia_' + $Lock.comfyui.portable.version + '.7z')) -Size $Lock.comfyui.portable.size -Sha256 $Lock.comfyui.portable.sha256 -Label "ComfyUI portable $($Lock.comfyui.portable.version)"
        $tmp = Join-Path $Runtime 'comfyui_extract'
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
        Write-Info 'Décompression (quelques minutes)…'
        Invoke-Native -Exe $sevenZip -Arguments @('x', '-y', '-bso0', '-bsp1', ('-o' + $tmp), $arch) -What '7zr (ComfyUI)' | Out-Null
        if (Test-Path -LiteralPath $ComfyDir) {
            $bk = Join-Path $Home3D ('backups\comfyui-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            Write-Info "Ancienne version déplacée vers $bk"
            Move-Item -LiteralPath $ComfyDir -Destination $bk
        }
        Move-Item -LiteralPath (Join-Path $tmp 'ComfyUI_windows_portable') -Destination $ComfyDir
        Remove-Item -LiteralPath $tmp -Recurse -Force
        Set-StepState 'comfyui' 'done' @{ version = $Lock.comfyui.portable.version; commit = $Lock.comfyui.portable.commit }
    }
    $tv = & (Join-Path $ComfyDir 'python_embeded\python.exe') -c "import torch,sys;print(sys.version.split()[0], torch.__version__, torch.version.cuda, torch.cuda.is_available())"
    Write-Ok ("ComfyUI {0} : Python/PyTorch/CUDA/GPU = {1}" -f $Lock.comfyui.portable.version, $tv)
    Set-ComponentRecord 'comfyui' @{ dir = $ComfyDir; version = $Lock.comfyui.portable.version; torch = "$tv" }

    if ($selected -contains 'image_edit') {
        $kn = $Lock.comfyui.custom_nodes.'comfyui-krea2edit'
        $dest = Join-Path $ComfyDir 'ComfyUI\custom_nodes\comfyui-krea2edit'
        $st = Get-StepState 'krea2edit'
        if ($null -eq $st -or $st['status'] -ne 'done' -or $st['data']['commit'] -ne $kn.commit -or -not (Test-Path -LiteralPath (Join-Path $dest '__init__.py'))) {
            Install-PinnedRepo -Spec $kn -Dest $dest -DownloadDir $Downloads -Label 'comfyui-krea2edit'
            Set-StepState 'krea2edit' 'done' @{ commit = $kn.commit }
        }
    }

    if ($needVideo) {
        # workflows MiniMax H3 Ultra fournis : nodes MiniMax natives de ComfyUI + rgthree (switch, LoRA) + VideoHelperSuite (MP4 avec son)
        foreach ($name in @('rgthree-comfy', 'ComfyUI-VideoHelperSuite')) {
            $spec = $Lock.comfyui.custom_nodes.$name
            $dest = Join-Path (Join-Path $ComfyDir 'ComfyUI\custom_nodes') $name
            $st = Get-StepState ('node_' + $name)
            if ($null -eq $st -or $st['status'] -ne 'done' -or $st['data']['commit'] -ne $spec.commit -or -not (Test-Path -LiteralPath (Join-Path $dest '__init__.py'))) {
                Install-PinnedRepo -Spec $spec -Dest $dest -DownloadDir $Downloads -Label ("{0} ({1})" -f $name, $spec.license)
                Set-StepState ('node_' + $name) 'done' @{ commit = $spec.commit; license = $spec.license }
            }
        }
        $comfyPy = Join-Path $ComfyDir 'python_embeded\python.exe'
        $wheels = @($Lock.comfyui.python_packages.video)
        $want = ($wheels | ForEach-Object { $_.filename }) -join ','
        $st = Get-StepState 'video_python_packages'
        if ($null -eq $st -or $st['status'] -ne 'done' -or $st['data']['wheels'] -ne $want) {
            $files = @()
            foreach ($w in $wheels) { $files += Invoke-A3DDownload -Url $w.url -Dest (Join-Path $Downloads $w.filename) -Size $w.size -Sha256 $w.sha256 -Label $w.filename }
            # fichiers vérifiés par SHA-256 ; aucune dépendance transitive (numpy est déjà fourni par ComfyUI)
            Invoke-Native -Exe $comfyPy -Arguments (@('-s', '-m', 'pip', 'install', '--no-deps', '--no-index', '--disable-pip-version-check', '--no-warn-script-location') + $files) -What 'dépendances de VideoHelperSuite (OpenCV, ffmpeg)' | Out-Null
            Set-StepState 'video_python_packages' 'done' @{ wheels = $want }
        }
        $chk = & $comfyPy -s -c "import cv2, imageio_ffmpeg; print('OpenCV', cv2.__version__, '/ ffmpeg', imageio_ffmpeg.get_ffmpeg_version())"
        if ($LASTEXITCODE -ne 0) { throw 'OpenCV / ffmpeg introuvables dans le Python du ComfyUI dédié après installation.' }
        Write-Ok ("Nodes vidéo prêtes : rgthree, VideoHelperSuite ; {0}" -f $chk)
        Set-ComponentRecord 'video' @{ provider = 'comfyui_dedicated'; nodes = @('rgthree-comfy', 'ComfyUI-VideoHelperSuite'); python_packages = $want }
    }
}

# ------------------------------------------------------------ 9. modèles
if ($plan.Count -gt 0) {
    Write-Step 'Modèles (sources officielles, vérification SHA-256)'
    foreach ($p in $plan) {
        $e = $p.entry
        $destDir = Split-Path -Parent $p.dest
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        if ($p.state -eq 'present') {
            $rec = $script:A3D_Manifest['components']
            $known = $rec.Contains('models') -and $rec['models'].Contains($p.id) -and $rec['models'][$p.id]['sha256'] -eq $e.sha256
            if (-not $known -and (Get-FileSha256 $p.dest) -ne $e.sha256) { Write-Warn "$($e.filename) présent mais différent : remplacement"; Remove-Item -LiteralPath $p.dest -Force; $p.state = 'download' }
        }
        if ($p.state -eq 'share') {
            $sameVolume = ([IO.Path]::GetPathRoot($p.shared) -eq [IO.Path]::GetPathRoot($p.dest))
            if ($sameVolume) {
                New-Item -ItemType HardLink -Path $p.dest -Target $p.shared | Out-Null
                Write-Ok ("{0} partagé par lien physique (aucune copie) avec {1}" -f $e.filename, $p.shared)
            } else {
                Copy-Item -LiteralPath $p.shared -Destination $p.dest
                Write-Ok ("{0} copié depuis {1} (autre volume)" -f $e.filename, $p.shared)
            }
        }
        if ($p.state -in @('download', 'resume')) {
            $part = Join-Path $Downloads $e.filename
            Invoke-A3DDownload -Url $e.url -Dest $part -Size $e.size -Sha256 $e.sha256 -Label $e.filename -BearerToken $(if ($e.gated) { $hfToken } else { $null }) | Out-Null
            Move-Item -LiteralPath $part -Destination $p.dest -Force
        }
        if (-not $script:A3D_Manifest['components'].Contains('models')) { $script:A3D_Manifest['components']['models'] = [ordered]@{} }
        $script:A3D_Manifest['components']['models'][$p.id] = [ordered]@{ path = $p.dest; sha256 = $e.sha256; source = $e.source; shared_from = $p.shared }
        Save-A3DManifest
    }
}

# ------------------------------------------------------------ 10. moteur de rigging MIA
$MiaDir = Join-Path $Runtime 'engines\mia'
$miaPy = Join-Path $Runtime 'venv-mia\Scripts\python.exe'
if ($selected -contains 'rig_mia') {
    Write-Step 'Moteur de rigging Make-It-Animatable (environnement isolé)'
    $mia = $Lock.engines.mia
    $st = Get-StepState 'mia_code'
    if ($null -eq $st -or $st['status'] -ne 'done' -or $st['data']['commit'] -ne $mia.code.commit) {
        Install-PinnedRepo -Spec $mia.code -Dest $MiaDir -DownloadDir $Downloads -Label 'Make-It-Animatable'
        Set-StepState 'mia_code' 'done' @{ commit = $mia.code.commit }
    }
    foreach ($w in $mia.weights.PSObject.Properties) {
        $dest = Join-Path $ModelsDir ('mia\' + $w.Name)
        if (-not (Test-Path -LiteralPath $dest)) {
            $tmpw = Join-Path $Downloads ('mia_' + $w.Name)
            Invoke-A3DDownload -Url $w.Value.url -Dest $tmpw -Size $w.Value.size -Sha256 $w.Value.sha256 -Label ('MIA ' + $w.Name) | Out-Null
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
            Move-Item -LiteralPath $tmpw -Destination $dest -Force
        }
    }
    $miaLock = Join-Path $DistDir 'app\engines\mia_runner\requirements.lock'
    $miaLockHash = Get-FileSha256 $miaLock
    $st = Get-StepState 'venv_mia'
    if (-not (Test-Path -LiteralPath $miaPy) -or $null -eq $st -or $st['status'] -ne 'done' -or $st['data']['lock_sha256'] -ne $miaLockHash) {
        Invoke-Native -Exe $uv -Arguments @('python', 'install', $Lock.python.mia_version) -What 'uv python install (MIA)' -Env $uvEnv | Out-Null
        if (-not (Test-Path -LiteralPath $miaPy)) { Invoke-Native -Exe $uv -Arguments @('venv', (Join-Path $Runtime 'venv-mia'), '--python', $Lock.python.mia_version) -What 'uv venv (MIA)' -Env $uvEnv | Out-Null }
        Write-Info 'Installation de PyTorch 2.8 cu128 et des dépendances MIA (~3 Go)…'
        Invoke-Native -Exe $uv -Arguments @('pip', 'sync', '--python', $miaPy, '--require-hashes', '--index-url', 'https://download.pytorch.org/whl/cu128', '--extra-index-url', 'https://pypi.org/simple', '--extra-index-url', 'https://miropsota.github.io/torch_packages_builder', '--find-links', 'https://data.pyg.org/whl/torch-2.8.0+cu128.html', '--index-strategy', 'unsafe-best-match', $miaLock) -What 'dépendances MIA' -Env $uvEnv | Out-Null
        Set-StepState 'venv_mia' 'done' @{ lock_sha256 = $miaLockHash; python = $Lock.python.mia_version }
    }
    $tv = & $miaPy -c "import torch,torch_cluster,pytorch3d;print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
    Write-Ok ("Environnement MIA : PyTorch/CUDA/GPU = {0}" -f $tv)
    Set-ComponentRecord 'mia' @{ code = $MiaDir; python = $miaPy; torch = "$tv"; commit = $mia.code.commit }
}

# ------------------------------------------------------------ 11. Blender
Write-Step 'Blender'
$blender = $null
foreach ($b in $blenders) { try { if ([version]$b.version -ge $minVer) { $blender = $b; break } } catch { } }
$localBlender = Join-Path $Runtime ('blender\blender-' + $Lock.blender.download.version + '-windows-x64\blender.exe')
if (-not $blender -and (Test-Path -LiteralPath $localBlender)) { $blender = @{ path = $localBlender; version = $Lock.blender.download.version } }
if ($blender) {
    Write-Ok ("Blender {0} réutilisé : {1}" -f $blender.version, $blender.path)
} else {
    Write-Info ("Aucun Blender >= {0} : installation portable de Blender {1}" -f $Lock.blender.minimum_version, $Lock.blender.download.version)
    $bz = Invoke-A3DDownload -Url $Lock.blender.download.url -Dest (Join-Path $Downloads ('blender-' + $Lock.blender.download.version + '-windows-x64.zip')) -Size $Lock.blender.download.size -Sha256 $Lock.blender.download.sha256 -Label "Blender $($Lock.blender.download.version)"
    New-Item -ItemType Directory -Force -Path (Join-Path $Runtime 'blender') | Out-Null
    Invoke-Native -Exe (Join-Path $env:SystemRoot 'System32\tar.exe') -Arguments @('-xf', $bz, '-C', (Join-Path $Runtime 'blender')) -What 'décompression Blender' | Out-Null
    $blender = @{ path = $localBlender; version = $Lock.blender.download.version }
    Write-Ok "Blender portable installé : $localBlender"
}
Set-ComponentRecord 'blender' @{ path = $blender.path; version = $blender.version }

# ------------------------------------------------------------ 12. configuration + diagnostic (Python)
Write-Step 'Configuration de l''application'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Join-Path $DistDir 'app\backend')
$setupArgs = @('-m', 'atelier3d.setup_install', '--home', $Home3D, '--runtime', $Runtime, '--dist', $DistDir, '--components', ($selected -join ','), '--blender', $blender.path)
Invoke-Native -Exe $appPy -Arguments $setupArgs -What 'configuration Atelier 3D' | ForEach-Object { Write-Info $_ }
Set-StepState 'config' 'done'

Write-Step 'Diagnostic'
Invoke-Native -Exe $appPy -Arguments @('-m', 'atelier3d.diagnose', '--home', $Home3D, '--quick') -What 'diagnostic' | ForEach-Object { Write-Host "    $_" }

Write-Host ''
Write-Ok ("Installation terminée. Journal : {0}" -f $script:A3D_LogFile)
if (-not $SkipStart) {
    Write-Step 'Démarrage'
    & (Join-Path $PSScriptRoot 'start.ps1')
    exit $LASTEXITCODE
}
exit 0
