# Atelier 3D — choix du dossier et des composants à installer.
# Fenêtre à cases à cocher (Windows Forms, fournie avec Windows) ; menu texte en secours (session sans bureau, erreur).
# Chaque entrée du catalogue : key, title, detail, note (avertissement), requires (clés), excludes (clés).
# $Measure : scriptblock (clés) -> @{ download = octets ; disk = octets } ; le pseudo-composant « _base » (socle toujours
# installé) est ajouté pour le total. $FreeOf : scriptblock (dossier) -> octets libres.
# Gestionnaires d'événements sans GetNewClosure : ils s'exécutent pendant ShowDialog, dans la portée de la fonction
# (variables et fonctions du script visibles), ce qui ne serait pas le cas dans une fermeture (module dynamique).

function Format-A3DSize([double]$Bytes) {
    if ($Bytes -le 0) { return '—' }
    if ($Bytes -ge 1GB) { return ('{0:N1} Go' -f ($Bytes / 1GB)) }
    return ('{0:N0} Mo' -f [math]::Max(1, $Bytes / 1MB))
}

function Get-A3DComponentSizeText($Item, [scriptblock]$Measure) {
    <# Téléchargement propre à un composant : ce qu'il ajoute à ses prérequis (« + »), sinon sa taille seule. #>
    $req = @(Get-A3DProp $Item 'requires' | Where-Object { $_ })
    $one = & $Measure (@($Item.key) + $req)
    $dl = [double]$one.download
    if ($req.Count) { $dl -= [double](& $Measure $req).download }
    if ($dl -le 0) { return 'aucun téléchargement' }
    return $(if ($req.Count) { '+' } else { '' }) + (Format-A3DSize $dl)
}

function Resolve-A3DSelection {
    <# Applique les dépendances (requires) et exclusions (excludes) après avoir coché/décoché $Changed. #>
    param($Catalog, [string[]]$Keys, [string]$Changed, [bool]$Checked)
    $set = New-Object System.Collections.Generic.List[string]
    foreach ($k in $Keys) { if ($k -and -not $set.Contains($k)) { $set.Add($k) } }
    $byKey = @{}; foreach ($c in $Catalog) { $byKey[$c.key] = $c }
    if ($Checked) {
        $todo = New-Object System.Collections.Generic.Queue[string]; $todo.Enqueue($Changed)
        while ($todo.Count) {
            $k = $todo.Dequeue()
            if (-not $set.Contains($k)) { $set.Add($k) }
            foreach ($x in @(Get-A3DProp $byKey[$k] 'excludes')) { if ($x) { [void]$set.Remove($x) } }
            foreach ($r in @(Get-A3DProp $byKey[$k] 'requires')) { if ($r -and -not $set.Contains($r)) { $todo.Enqueue($r) } }
        }
    } else {
        [void]$set.Remove($Changed)
    }
    # une exclusion ou un décochage peut retirer le prérequis d'une autre entrée cochée : on retire aussi celle-ci
    $again = $true  # (pas « $changed » : PowerShell ignore la casse et écraserait le paramètre $Changed)
    while ($again) {
        $again = $false
        foreach ($k in @($set)) {
            foreach ($r in @(Get-A3DProp $byKey[$k] 'requires')) { if ($r -and -not $set.Contains($r)) { [void]$set.Remove($k); $again = $true } }
        }
    }
    return @($Catalog | Where-Object { $set.Contains($_.key) } | ForEach-Object { $_.key })
}

function Show-A3DComponentDialog {
    param($Catalog, [string[]]$Selected, [string]$InstallDir, [bool]$DirLocked, [string]$Hardware, [string[]]$Warnings,
          [scriptblock]$Measure, [scriptblock]$FreeOf)
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    try {
        Add-Type -Namespace A3D -Name Dpi -MemberDefinition '[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -ErrorAction Stop
    } catch { }
    try { [void][A3D.Dpi]::SetProcessDPIAware() } catch { }
    [System.Windows.Forms.Application]::EnableVisualStyles()

    # tailles en pixels mises à l'échelle de l'affichage (125 %, 150 %…) ; les polices (en points) suivent seules
    $S = 1.0
    try { $g = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero); $S = [math]::Max(1.0, $g.DpiX / 96.0); $g.Dispose() } catch { }
    $W = [int](660 * $S)
    $grey = [System.Drawing.Color]::FromArgb(96, 96, 104)
    $warnColor = [System.Drawing.Color]::FromArgb(176, 72, 0)
    $font = New-Object System.Drawing.Font('Segoe UI', 9)
    $bold = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold)

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Atelier 3D — installation'
    $form.Font = $font
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.AutoScaleMode = 'None'
    $form.AutoSize = $true
    $form.AutoSizeMode = 'GrowAndShrink'
    $form.Padding = New-Object System.Windows.Forms.Padding(14)
    $form.BackColor = [System.Drawing.Color]::White

    $root = New-Object System.Windows.Forms.FlowLayoutPanel
    $root.FlowDirection = 'TopDown'
    $root.WrapContents = $false
    $root.AutoSize = $true
    $root.AutoSizeMode = 'GrowAndShrink'
    $form.Controls.Add($root)

    function New-Label([string]$Text, $Font = $null, $Color = $null, [int]$Indent = 0, [int]$Top = 0) {
        $l = New-Object System.Windows.Forms.Label
        $l.Text = $Text
        $l.AutoSize = $true
        $ind = [int]($Indent * $S)
        $l.MaximumSize = New-Object System.Drawing.Size(($W - $ind), 0)
        $l.Margin = New-Object System.Windows.Forms.Padding($ind, [int]($Top * $S), 0, 2)
        if ($Font) { $l.Font = $Font }
        if ($Color) { $l.ForeColor = $Color }
        return $l
    }

    $root.Controls.Add((New-Label 'Choisissez ce qu''il faut installer' (New-Object System.Drawing.Font('Segoe UI', 12, [System.Drawing.FontStyle]::Bold))))
    $root.Controls.Add((New-Label ('Cochez seulement ce dont vous avez besoin : une partie décochée n''est ni téléchargée ni installée. ' +
            'Tout le reste est automatique (aucune installation manuelle). Relancer INSTALL.bat plus tard permet d''ajouter une partie.') $null $grey))
    $root.Controls.Add((New-Label $Hardware $null $grey 0 4))
    foreach ($w in @($Warnings)) { if ($w) { $root.Controls.Add((New-Label ('⚠ ' + $w) $null $warnColor 0 2)) } }

    # dossier
    $root.Controls.Add((New-Label 'Dossier d''installation (logiciels, modèles, vos données)' $bold $null 0 10))
    $dirRow = New-Object System.Windows.Forms.FlowLayoutPanel
    $dirRow.AutoSize = $true
    $dirRow.WrapContents = $false
    $dirRow.Margin = New-Object System.Windows.Forms.Padding(0)
    $dirBox = New-Object System.Windows.Forms.TextBox
    $dirBox.Text = $InstallDir
    $dirBox.Width = $W - [int](110 * $S)
    $dirBox.ReadOnly = $DirLocked
    $dirRow.Controls.Add($dirBox)
    $browse = New-Object System.Windows.Forms.Button
    $browse.Text = 'Parcourir…'
    $browse.AutoSize = $true
    $browse.Enabled = -not $DirLocked
    $dirRow.Controls.Add($browse)
    $root.Controls.Add($dirRow)
    if ($DirLocked) { $root.Controls.Add((New-Label 'Installation existante : les parties déjà installées sont conservées, cochez celles à ajouter.' $null $grey)) }

    # composants
    $root.Controls.Add((New-Label 'Composants' $bold $null 0 10))
    $list = New-Object System.Windows.Forms.FlowLayoutPanel
    $list.FlowDirection = 'TopDown'
    $list.WrapContents = $false
    $list.AutoSize = $true
    $list.AutoSizeMode = 'GrowAndShrink'
    $list.Margin = New-Object System.Windows.Forms.Padding(0)
    $root.Controls.Add($list)
    $boxes = @{}
    $sizeLabels = @{}
    foreach ($c in $Catalog) {
        $row = New-Object System.Windows.Forms.TableLayoutPanel
        $row.ColumnCount = 2
        $row.RowCount = 1
        $row.AutoSize = $false
        $row.Width = $W
        $row.Height = [int](24 * $S)
        $row.Margin = New-Object System.Windows.Forms.Padding(0, [int](6 * $S), 0, 0)
        [void]$row.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
        [void]$row.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::AutoSize)))
        $cb = New-Object System.Windows.Forms.CheckBox
        $cb.Text = $c.title
        $cb.Font = $bold
        $cb.AutoSize = $true
        $cb.Tag = $c.key
        $cb.Checked = ($Selected -contains $c.key)
        $row.Controls.Add($cb, 0, 0)
        $sz = New-Object System.Windows.Forms.Label
        $sz.AutoSize = $true
        $sz.ForeColor = $grey
        $sz.Anchor = 'Right'
        $row.Controls.Add($sz, 1, 0)
        $list.Controls.Add($row)
        $list.Controls.Add((New-Label $c.detail $null $grey 20))
        $note = Get-A3DProp $c 'note'
        if ($note) { $list.Controls.Add((New-Label ('⚠ ' + $note) $null $warnColor 20)) }
        $boxes[$c.key] = $cb
        $sizeLabels[$c.key] = $sz
    }

    $total = New-Label '' $bold $null 0 12
    $root.Controls.Add($total)
    $buttons = New-Object System.Windows.Forms.FlowLayoutPanel
    $buttons.FlowDirection = 'RightToLeft'
    $buttons.Width = $W
    $buttons.Height = [int](40 * $S)
    $buttons.Margin = New-Object System.Windows.Forms.Padding(0, 10, 0, 0)
    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = 'Installer'
    $ok.AutoSize = $true
    $ok.Padding = New-Object System.Windows.Forms.Padding(12, 2, 12, 2)
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = 'Annuler'
    $cancel.AutoSize = $true
    $cancel.DialogResult = 'Cancel'
    $buttons.Controls.Add($ok)
    $buttons.Controls.Add($cancel)
    $root.Controls.Add($buttons)
    $form.AcceptButton = $ok
    $form.CancelButton = $cancel

    $state = @{ busy = $false; need = 0.0; free = 0.0 }
    $current = { @($Catalog | Where-Object { $boxes[$_.key].Checked } | ForEach-Object { $_.key }) }
    $refresh = {
        $keys = & $current
        foreach ($c in $Catalog) { $sizeLabels[$c.key].Text = Get-A3DComponentSizeText $c $Measure }
        $m = & $Measure (@($keys) + '_base')
        $free = 0.0
        try { $free = [double](& $FreeOf $dirBox.Text) } catch { $free = 0.0 }
        $state.need = [double]$m.disk * 1.1
        $state.free = $free
        $total.Text = ('À télécharger : {0}   ·   espace disque nécessaire : {1}   ·   libre : {2}' -f (Format-A3DSize $m.download), (Format-A3DSize $m.disk), $(if ($free -gt 0) { Format-A3DSize $free } else { 'dossier invalide' }))
        $total.ForeColor = if ($free -gt 0 -and $free -ge $state.need) { [System.Drawing.Color]::FromArgb(20, 90, 40) } else { $warnColor }
    }
    foreach ($cb in $boxes.Values) {
        $cb.Add_CheckedChanged({
            param($sender, $e)
            if ($state.busy) { return }
            $state.busy = $true
            try {
                $keys = Resolve-A3DSelection -Catalog $Catalog -Keys (& $current) -Changed $sender.Tag -Checked $sender.Checked
                foreach ($k in $boxes.Keys) { $boxes[$k].Checked = ($keys -contains $k) }
                & $refresh
            } catch {
                Write-Warn ("Fenêtre de choix : {0}" -f $_.Exception.Message)
            } finally { $state.busy = $false }
        })
    }
    $dirBox.Add_TextChanged({ try { & $refresh } catch { Write-Warn ("Fenêtre de choix : {0}" -f $_.Exception.Message) } })
    $browse.Add_Click({
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = 'Dossier où installer Atelier 3D (un sous-dossier « Atelier3D » est créé si vous choisissez la racine d''un disque)'
        if ($dlg.ShowDialog($form) -eq 'OK') {
            $p = $dlg.SelectedPath
            if ([IO.Path]::GetPathRoot($p).TrimEnd('\') -eq $p.TrimEnd('\')) { $p = Join-Path $p 'Atelier3D' }
            $dirBox.Text = $p
        }
    })
    $ok.Add_Click({
        $p = $dirBox.Text.Trim()
        if (-not $p -or -not [IO.Path]::IsPathRooted($p)) {
            [void][System.Windows.Forms.MessageBox]::Show($form, 'Indiquez un dossier complet, par exemple D:\Atelier3D.', 'Atelier 3D', 'OK', 'Warning')
            return
        }
        if ($state.free -lt $state.need) {
            $msg = 'Espace libre insuffisant : {0} nécessaires (marge de 10 % comprise), {1} libres. Décochez des composants ou choisissez un autre disque.' -f (Format-A3DSize $state.need), (Format-A3DSize $state.free)
            [void][System.Windows.Forms.MessageBox]::Show($form, $msg, 'Atelier 3D', 'OK', 'Warning')
            return
        }
        $form.DialogResult = 'OK'
        $form.Close()
    })

    # outil de test : capture de la fenêtre puis validation automatique (aucun effet en usage normal)
    if ($env:A3D_MENU_SNAPSHOT) {
        $timer = New-Object System.Windows.Forms.Timer
        $timer.Interval = 400
        $timer.Add_Tick({
          $timer.Stop()
          try {
            $bmp = New-Object System.Drawing.Bitmap($form.Width, $form.Height)
            $form.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
            $bmp.Save($env:A3D_MENU_SNAPSHOT, [System.Drawing.Imaging.ImageFormat]::Png)
            $bmp.Dispose()
            if ($env:A3D_MENU_TOGGLE) { foreach ($k in $env:A3D_MENU_TOGGLE.Split(',')) { if ($boxes.ContainsKey($k)) { $boxes[$k].Checked = -not $boxes[$k].Checked } } }
            if ($env:A3D_MENU_SNAPSHOT2) {
                $bmp = New-Object System.Drawing.Bitmap($form.Width, $form.Height)
                $form.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
                $bmp.Save($env:A3D_MENU_SNAPSHOT2, [System.Drawing.Imaging.ImageFormat]::Png)
                $bmp.Dispose()
            }
            if ($env:A3D_MENU_AUTOCLICK -eq 'ok') { $form.DialogResult = 'OK' } else { $form.DialogResult = 'Cancel' }
          } catch {
            Write-Host ("[test menu] " + ($_ | Out-String))
            $form.DialogResult = 'Cancel'
          }
          $form.Close()
        })
        $form.Add_Shown({ $timer.Start() })
    }

    & $refresh
    # écran trop petit (portable en 125 %, 150 %…) : la liste des composants défile au lieu de dépasser de l'écran
    $screenH = [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position).WorkingArea.Height
    if ($env:A3D_MENU_MAXH) { $screenH = [int]$env:A3D_MENU_MAXH }
    $over = $form.PreferredSize.Height - ($screenH - [int](30 * $S))
    if ($over -gt 0) {
        $h = $list.PreferredSize.Height
        $list.AutoSize = $false
        $list.AutoScroll = $true
        $list.Width = $W + [System.Windows.Forms.SystemInformation]::VerticalScrollBarWidth + [int](4 * $S)
        $list.Height = [int][math]::Max(160 * $S, $h - $over)
    }
    $res = $form.ShowDialog()
    $out = $null
    if ($res -eq 'OK') { $out = @{ dir = $dirBox.Text.Trim(); components = @(& $current) } }
    $form.Dispose()
    return $out
}

function Show-A3DComponentConsole {
    param($Catalog, [string[]]$Selected, [string]$InstallDir, [bool]$DirLocked, [scriptblock]$Measure, [scriptblock]$FreeOf)
    $sel = @($Selected)
    $dir = $InstallDir
    while ($true) {
        Write-Host ''
        Write-Host '  Composants à installer (une partie décochée n''est ni téléchargée ni installée) :' -ForegroundColor Cyan
        $i = 0
        foreach ($c in $Catalog) {
            $i++
            Write-Host ('   {0,2}. [{1}] {2}  ({3})' -f $i, $(if ($sel -contains $c.key) { 'x' } else { ' ' }), $c.title, (Get-A3DComponentSizeText $c $Measure))
            $note = Get-A3DProp $c 'note'
            if ($note) { Write-Host ('          ⚠ ' + $note) -ForegroundColor Yellow }
        }
        $m = & $Measure (@($sel) + '_base')
        $free = 0.0; try { $free = [double](& $FreeOf $dir) } catch { }
        Write-Host ('  Dossier : {0}' -f $dir)
        Write-Host ('  À télécharger : {0} ; espace nécessaire : {1} ; libre : {2}' -f (Format-A3DSize $m.download), (Format-A3DSize $m.disk), (Format-A3DSize $free))
        $hint = if ($DirLocked) { 'numéro = cocher/décocher, Entrée = installer, Q = quitter' } else { 'numéro = cocher/décocher, D = changer de dossier, Entrée = installer, Q = quitter' }
        $a = Read-Host ('  Choix ({0})' -f $hint)
        if ([string]::IsNullOrWhiteSpace($a)) {
            if ($free -lt $m.disk * 1.1) { Write-Host '  Espace libre insuffisant : décochez des composants ou changez de dossier.' -ForegroundColor Yellow; continue }
            return @{ dir = $dir; components = $sel }
        }
        $a = $a.Trim()
        if ($a -match '^[qQ]$') { return $null }
        if ($a -match '^[dD]$' -and -not $DirLocked) {
            $n = Read-Host ('  Nouveau dossier [{0}]' -f $dir)
            if (-not [string]::IsNullOrWhiteSpace($n) -and [IO.Path]::IsPathRooted($n.Trim())) { $dir = $n.Trim() }
            continue
        }
        $n = 0
        if ([int]::TryParse($a, [ref]$n) -and $n -ge 1 -and $n -le $Catalog.Count) {
            $k = $Catalog[$n - 1].key
            $sel = Resolve-A3DSelection -Catalog $Catalog -Keys $sel -Changed $k -Checked (-not ($sel -contains $k))
        }
    }
}

function Select-A3DComponents {
    <# Fenêtre si possible, sinon menu texte. Retourne @{ dir ; components } ou $null si l'utilisateur annule. #>
    param($Catalog, [string[]]$Selected, [string]$InstallDir, [bool]$DirLocked, [string]$Hardware, [string[]]$Warnings,
          [scriptblock]$Measure, [scriptblock]$FreeOf)
    if (-not $env:A3D_NO_GUI -and [Environment]::UserInteractive) {
        try {
            return (Show-A3DComponentDialog -Catalog $Catalog -Selected $Selected -InstallDir $InstallDir -DirLocked $DirLocked -Hardware $Hardware `
                    -Warnings $Warnings -Measure $Measure -FreeOf $FreeOf)
        } catch {
            Write-Warn ("Fenêtre de choix indisponible ({0}) : menu texte." -f $_.Exception.Message)
        }
    }
    foreach ($w in @($Warnings)) { if ($w) { Write-Warn $w } }
    return (Show-A3DComponentConsole -Catalog $Catalog -Selected $Selected -InstallDir $InstallDir -DirLocked $DirLocked -Measure $Measure -FreeOf $FreeOf)
}
