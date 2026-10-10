# Outil de développement : réencode les scripts PowerShell en UTF-8 avec BOM
# (Windows PowerShell 5.1 lit les fichiers sans BOM en ANSI, ce qui casse les accents).
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object { $_.Extension -eq '.ps1' -and $_.FullName -notmatch '\\node_modules\\' } | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if (-not $hasBom) {
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        [System.IO.File]::WriteAllText($_.FullName, $text, (New-Object System.Text.UTF8Encoding($true)))
        Write-Host "BOM ajouté : $($_.FullName)"
    }
}
