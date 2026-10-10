# Atelier 3D — mise à jour contrôlée avec retour arrière
#   UPDATE.bat                 sauvegarde → installation des versions verrouillées de CE dossier → tests → retour arrière si échec
#   UPDATE.bat -Rollback <id>  restaure une sauvegarde (configuration, base de données, ComfyUI dédié précédent)
# Jamais de mise à jour automatique vers « latest » : seules les versions de installer\versions.lock.json sont installées.
param([string]$Rollback = '', [switch]$NonInteractive)

$DistDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$loc = Read-JsonFile (Join-Path $DistDir 'install-location.json')
if (-not $loc) { $loc = Read-JsonFile (Join-Path $env:LOCALAPPDATA 'Atelier3D\install-location.json') }
if (-not $loc) { Write-Err 'Aucune installation existante : utilisez INSTALL.bat.'; exit 2 }
$Home3D = $loc.home
$py = Join-Path $loc.runtime 'venv-app\Scripts\python.exe'
Set-A3DLogFile (Join-Path $Home3D ('logs\update-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
$env:ATELIER3D_HOME = $Home3D
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Join-Path $DistDir 'app\backend')

function Stop-App { if (Test-Path -LiteralPath $py) { & $py -m atelier3d.launcher stop | ForEach-Object { Write-Info $_ } } }

function Restore-Backup([string]$Id) {
    $b = Join-Path $Home3D "backups\update-$Id"
    if (-not (Test-Path -LiteralPath $b)) { throw "Sauvegarde introuvable : $b" }
    Stop-App
    Write-Step "Retour arrière vers la sauvegarde $Id"
    Copy-Item -LiteralPath (Join-Path $b 'config\atelier3d.json') -Destination (Join-Path $Home3D 'config\atelier3d.json') -Force
    Copy-Item -LiteralPath (Join-Path $b 'config\install-manifest.json') -Destination (Join-Path $Home3D 'config\install-manifest.json') -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath (Join-Path $b 'atelier3d.sqlite3')) {
        Copy-Item -LiteralPath (Join-Path $b 'atelier3d.sqlite3') -Destination (Join-Path $Home3D 'data\atelier3d.sqlite3') -Force
        Remove-Item -LiteralPath (Join-Path $Home3D 'data\atelier3d.sqlite3-wal'), (Join-Path $Home3D 'data\atelier3d.sqlite3-shm') -Force -ErrorAction SilentlyContinue
    }
    $prevComfy = Get-Content -LiteralPath (Join-Path $b 'comfyui_backup.txt') -ErrorAction SilentlyContinue
    if ($prevComfy -and (Test-Path -LiteralPath $prevComfy)) {
        $cur = Join-Path $loc.runtime 'comfyui'
        if (Test-Path -LiteralPath $cur) { Move-Item -LiteralPath $cur -Destination ($cur + '_rejete_' + (Get-Date -Format 'yyyyMMddHHmmss')) }
        Move-Item -LiteralPath $prevComfy -Destination $cur
        Write-Ok "ComfyUI précédent restauré"
    }
    Write-Ok "Configuration et données restaurées depuis $b"
}

if ($Rollback) { Restore-Backup $Rollback; exit 0 }

$id = Get-Date -Format 'yyyyMMdd-HHmmss'
$b = Join-Path $Home3D "backups\update-$id"
Write-Step "Sauvegarde avant mise à jour ($b)"
Stop-App
New-Item -ItemType Directory -Force -Path (Join-Path $b 'config') | Out-Null
Copy-Item -LiteralPath (Join-Path $Home3D 'config\atelier3d.json') -Destination (Join-Path $b 'config') -ErrorAction SilentlyContinue
Copy-Item -LiteralPath (Join-Path $Home3D 'config\install-manifest.json') -Destination (Join-Path $b 'config') -ErrorAction SilentlyContinue
Invoke-Native -Exe $py -Arguments @('-c', "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()",
    (Join-Path $Home3D 'data\atelier3d.sqlite3'), (Join-Path $b 'atelier3d.sqlite3')) -What 'sauvegarde SQLite' | Out-Null
Write-Ok 'Configuration et base de données sauvegardées (les artefacts ne sont jamais supprimés)'

Write-Step 'Installation des versions verrouillées de ce dossier'
$before = @(Get-ChildItem -LiteralPath (Join-Path $Home3D 'backups') -Directory -Filter 'comfyui-*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
$instArgs = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'install.ps1'), '-InstallDir', $Home3D, '-SkipStart', '-NonInteractive')
$comps = (Read-JsonFile (Join-Path $Home3D 'config\atelier3d.json')).components
if ($comps) { $instArgs += @('-Components', ($comps -join ',')) }
& powershell.exe @instArgs
$rc = $LASTEXITCODE
$after = @(Get-ChildItem -LiteralPath (Join-Path $Home3D 'backups') -Directory -Filter 'comfyui-*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
$newBackup = $after | Where-Object { $before -notcontains $_ } | Select-Object -First 1
if ($newBackup) { Set-Content -LiteralPath (Join-Path $b 'comfyui_backup.txt') -Value $newBackup -Encoding UTF8 }

if ($rc -eq 0) {
    Write-Step 'Test de la nouvelle combinaison de versions'
    & $py -m atelier3d.launcher start --no-browser
    $ok = ($LASTEXITCODE -eq 0)
    if ($ok) {
        $code = "import sys; sys.path.insert(0, r'$DistDir\clients\python'); from atelier3d_client import Atelier3D; c=Atelier3D(); caps=c.capabilities(refresh=True)['capabilities']; bad=[k for k,v in caps.items() if v.get('status')=='indisponible' and k in ('image.create','image.edit','image.remove_background','mesh.generate','blender','rig.mia')]; print('indisponibles:', bad); sys.exit(1 if bad else 0)"
        & $py -c $code
        $ok = ($LASTEXITCODE -eq 0)
    }
    if ($ok) { Write-Ok "Mise à jour validée. Sauvegarde conservée : UPDATE.bat -Rollback $id"; exit 0 }
    Write-Warn 'Les tests de la nouvelle combinaison ont échoué.'
}
else { Write-Warn "L'installation de la nouvelle version a échoué (code $rc)." }
if ($NonInteractive -or (Read-Host 'Revenir à la version précédente ? [O/n]') -notmatch '^[nN]') {
    Restore-Backup $id
    & $py -m atelier3d.launcher start --no-browser | Out-Null
    exit 1
}
exit 1
