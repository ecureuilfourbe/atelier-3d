# Test de reprise d'un téléchargement interrompu (fonction réelle de l'installateur).
# 1) lance le téléchargement d'un fichier verrouillé, 2) tue curl après quelques secondes, 3) relance : reprise + SHA-256.
param([string]$Dir = (Join-Path $env:TEMP 'a3d_resume_test'), [int]$KillAfter = 3)
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'lib\common.ps1')
$lock = Read-JsonFile (Join-Path (Split-Path -Parent $PSScriptRoot) 'versions.lock.json')
$e = $lock.models.birefnet
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$dest = Join-Path $Dir $e.filename
Remove-Item -LiteralPath $dest, ($dest + '.part') -Force -ErrorAction SilentlyContinue
$part = $dest + '.part'
$curl = Get-CurlExe
$p = Start-Process -PassThru -NoNewWindow -FilePath $curl -ArgumentList @('-sL', '--fail', '-C', '-', '-o', "`"$part`"", $e.url)
Start-Sleep -Seconds $KillAfter
Stop-Process -Id $p.Id -Force
Start-Sleep -Milliseconds 500
$partial = (Get-Item -LiteralPath $part).Length
Write-Host ("Interrompu volontairement après {0} s : {1} reçus sur {2}" -f $KillAfter, (Format-Bytes $partial), (Format-Bytes $e.size))
$sw = [Diagnostics.Stopwatch]::StartNew()
Invoke-A3DDownload -Url $e.url -Dest $dest -Size $e.size -Sha256 $e.sha256 -Label $e.filename | Out-Null
$ok = (Get-FileSha256 $dest) -eq $e.sha256
Write-Host ("Reprise terminée en {0:N1} s, SHA-256 conforme : {1}" -f $sw.Elapsed.TotalSeconds, $ok)
Remove-Item -LiteralPath $Dir -Recurse -Force
if (-not $ok) { exit 1 }
