# Atelier 3D — fonctions communes de l'installateur (Windows PowerShell 5.1+)
# Aucune modification de politique globale, aucun secret journalisé.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 }

$script:A3D_LogFile = $null
$script:A3D_Secrets = New-Object System.Collections.Generic.List[string]

function Register-A3DSecret([string]$Value) {
    if ($Value) { $script:A3D_Secrets.Add($Value) }
}

function Protect-A3DText([string]$Text) {
    if (-not $Text) { return $Text }
    foreach ($s in $script:A3D_Secrets) { if ($s) { $Text = $Text.Replace($s, '***') } }
    # jetons Hugging Face éventuels
    return ($Text -replace 'hf_[A-Za-z0-9]{20,}', 'hf_***')
}

function Set-A3DLogFile([string]$Path) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $script:A3D_LogFile = $Path
}

function Write-A3DLog([string]$Level, [string]$Message) {
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, (Protect-A3DText $Message)
    if ($script:A3D_LogFile) { Add-Content -LiteralPath $script:A3D_LogFile -Value $line -Encoding UTF8 }
}

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host ('==> ' + $Message) -ForegroundColor Cyan
    Write-A3DLog 'STEP' $Message
}
function Write-Info([string]$Message) { Write-Host ('    ' + (Protect-A3DText $Message)); Write-A3DLog 'INFO' $Message }
function Write-Ok([string]$Message) { Write-Host ('    [OK] ' + (Protect-A3DText $Message)) -ForegroundColor Green; Write-A3DLog 'OK' $Message }
function Write-Warn([string]$Message) { Write-Host ('    [ATTENTION] ' + (Protect-A3DText $Message)) -ForegroundColor Yellow; Write-A3DLog 'WARN' $Message }
function Write-Err([string]$Message) { Write-Host ('    [ERREUR] ' + (Protect-A3DText $Message)) -ForegroundColor Red; Write-A3DLog 'ERROR' $Message }

function Get-A3DProp($Obj, [string]$Name) {
    <# Champ facultatif d'un dictionnaire ou d'un objet JSON, $null s'il manque (le mode strict refuse l'accès direct). #>
    if ($null -eq $Obj) { return $null }
    if ($Obj -is [System.Collections.IDictionary]) { if ($Obj.Contains($Name)) { return $Obj[$Name] }; return $null }
    $p = $Obj.PSObject.Properties[$Name]
    if ($p) { return $p.Value }
    return $null
}

function Format-Bytes([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N1} Gio' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} Mio' -f ($Bytes / 1MB)) }
    return ('{0:N0} o' -f $Bytes)
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    return ($raw | ConvertFrom-Json)
}

function Write-JsonFile([string]$Path, $Object) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $json = $Object | ConvertTo-Json -Depth 30
    $tmp = $Path + '.tmp'
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function ConvertTo-Hashtable($Object) {
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) { return $Object }
    if ($Object -is [System.Management.Automation.PSCustomObject]) {
        $h = [ordered]@{}
        foreach ($p in $Object.PSObject.Properties) { $h[$p.Name] = ConvertTo-Hashtable $p.Value }
        return $h
    }
    if ($Object -is [System.Collections.IEnumerable] -and -not ($Object -is [string])) {
        return @($Object | ForEach-Object { ConvertTo-Hashtable $_ })
    }
    return $Object
}

# ---------------------------------------------------------------- manifeste
$script:A3D_ManifestPath = $null
$script:A3D_Manifest = $null

function Initialize-A3DManifest([string]$Path) {
    $script:A3D_ManifestPath = $Path
    $m = Read-JsonFile $Path
    if ($null -eq $m) {
        $script:A3D_Manifest = [ordered]@{ schema = 'atelier3d-install-manifest/1'; created_at = (Get-Date).ToString('o'); steps = [ordered]@{}; components = [ordered]@{} }
    } else {
        $script:A3D_Manifest = ConvertTo-Hashtable $m
        if (-not $script:A3D_Manifest.Contains('steps')) { $script:A3D_Manifest['steps'] = [ordered]@{} }
        if (-not $script:A3D_Manifest.Contains('components')) { $script:A3D_Manifest['components'] = [ordered]@{} }
    }
}

function Save-A3DManifest {
    $script:A3D_Manifest['updated_at'] = (Get-Date).ToString('o')
    Write-JsonFile $script:A3D_ManifestPath $script:A3D_Manifest
}

function Get-StepState([string]$Name) {
    if ($script:A3D_Manifest['steps'].Contains($Name)) { return $script:A3D_Manifest['steps'][$Name] }
    return $null
}

function Set-StepState([string]$Name, [string]$Status, $Data = $null) {
    $entry = [ordered]@{ status = $Status; at = (Get-Date).ToString('o') }
    if ($null -ne $Data) { $entry['data'] = $Data }
    $script:A3D_Manifest['steps'][$Name] = $entry
    Save-A3DManifest
}

function Set-ComponentRecord([string]$Name, $Data) {
    $script:A3D_Manifest['components'][$Name] = $Data
    Save-A3DManifest
}

function Test-StepDone([string]$Name) {
    $s = Get-StepState $Name
    return ($null -ne $s -and $s['status'] -eq 'done')
}

# ---------------------------------------------------------------- réseau
function Get-CurlProxyArgs([string]$Url) {
    # 1) variables HTTPS_PROXY / HTTP_PROXY : curl les lit lui-même.
    if ($env:HTTPS_PROXY -or $env:https_proxy -or $env:HTTP_PROXY -or $env:http_proxy -or $env:ALL_PROXY) { return @() }
    # 2) proxy système Windows (Internet Options / WinHTTP) — validation TLS conservée.
    try {
        $sys = [System.Net.WebRequest]::GetSystemWebProxy()
        $uri = [Uri]$Url
        $p = $sys.GetProxy($uri)
        if ($p -and $p.AbsoluteUri -ne $uri.AbsoluteUri) { return @('--proxy', $p.AbsoluteUri, '--proxy-anyauth', '--proxy-user', ':') }
    } catch { }
    return @()
}

function Get-CurlExe {
    $c = Join-Path $env:SystemRoot 'System32\curl.exe'
    if (Test-Path -LiteralPath $c) { return $c }
    throw "curl.exe introuvable (présent par défaut depuis Windows 10 1803)."
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Invoke-A3DDownload {
    <#
      Télécharge $Url vers $Dest avec reprise (.part), vérifie taille et SHA-256.
      Retourne le chemin final. Ne journalise jamais d'en-tête d'authentification.
    #>
    param(
        [Parameter(Mandatory)] [string]$Url,
        [Parameter(Mandatory)] [string]$Dest,
        [long]$Size = 0,
        [string]$Sha256 = $null,
        [string]$Label = $null,
        [string]$BearerToken = $null,
        [int]$Attempts = 3
    )
    if (-not $Label) { $Label = Split-Path -Leaf $Dest }
    $dir = Split-Path -Parent $Dest
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

    if (Test-Path -LiteralPath $Dest) {
        $len = (Get-Item -LiteralPath $Dest).Length
        if (($Size -le 0 -or $len -eq $Size) -and (-not $Sha256 -or (Get-FileSha256 $Dest) -eq $Sha256.ToLowerInvariant())) {
            Write-Ok "$Label déjà présent et vérifié"
            return $Dest
        }
        Write-Warn "$Label présent mais non conforme : nouveau téléchargement"
        Remove-Item -LiteralPath $Dest -Force
    }

    $part = $Dest + '.part'
    $curl = Get-CurlExe
    for ($i = 1; $i -le $Attempts; $i++) {
        $complete = $false
        if (Test-Path -LiteralPath $part) {
            $have = (Get-Item -LiteralPath $part).Length
            if ($Size -gt 0 -and $have -gt $Size) { Remove-Item -LiteralPath $part -Force; $have = 0 }
            if ($Size -gt 0 -and $have -eq $Size) { $complete = $true; Write-Info ("{0} : téléchargement déjà complet, vérification" -f $Label) }
            elseif ($have -gt 0) { Write-Info ("Reprise de {0} à {1}" -f $Label, (Format-Bytes $have)) }
        }
        if (-not $complete) {
        Write-Info ("Téléchargement : {0} ({1}) depuis {2}" -f $Label, $(if ($Size -gt 0) { Format-Bytes $Size } else { 'taille inconnue' }), ([Uri]$Url).Host)
        Write-A3DLog 'DL' "GET $Url -> $Dest"
        $cargs = @('-L', '--fail', '--retry', '5', '--retry-delay', '3', '--connect-timeout', '30', '-C', '-', '-o', $part, '--progress-bar')
        $cargs += Get-CurlProxyArgs $Url
        if ($BearerToken) {
            # jeton passé par fichier de config temporaire pour ne pas apparaître dans la ligne de commande
            $cfg = [System.IO.Path]::GetTempFileName()
            [System.IO.File]::WriteAllText($cfg, ('header = "Authorization: Bearer {0}"' -f $BearerToken))
            $cargs += @('--config', $cfg)
        }
        $cargs += $Url
        & $curl @cargs
        $code = $LASTEXITCODE
        if ($BearerToken -and $cfg) { Remove-Item -LiteralPath $cfg -Force -ErrorAction SilentlyContinue }
        if ($code -eq 33) {
            # le serveur refuse la reprise : on repart de zéro une fois
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            continue
        }
        if ($code -ne 0) {
            Write-Warn "curl a échoué (code $code), tentative $i/$Attempts"
            if ($code -eq 22) {
                $status = ''
                try { $status = (& $curl -s -o NUL -w '%{http_code}' -I -L @(Get-CurlProxyArgs $Url) $Url) } catch {}
                if ($status -in @('401', '403')) { throw "Accès refusé ($status) à $Label : ressource protégée (compte ou acceptation de licence requis)." }
            }
            Start-Sleep -Seconds (3 * $i)
            continue
        }
        }
        $len = (Get-Item -LiteralPath $part).Length
        if ($Size -gt 0 -and $len -ne $Size) {
            Write-Warn ("Taille inattendue pour {0} : {1} au lieu de {2}" -f $Label, $len, $Size)
            if ($len -gt $Size) { Remove-Item -LiteralPath $part -Force }
            continue
        }
        if ($Sha256) {
            Write-Info "Vérification SHA-256 de $Label…"
            $h = Get-FileSha256 $part
            if ($h -ne $Sha256.ToLowerInvariant()) {
                Remove-Item -LiteralPath $part -Force
                Write-Warn "Empreinte SHA-256 incorrecte pour $Label (fichier supprimé)"
                continue
            }
        }
        Move-Item -LiteralPath $part -Destination $Dest -Force
        Write-Ok ("{0} téléchargé et vérifié" -f $Label)
        return $Dest
    }
    throw "Échec du téléchargement de $Label après $Attempts tentatives (relancez INSTALL.bat : la reprise conserve la partie déjà reçue)."
}

# ---------------------------------------------------------------- git blobs
function Get-GitBlobSha1([string]$Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $header = [System.Text.Encoding]::ASCII.GetBytes(('blob {0}' -f $bytes.Length) + [char]0)
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    $all = New-Object byte[] ($header.Length + $bytes.Length)
    [Array]::Copy($header, 0, $all, 0, $header.Length)
    [Array]::Copy($bytes, 0, $all, $header.Length, $bytes.Length)
    return (($sha1.ComputeHash($all) | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Install-PinnedRepo {
    <# Télécharge l'archive d'un commit figé et ne copie que les fichiers listés, chacun vérifié par SHA1 de blob git. #>
    param([Parameter(Mandatory)] $Spec, [Parameter(Mandatory)] [string]$Dest, [Parameter(Mandatory)] [string]$DownloadDir, [string]$Label)
    $zip = Join-Path $DownloadDir (($Spec.archive_root) + '.zip')
    Invoke-A3DDownload -Url $Spec.url -Dest $zip -Label $Label | Out-Null
    $tmp = Join-Path $DownloadDir ('extract_' + $Spec.commit.Substring(0, 12))
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $src = Join-Path $tmp $Spec.archive_root
    if (-not (Test-Path -LiteralPath $dest)) { New-Item -ItemType Directory -Force -Path $Dest | Out-Null }
    foreach ($p in $Spec.files.PSObject.Properties) {
        $rel = $p.Name
        $from = Join-Path $src ($rel -replace '/', '\')
        if (-not (Test-Path -LiteralPath $from)) { throw "Fichier attendu absent de l'archive : $rel" }
        $sha = Get-GitBlobSha1 $from
        if ($sha -ne $p.Value.git_blob_sha1) { throw "Empreinte git inattendue pour $rel ($sha)" }
        $to = Join-Path $Dest ($rel -replace '/', '\')
        $toDir = Split-Path -Parent $to
        if (-not (Test-Path -LiteralPath $toDir)) { New-Item -ItemType Directory -Force -Path $toDir | Out-Null }
        Copy-Item -LiteralPath $from -Destination $to -Force
    }
    Remove-Item -LiteralPath $tmp -Recurse -Force
    Write-Ok ("{0} installé au commit {1} ({2} fichiers vérifiés)" -f $Label, $Spec.commit.Substring(0, 12), @($Spec.files.PSObject.Properties).Count)
}

# ---------------------------------------------------------------- chemins
function Test-AsciiPath([string]$Path) {
    return ($Path -cmatch '^[\x20-\x7E]+$')
}

function Get-FreeBytes([string]$Path) {
    $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Path))
    $d = New-Object System.IO.DriveInfo($root)
    return $d.AvailableFreeSpace
}

function Invoke-Native {
    <# Exécute un programme, journalise sa sortie, lève une erreur lisible si le code retour est non nul. #>
    param([Parameter(Mandatory)] [string]$Exe, [string[]]$Arguments = @(), [string]$What = $null, [hashtable]$Env = @{})
    if (-not $What) { $What = Split-Path -Leaf $Exe }
    $saved = @{}
    foreach ($k in $Env.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process'); [Environment]::SetEnvironmentVariable($k, $Env[$k], 'Process') }
    try {
        Write-A3DLog 'EXEC' ("{0} {1}" -f $Exe, ($Arguments -join ' '))
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $out = & $Exe @Arguments 2>&1 | ForEach-Object { $s = "$_"; Write-A3DLog 'OUT' $s; $s }
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($code -ne 0) {
            $tail = ($out | Select-Object -Last 15) -join "`n"
            throw ("{0} a échoué (code {1}).`n{2}" -f $What, $code, (Protect-A3DText $tail))
        }
        return $out
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
}
