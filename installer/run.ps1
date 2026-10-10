# Atelier 3D — démarrage / arrêt / diagnostic (appelé par START.bat, STOP.bat, DIAGNOSE.bat)
param([Parameter(Position = 0)][ValidateSet('start', 'stop', 'diagnose', 'status', 'restart')][string]$Command = 'start',
      [switch]$NoBrowser)

$DistDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$loc = Read-JsonFile (Join-Path $DistDir 'install-location.json')
if (-not $loc) { $loc = Read-JsonFile (Join-Path $env:LOCALAPPDATA 'Atelier3D\install-location.json') }
if (-not $loc -or -not $loc.home) {
    Write-Err 'Installation introuvable : lancez d''abord INSTALL.bat.'
    exit 2
}
$py = Join-Path $loc.runtime 'venv-app\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Err "Environnement Python absent ($py) : relancez INSTALL.bat."
    exit 2
}
$env:ATELIER3D_HOME = $loc.home
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Join-Path $DistDir 'app\backend')

switch ($Command) {
    'start' {
        $args2 = @('-m', 'atelier3d.launcher', 'start')
        if ($NoBrowser) { $args2 += '--no-browser' }
        & $py @args2
        exit $LASTEXITCODE
    }
    'restart' { & $py -m atelier3d.launcher restart; exit $LASTEXITCODE }
    'stop' { & $py -m atelier3d.launcher stop; exit $LASTEXITCODE }
    'status' { & $py -m atelier3d.launcher status; exit $LASTEXITCODE }
    'diagnose' {
        Write-Host ''
        Write-Host '  Atelier 3D - diagnostic' -ForegroundColor Cyan
        Write-Host ("  Installation : {0}" -f $loc.home)
        & $py -m atelier3d.launcher status
        & $py -m atelier3d.diagnose
        exit $LASTEXITCODE
    }
}
