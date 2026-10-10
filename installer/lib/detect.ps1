# Atelier 3D — détection matériel et installations existantes (lecture seule)

function Get-A3DGpuInfo {
    $smi = $null
    foreach ($c in @((Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'), 'C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe')) {
        if (Test-Path -LiteralPath $c) { $smi = $c; break }
    }
    if (-not $smi) { $cmd = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue; if ($cmd) { $smi = $cmd.Source } }
    $gpus = @()
    if ($smi) {
        try {
            $lines = & $smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv,noheader,nounits 2>$null
            foreach ($l in $lines) {
                $f = $l.Split(',') | ForEach-Object { $_.Trim() }
                if ($f.Count -ge 4) {
                    $gpus += [ordered]@{ vendor = 'NVIDIA'; name = $f[0]; vram_total_mb = [int]$f[1]; vram_free_mb = [int]$f[2]; driver = $f[3] }
                }
            }
            # génération de la carte (7.5 = Turing) : champ absent des anciens nvidia-smi, d'où une requête séparée
            $caps = @(& $smi --query-gpu=compute_cap --format=csv,noheader 2>$null)
            for ($i = 0; $i -lt [math]::Min($caps.Count, $gpus.Count); $i++) { if ("$($caps[$i])" -match '^\s*([0-9]+\.[0-9]+)\s*$') { $gpus[$i]['compute_cap'] = $Matches[1] } }
            $cudaLine = (& $smi 2>$null | Select-String -Pattern 'CUDA (UMD )?Version:\s*([0-9.]+)' | Select-Object -First 1)
            if ($cudaLine -and $gpus.Count -gt 0) { $gpus[0]['driver_cuda_max'] = $cudaLine.Matches[0].Groups[2].Value }
        } catch { }
    }
    if ($gpus.Count -eq 0) {
        try {
            Get-CimInstance Win32_VideoController | ForEach-Object {
                $gpus += [ordered]@{ vendor = $(if ($_.Name -match 'NVIDIA') { 'NVIDIA' } elseif ($_.Name -match 'AMD|Radeon') { 'AMD' } elseif ($_.Name -match 'Intel') { 'Intel' } else { 'autre' }); name = $_.Name; vram_total_mb = $null; vram_free_mb = $null; driver = $_.DriverVersion; note = 'nvidia-smi indisponible' }
            }
        } catch { }
    }
    return ,$gpus
}

function Get-A3DSystemInfo {
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    return [ordered]@{
        os = $os.Caption
        os_version = $os.Version
        arch = $os.OSArchitecture
        ram_total_mb = [int]($os.TotalVisibleMemorySize / 1024)
        ram_free_mb = [int]($os.FreePhysicalMemory / 1024)
        cpu = $cpu.Name.Trim()
        cpu_threads = $cpu.NumberOfLogicalProcessors
        powershell = $PSVersionTable.PSVersion.ToString()
        long_paths_enabled = $(try { (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled -eq 1 } catch { $false })
    }
}

function Get-A3DBlenderCandidates {
    $cands = New-Object System.Collections.Generic.List[string]
    $cmd = Get-Command blender.exe -ErrorAction SilentlyContinue
    if ($cmd) { $cands.Add($cmd.Source) }
    foreach ($base in @("$env:ProgramFiles\Blender Foundation", "${env:ProgramFiles(x86)}\Steam\steamapps\common\Blender")) {
        if (Test-Path -LiteralPath $base) {
            Get-ChildItem -LiteralPath $base -Recurse -Depth 2 -Filter blender.exe -ErrorAction SilentlyContinue | ForEach-Object { $cands.Add($_.FullName) }
        }
    }
    foreach ($root in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')) {
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($p -and ($p.PSObject.Properties.Name -contains 'DisplayName') -and $p.DisplayName -match '^Blender' -and ($p.PSObject.Properties.Name -contains 'InstallLocation') -and $p.InstallLocation) {
                $exe = Join-Path $p.InstallLocation 'blender.exe'
                if (Test-Path -LiteralPath $exe) { $cands.Add($exe) }
            }
        }
    }
    # dossiers « X:\Blender » fréquents pour les versions portables
    foreach ($d in [System.IO.DriveInfo]::GetDrives()) {
        if ($d.DriveType -ne 'Fixed' -or -not $d.IsReady) { continue }
        foreach ($sub in @('Blender', 'Program Files\Blender Foundation')) {
            $dir = Join-Path $d.RootDirectory.FullName $sub
            if (Test-Path -LiteralPath $dir) {
                Get-ChildItem -LiteralPath $dir -Recurse -Depth 2 -Filter blender.exe -ErrorAction SilentlyContinue | ForEach-Object { $cands.Add($_.FullName) }
            }
        }
    }
    $out = @()
    foreach ($exe in ($cands | Select-Object -Unique)) {
        try {
            $v = & $exe --factory-startup --version 2>$null | Select-Object -First 1
            if ($v -match 'Blender\s+([0-9]+\.[0-9]+(\.[0-9]+)?)') {
                $out += [ordered]@{ path = $exe; version = $Matches[1]; banner = "$v".Trim() }
            }
        } catch { }
    }
    return ,$out
}

function Find-A3DComfyInstalls {
    <# Recherche bornée (profondeur et durée) des installations ComfyUI existantes, sans rien modifier. #>
    param([int]$MaxDepth = 5, [int]$BudgetSeconds = 45, [string[]]$ExtraRoots = @())
    $skip = '^(Windows|\$Recycle\.Bin|System Volume Information|ProgramData|Program Files|Program Files \(x86\)|WindowsApps|node_modules|\.git|site-packages|__pycache__|Lib|SteamLibrary|steamapps|XboxGames|WpSystem|WUDownloadCache)$'
    $skipDot = '^\.(git|cache|vscode|idea|npm|nuget|gradle|android|cargo|rustup|conda|venv|tox|mypy_cache|pytest_cache|next|svn|hg)$'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $found = New-Object System.Collections.Generic.List[string]
    $roots = @()
    foreach ($d in [System.IO.DriveInfo]::GetDrives()) { if ($d.DriveType -eq 'Fixed' -and $d.IsReady) { $roots += $d.RootDirectory.FullName } }
    $roots += @("$env:USERPROFILE", "$env:USERPROFILE\Documents", "$env:USERPROFILE\Desktop", "$env:LOCALAPPDATA\Programs") + $ExtraRoots
    $queue = New-Object System.Collections.Generic.Queue[object]
    foreach ($r in ($roots | Select-Object -Unique)) { if (Test-Path -LiteralPath $r) { $queue.Enqueue(@($r, 0)) } }
    $seen = @{}
    while ($queue.Count -gt 0 -and $sw.Elapsed.TotalSeconds -lt $BudgetSeconds) {
        $item = $queue.Dequeue(); $dir = $item[0]; $depth = $item[1]
        if ($seen.ContainsKey($dir)) { continue }; $seen[$dir] = $true
        if (Test-Path -LiteralPath (Join-Path $dir 'main.py')) {
            if ((Test-Path -LiteralPath (Join-Path $dir 'comfy')) -and (Test-Path -LiteralPath (Join-Path $dir 'nodes.py'))) { $found.Add($dir); continue }
        }
        if ($depth -ge $MaxDepth) { continue }
        try {
            foreach ($sub in [System.IO.Directory]::EnumerateDirectories($dir)) {
                $name = [System.IO.Path]::GetFileName($sub)
                if ($name -match $skip -or $name -match $skipDot) { continue }
                $queue.Enqueue(@($sub, $depth + 1))
            }
        } catch { }
    }
    $result = @()
    foreach ($comfy in ($found | Select-Object -Unique)) {
        $info = [ordered]@{ comfy_dir = $comfy; version = $null; python = $null; models_dir = (Join-Path $comfy 'models'); has_minimax_h3 = $false; has_mesh3d = $false; has_krea2 = $false; launcher = $null }
        $vf = Join-Path $comfy 'comfyui_version.py'
        if (Test-Path -LiteralPath $vf) { $m = Select-String -LiteralPath $vf -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1; if ($m) { $info.version = $m.Matches[0].Groups[1].Value } }
        $parent = Split-Path -Parent $comfy
        $emb = Join-Path $parent 'python_embeded\python.exe'
        $venv = Join-Path $comfy 'venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $emb) { $info.python = $emb } elseif (Test-Path -LiteralPath $venv) { $info.python = $venv }
        $info.has_minimax_h3 = Test-Path -LiteralPath (Join-Path $comfy 'comfy_extras\nodes_minimax_h3.py')
        $info.has_mesh3d = Test-Path -LiteralPath (Join-Path $comfy 'comfy_extras\nodes_trellis2.py')
        $bat = Join-Path $parent 'run_nvidia_gpu.bat'
        if (Test-Path -LiteralPath $bat) { $info.launcher = $bat }
        $mm = @()
        $dm = Join-Path $comfy 'models\diffusion_models'
        if (Test-Path -LiteralPath $dm) { $mm = Get-ChildItem -LiteralPath $dm -File -Filter '*minimax_h3*' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name } }
        $info['minimax_h3_models'] = @($mm)
        $result += $info
    }
    return ,$result
}

function Find-A3DExistingModel {
    <# Cherche un fichier modèle identique (taille puis SHA-256) dans les dossiers models des installations détectées. #>
    param([Parameter(Mandatory)] $Entry, [string[]]$ModelDirs)
    foreach ($md in $ModelDirs) {
        $cand = Join-Path (Join-Path $md $Entry.comfy_folder) $Entry.filename
        if (Test-Path -LiteralPath $cand) {
            $len = (Get-Item -LiteralPath $cand).Length
            if ($len -eq [long]$Entry.size) {
                if ((Get-FileSha256 $cand) -eq $Entry.sha256) { return $cand }
            }
        }
    }
    return $null
}
