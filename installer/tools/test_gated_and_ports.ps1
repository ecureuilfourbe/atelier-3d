# Tests : ressource Hugging Face protégée sans jeton (message explicite, aucun fichier laissé) et port occupé (port libre suivant choisi).
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'lib\common.ps1')
$dir = Join-Path $env:TEMP 'a3d_gated_test'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
try {
    # dépôt officiel krea/Krea-2-Turbo : protégé (gated=auto). La carte README reste publique, les autres fichiers exigent un compte.
    Invoke-A3DDownload -Url 'https://huggingface.co/krea/Krea-2-Turbo/resolve/main/model_index.json' -Dest (Join-Path $dir 'model_index.json') -Label 'krea/Krea-2-Turbo model_index.json' -Attempts 1 | Out-Null
    Write-Host 'ÉCHEC : téléchargement protégé accepté sans jeton'
} catch {
    Write-Host ("OK : refus explicite -> {0}" -f $_.Exception.Message)
}
if (Test-Path (Join-Path $dir 'model_index.json*')) { Write-Host 'ÉCHEC : fichier partiel laissé' } else { Write-Host 'OK : aucun fichier partiel laissé' }
Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Parse('127.0.0.1'), 8899)
$listener.Start()
$py = 'E:\Atelier3D_runtime\venv-app\Scripts\python.exe'
$env:PYTHONPATH = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'app\backend')
$chosen = & $py -c "from atelier3d.launcher import pick_port, port_free; print(port_free(8899), pick_port(8899))"
$listener.Stop()
Write-Host ("Port 8899 occupé -> port_free/choisi : {0}" -f $chosen)
