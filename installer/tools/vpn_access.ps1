# Atelier 3D — accès à distance par le VPN WireGuard de la Freebox (appelé par ACCES_VPN.bat)
# Crée (ou retire avec -Remove) :
#   1. une règle de pare-feu Windows « Atelier 3D (VPN WireGuard) » : TCP, port d'Atelier 3D, depuis les réseaux autorisés
#      uniquement (par défaut 192.168.27.64/27 = clients du serveur WireGuard de la Freebox) ;
#   2. une route vers ces réseaux par la Freebox (passerelle du réseau local) : sans elle, si un autre VPN (ProtonVPN…)
#      capte tout le trafic du PC, les réponses partiraient dans ce VPN et les appareils distants n'auraient aucune réponse.
# Demande les droits administrateur (fenêtre Windows). Ne modifie rien d'autre.
param([switch]$Remove, [switch]$Elevated)

$DistDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $DistDir 'installer\lib\common.ps1')

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    if ($Elevated) { Write-Err 'Droits administrateur refusés : rien n''a été modifié.'; exit 1 }
    $a = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Elevated')
    if ($Remove) { $a += '-Remove' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $a -Wait
    exit 0
}

$loc = Read-JsonFile (Join-Path $DistDir 'install-location.json')
if (-not $loc) { $loc = Read-JsonFile (Join-Path $env:LOCALAPPDATA 'Atelier3D\install-location.json') }
if (-not $loc -or -not $loc.home) { Write-Err 'Installation introuvable : lancez d''abord INSTALL.bat.'; Read-Host 'Entrée pour fermer'; exit 2 }
$cfg = Read-JsonFile (Join-Path $loc.home 'config\atelier3d.json')
$nets = @()
if ($cfg -and $cfg.security -and $cfg.security.remote -and $cfg.security.remote.networks) { $nets = @($cfg.security.remote.networks | Where-Object { $_ }) }
if ($nets.Count -eq 0) { $nets = @('192.168.27.64/27') }
$state = Read-JsonFile (Join-Path $loc.home 'run\state.json')
$port = 8765
if ($state -and $state.app -and $state.app.port) { $port = [int]$state.app.port } elseif ($cfg -and $cfg.app -and $cfg.app.port) { $port = [int]$cfg.app.port }
$ruleName = 'Atelier 3D (VPN WireGuard)'

# état précédent retiré (script relançable sans doublon)
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
foreach ($n in $nets) { Get-NetRoute -DestinationPrefix $n -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue }
if ($Remove) {
    Write-Ok 'Règle de pare-feu et route d''Atelier 3D retirées.'
    Write-Host '  Pensez aussi à désactiver « Accès à distance » dans ⚙ Réglages, puis STOP.bat et START.bat.'
    Read-Host 'Entrée pour fermer'
    exit 0
}

New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -RemoteAddress $nets -Profile Any `
    -Description 'Atelier 3D : accès depuis le VPN WireGuard de la Freebox uniquement (créée par ACCES_VPN.bat)' | Out-Null
Write-Ok ("Pare-feu : TCP {0} autorisé depuis {1} uniquement" -f $port, ($nets -join ', '))

$lan = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' -and "$($_.IPv4DefaultGateway.NextHop)" -like '192.168.*' } | Select-Object -First 1
if ($lan) {
    $gw = $lan.IPv4DefaultGateway.NextHop
    foreach ($n in $nets) { New-NetRoute -DestinationPrefix $n -InterfaceIndex $lan.InterfaceIndex -NextHop $gw -RouteMetric 5 | Out-Null }
    Write-Ok ("Route : {0} par la Freebox ({1}, carte « {2} »)" -f ($nets -join ', '), $gw, $lan.InterfaceAlias)
    Write-Host ''
    Write-Host ('  Adresse à ouvrir depuis l''appareil connecté au VPN : http://{0}:{1}/' -f @($lan.IPv4Address)[0].IPAddress, $port) -ForegroundColor Cyan
} else {
    Write-Warn 'Passerelle du réseau local (192.168.x.x) introuvable : route non créée.'
}
Write-Host ''
Write-Host '  À faire aussi :'
Write-Host '   - Atelier 3D : ⚙ Réglages → Accès à distance → activer, puis STOP.bat et START.bat'
Write-Host '   - Freebox : réserver l''adresse IP de ce PC (bail DHCP statique) pour qu''elle ne change pas'
Write-Host '   - ProtonVPN (ou autre VPN actif sur ce PC) : exclure 192.168.27.64/27 (split tunneling), indispensable si le kill switch est actif'
Read-Host 'Entrée pour fermer'
