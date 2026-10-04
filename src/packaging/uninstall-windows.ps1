<#
    Remove Snipwright from this computer (Windows 10/11).

    Double-click uninstall-windows.bat, or from a PowerShell window run:

        powershell -ExecutionPolicy Bypass -File uninstall-windows.ps1

    It undoes what install-windows.ps1 set up, and tidies away what Snipwright
    itself creates while it runs:
      - the "Snipwright" shortcuts in the Start menu and on the Desktop;
      - Snipwright's file-type registrations for .vprj and .swproj (all under
        HKEY_CURRENT_USER - VideoReDo's own .vprj registration is never
        touched);
      - the Watcher's start-on-login entry;
      - Snipwright's cache, and the small scratch files it leaves in the
        temporary folder;
      - the Python environment (.venv) the installer made;
      - the Windows Defender exclusion for that folder, if one was added
        (Windows asks for permission, as it did when it was added);
      - only if you say so: Quick Stream Fix working copies, and your settings.

    It never touches recordings, exported videos, project files or logs, and it
    leaves Python, FFmpeg and MKVToolNix installed - they are separate programs
    that other software may use.  Every question is asked before anything is
    removed, so stopping part-way through the questions changes nothing.

    The shortcuts, file types and start-on-login entry belong to whichever copy
    of Snipwright installed them last.  If they point at a different copy than
    this one, they are left alone, so removing an old copy cannot break the one
    you still use.

    A script cannot delete the folder it is running from, so the last step -
    deleting the Snipwright folder itself - is yours; it says which.
#>

# Native commands print to stderr in normal operation; carry on and report.
$ErrorActionPreference = "Continue"

function Section($t) { Write-Host "`n$t" -ForegroundColor Cyan }
function Info($t)    { Write-Host "  $t" }
function Warn($t)    { Write-Host "  $t" -ForegroundColor Yellow }
function Pause-Exit  { Write-Host ""; Read-Host "Press Enter to close" | Out-Null }

# Ask a yes/no question.  Anything other than a clear yes or no takes the
# default shown in capitals.
function Ask($question, $defaultYes) {
    $hint = if ($defaultYes) { "[Y/n]" } else { "[y/N]" }
    $answer = "$(Read-Host "  $question $hint")".Trim().ToLower()
    if ($answer -eq "y" -or $answer -eq "yes") { return $true }
    if ($answer -eq "n" -or $answer -eq "no")  { return $false }
    return [bool]$defaultYes
}

# Case-insensitive "does this text contain that text", without the wildcard
# surprises -like has when a path contains [ or ].
function Contains($text, $part) {
    if (-not $text -or -not $part) { return $false }
    return $text.IndexOf($part, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

$Here = $PSScriptRoot
if (-not $Here) { $Here = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Src     = Split-Path -Parent $Here
$Root    = Split-Path -Parent $Src
$Venv    = Join-Path $Root ".venv"
$MainPy  = Join-Path $Src "main.py"
$WatchPy = Join-Path $Src "watcher.py"

# Python's Path.home() - where Snipwright keeps its settings and cache.
$HomeDir    = [Environment]::GetFolderPath("UserProfile")
$Conf       = Join-Path $HomeDir ".config\snipwright"
$LegacyConf = Join-Path $HomeDir ".config\vrd-next"       # the earlier name
$Cache      = Join-Path $HomeDir ".cache\snipwright"
$Temp       = [IO.Path]::GetTempPath()
$Classes    = "HKCU:\Software\Classes"
$StartupDir = Join-Path $env:AppData "Microsoft\Windows\Start Menu\Programs\Startup"
$Shortcuts  = @(
    (Join-Path $env:AppData "Microsoft\Windows\Start Menu\Programs\Snipwright.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Snipwright.lnk")
)

$removed = New-Object System.Collections.Generic.List[string]

function Remove-Path($path, $label) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    Remove-Item -LiteralPath $path -Force -Recurse -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $path) { Warn "Could not remove $path" }
    else { $removed.Add($(if ($label) { $label } else { $path })) }
}

# The project folder another installation lives in, from a command line or
# shortcut argument that names its main.py or watcher.py.
function Get-OtherRoot($text) {
    if ($text -match '"?([^"]+)\\(main|watcher)\.py"?') {
        return Split-Path -Parent $Matches[1]
    }
    return "(unknown)"
}

# --- 1. what is here -------------------------------------------------------
Section "Uninstall Snipwright"
Info "This removes the copy of Snipwright in:"
Info "    $Root"
Info "Your recordings, exported videos, project files and logs are not touched."
Write-Host ""
if (-not (Ask "Continue?" $false)) {
    Info "Nothing has been changed."
    Pause-Exit; return
}

# --- 2. it must not be running ---------------------------------------------
# Removing the Python environment under a running editor or Watcher would pull
# it out from under them, and Windows would refuse to delete files they have
# open anyway.  Matched on this copy's own paths, so another copy of
# Snipwright running elsewhere does not count.
function Get-OurProcesses {
    try {
        Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop |
            Where-Object { (Contains $_.CommandLine $MainPy) -or (Contains $_.CommandLine $WatchPy) }
    } catch { @() }
}
while (@(Get-OurProcesses).Count -gt 0) {
    Section "Snipwright is still running"
    Info "Close Snipwright, and quit the Watcher (right-click its icon in the"
    Info "notification area and choose Quit)."
    $answer = "$(Read-Host "  Press Enter to check again, or type q to stop without changing anything")"
    if ($answer.Trim().ToLower() -eq "q") {
        Info "Nothing has been changed."
        Pause-Exit; return
    }
}

# --- 3. look before deleting anything --------------------------------------
$shell = New-Object -ComObject WScript.Shell

# Who owns the file-type registrations?  Both ProgIDs are written together,
# with this copy's main.py in their open command.
$regState = "none"; $regOther = ""
foreach ($progId in @("Snipwright.Project", "Snipwright.SwProject")) {
    $key = "$Classes\$progId\shell\open\command"
    if (Test-Path $key) {
        $cmd = (Get-ItemProperty -Path $key -ErrorAction SilentlyContinue).'(default)'
        if (Contains $cmd $MainPy) { $regState = "ours" }
        else { $regState = "other"; $regOther = Get-OtherRoot $cmd }
        break
    }
}

# Quick Stream Fix working copies, and where the logs are.  Read straight from
# the settings file rather than through Snipwright's own loader, which would
# recreate the settings folder if it were missing.  The test for "is this a
# working copy" IS Snipwright's own, so the uninstaller cannot disagree with
# the application about what is safe to delete.
$venvPy = Join-Path $Venv "Scripts\python.exe"
$pyCode = @'
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
settings, paths = {}, {}
try:
    data = json.loads((Path.home() / ".config" / "snipwright" / "config.json")
                      .read_text(encoding="utf-8"))
    settings = data.get("settings", {}) or {}
    paths = data.get("paths", {}) or {}
except Exception:
    pass
qsf = os.path.expanduser(str(settings.get("qsf_temp_dir", "") or "").strip())
if not (qsf and os.path.isdir(qsf)):
    qsf = tempfile.gettempdir()
from utils.qsf_temp import list_working_copies
print("QSFDIR\t" + qsf)
for path, size, _mtime in list_working_copies(qsf):
    print("QSF\t%d\t%s" % (size, path))
print("LOGDIR\t" + os.path.expanduser(str(paths.get("log_folder", "") or "").strip()))
'@
$qsfDir = ""; $logDir = ""; $qsfPaths = @(); [long]$qsfBytes = 0; $qsfChecked = $false
if (Test-Path -LiteralPath $venvPy) {
    $oldEncoding = [Console]::OutputEncoding
    $env:PYTHONIOENCODING = "utf-8"
    try {
        [Console]::OutputEncoding = [Text.Encoding]::UTF8
        $lines = $pyCode | & $venvPy - $Src 2>$null
    } finally {
        [Console]::OutputEncoding = $oldEncoding
    }
    foreach ($line in @($lines)) {
        $parts = "$line".Split("`t")
        switch ($parts[0]) {
            "QSFDIR" { $qsfDir = $parts[1]; $qsfChecked = $true }
            "QSF"    { $qsfBytes += [long]$parts[1]; $qsfPaths += $parts[2] }
            "LOGDIR" { $logDir = $parts[1] }
        }
    }
}

$deleteQsf = $false
if ($qsfPaths.Count -gt 0) {
    Section "Quick Stream Fix working copies"
    $size = if ($qsfBytes -ge 1GB) { "{0:N1} GB" -f ($qsfBytes / 1GB) } else { "{0:N0} MB" -f ($qsfBytes / 1MB) }
    $what = if ($qsfPaths.Count -eq 1) { "1 working copy" } else { "$($qsfPaths.Count) working copies" }
    Info "Found $what in $qsfDir, taking $size."
    Info "Snipwright makes these temporary repaired copies while editing -"
    Info "not your recordings, which are where you left them:"
    foreach ($p in ($qsfPaths | Select-Object -First 10)) { Info "    $(Split-Path -Leaf $p)" }
    if ($qsfPaths.Count -gt 10) { Info "    ...and $($qsfPaths.Count - 10) more" }
    Write-Host ""
    $deleteQsf = Ask "Delete them?" $true
} elseif (-not $qsfChecked) {
    Info "(Could not check for Quick Stream Fix working copies - the Python"
    Info "environment is already gone.)"
}

$deleteSettings = $false
if ((Test-Path -LiteralPath $Conf) -or (Test-Path -LiteralPath $LegacyConf)) {
    Section "Your settings"
    Info "Snipwright keeps your settings, output profiles, learned channel logos"
    Info "and break idents, the batch queue and the Watcher's settings in:"
    if (Test-Path -LiteralPath $Conf)       { Info "    $Conf" }
    if (Test-Path -LiteralPath $LegacyConf) { Info "    $LegacyConf   (from Snipwright's earlier name)" }
    Info "Keep them if you might reinstall - Snipwright picks them up again, so"
    Info "it will not have to relearn your channels.  Any log files in there"
    Info "are kept either way."
    if ($regState -eq "other") {
        Info "Another copy of Snipwright is installed, in"
        Info "    $regOther"
        Info "and it uses these settings too."
    }
    Write-Host ""
    $deleteSettings = Ask "Delete your settings?" $false
}

# The installer only ever OFFERED a Defender exclusion, and reading the list
# of exclusions needs administrator rights, so ask rather than guess.
$checkDefender = $false
$defenderOn = $false
if (Get-Command Remove-MpPreference -ErrorAction SilentlyContinue) {
    try { $defenderOn = [bool](Get-MpComputerStatus -ErrorAction Stop).AntivirusEnabled } catch { }
}
if ($defenderOn) {
    Section "Windows Defender"
    Info "If you let the installer exclude Snipwright's Python folder from"
    Info "Windows Defender, that exclusion should go too - otherwise Defender"
    Info "would keep skipping that folder, whatever is put there later."
    Info "Windows will ask for permission, and nothing changes if there is no"
    Info "exclusion to remove."
    Write-Host ""
    $checkDefender = Ask "Check for it and remove it?" $true
}

# --- 4. remove --------------------------------------------------------------
Section "Removing Snipwright"

foreach ($link in $Shortcuts) {
    if (-not (Test-Path -LiteralPath $link)) { continue }
    $target = ""
    try { $target = $shell.CreateShortcut($link).Arguments } catch { }
    if (Contains $target $MainPy) { Remove-Path $link }
    else {
        Info "Leaving $link alone: it starts the copy in"
        Info "    $(Get-OtherRoot $target)"
    }
}

if ($regState -eq "other") {
    Info "Leaving the .vprj and .swproj file types alone: they belong to the copy in"
    Info "    $regOther"
} else {
    $hadAny = [bool](@("Snipwright.Project", "Snipwright.SwProject", "Applications\snipwright.exe", ".swproj") |
        Where-Object { Test-Path "$Classes\$_" })
    foreach ($key in @("Snipwright.Project", "Snipwright.SwProject", "Applications\snipwright.exe")) {
        if (Test-Path "$Classes\$key") {
            Remove-Item -Path "$Classes\$key" -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # .vprj belongs to VideoReDo as much as to Snipwright: remove only
    # Snipwright's own entries from it, and its default ONLY if that default is
    # Snipwright's (the installer never sets it, but someone may have).
    $vprj = "$Classes\.vprj"
    if (Test-Path "$vprj\OpenWithProgids") {
        Remove-ItemProperty -Path "$vprj\OpenWithProgids" -Name "Snipwright.Project" -ErrorAction SilentlyContinue
    }
    if (Test-Path "$vprj\OpenWithList\snipwright.exe") {
        Remove-Item -Path "$vprj\OpenWithList\snipwright.exe" -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $vprj) {
        $default = (Get-ItemProperty -Path $vprj -ErrorAction SilentlyContinue).'(default)'
        if ($default -eq "Snipwright.Project") {
            Remove-ItemProperty -Path $vprj -Name "(default)" -ErrorAction SilentlyContinue
        }
    }
    # Tidy away anything now empty, so a PC without VideoReDo is left with no
    # trace of .vprj - and one with VideoReDo keeps everything of its own.
    foreach ($key in @("$vprj\OpenWithProgids", "$vprj\OpenWithList", $vprj)) {
        if (Test-Path $key) {
            $item = Get-Item -Path $key
            if ($item.SubKeyCount -eq 0 -and $item.ValueCount -eq 0) {
                Remove-Item -Path $key -Force -ErrorAction SilentlyContinue
            }
        }
    }

    # .swproj is Snipwright's own format: nothing else registers it.
    if (Test-Path "$Classes\.swproj") {
        Remove-Item -Path "$Classes\.swproj" -Recurse -Force -ErrorAction SilentlyContinue
    }

    $left = @("Snipwright.Project", "Snipwright.SwProject", "Applications\snipwright.exe", ".swproj") |
        Where-Object { Test-Path "$Classes\$_" }
    if ($left) { Warn "Could not remove every file-type registration: $($left -join ', ')" }
    elseif ($hadAny) { $removed.Add("Snipwright's .vprj and .swproj file types") }

    # Windows caches file-type icons; nudge it so the change shows at once.
    try { ie4uinit.exe -show 2>$null } catch { }
}

$startupCmd = Join-Path $StartupDir "snipwright-watcher.cmd"
if (Test-Path -LiteralPath $startupCmd) {
    $content = Get-Content -LiteralPath $startupCmd -Raw -ErrorAction SilentlyContinue
    if (Contains $content $WatchPy) { Remove-Path $startupCmd "The Watcher's start-on-login entry" }
    else {
        Info "Leaving the Watcher's start-on-login entry alone: it starts the copy in"
        Info "    $(Get-OtherRoot $content)"
    }
}
# Left by the Watcher under Snipwright's earlier name.  It can only start an
# installation that no longer exists, which is why the Watcher removes it too.
Remove-Path (Join-Path $StartupDir "vrd-next-watcher.cmd")

Remove-Path $Cache "$Cache (cache)"

# Snipwright's own scratch files in the temporary folder: the lock files that
# stop a second editor or Watcher starting, and the Joiner's editing project.
# These are shared by every copy of Snipwright you have, so a lock is removed
# only when the process that took it has gone - deleting a live one would let
# another copy start a second Watcher beside the first.  Qt writes the owner's
# process ID on the lock file's first line.
function Test-LockLive($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    $first = Get-Content -LiteralPath $path -TotalCount 1 -ErrorAction SilentlyContinue
    $pidValue = 0
    if (-not [int]::TryParse("$first", [ref]$pidValue)) { return $false }
    return [bool](Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
}
$watcherLock = Join-Path $Temp "snipwright-watcher.lock"
$editorLock  = Join-Path $Temp "snipwright-editor.lock"
if (-not (Test-LockLive $watcherLock)) { Remove-Path $watcherLock }
if (-not (Test-LockLive $editorLock)) {
    Remove-Path $editorLock
    # Only an editor can be using this, so it goes when no editor is open.
    Remove-Path (Join-Path $Temp "snipwright-joiner-edit.swproj")
}

if ($deleteQsf) {
    $gone = 0
    foreach ($p in $qsfPaths) {
        Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path -LiteralPath $p)) { $gone++ }
    }
    $removed.Add($(if ($gone -eq 1) { "1 Quick Stream Fix working copy" } else { "$gone Quick Stream Fix working copies" }))
}

$keptLogs = ""
if ($deleteSettings) {
    foreach ($dir in @($Conf, $LegacyConf)) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        # Everything except log files, which are kept as promised.
        Get-ChildItem -LiteralPath $dir -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -ne ".log" } |
            Remove-Item -Force -ErrorAction SilentlyContinue
        # Then any folders left empty, deepest first.
        Get-ChildItem -LiteralPath $dir -Recurse -Directory -Force -ErrorAction SilentlyContinue |
            Sort-Object { $_.FullName.Length } -Descending |
            Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force) } |
            Remove-Item -Force -ErrorAction SilentlyContinue
        if (-not (Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)) {
            Remove-Path $dir "$dir (settings)"
        } else {
            $removed.Add("your settings in $dir")
            $keptLogs = $dir
        }
    }
}

if ($checkDefender) {
    # Single quotes inside the path are doubled for PowerShell's own quoting.
    $quoted = $Venv -replace "'", "''"
    $command = "try { if ((Get-MpPreference).ExclusionPath -contains '$quoted') { Remove-MpPreference -ExclusionPath '$quoted' -ErrorAction Stop; exit 0 } else { exit 3 } } catch { exit 1 }"
    try {
        $proc = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command)
        switch ($proc.ExitCode) {
            0 { $removed.Add("The Windows Defender exclusion for $Venv") }
            3 { Info "There was no Windows Defender exclusion to remove." }
            default {
                Warn "Windows did not let the Defender exclusion be checked or removed."
                Warn "You can remove it yourself in Windows Security > Virus & threat"
                Warn "protection > Manage settings > Exclusions:  $Venv"
            }
        }
    } catch {
        Warn "Permission was not given, so the Defender exclusion (if there is one)"
        Warn "is still there.  You can remove it in Windows Security > Virus & threat"
        Warn "protection > Manage settings > Exclusions:  $Venv"
    }
}

# Only a folder that really is a Python environment is removed.  PySide6's
# deepest files can pass Windows' old 260-character path limit, which
# Remove-Item in Windows PowerShell trips over, so fall back to rd with the
# long-path prefix if anything is left.
if (Test-Path -LiteralPath (Join-Path $Venv "pyvenv.cfg")) {
    Remove-Item -LiteralPath $Venv -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $Venv) {
        cmd.exe /c "rd /s /q `"\\?\$Venv`"" 2>$null
    }
    if (Test-Path -LiteralPath $Venv) { Warn "Could not remove $Venv completely." }
    else { $removed.Add("$Venv (Python environment)") }
}

# --- 5. report --------------------------------------------------------------
Section "Done"
if ($removed.Count -eq 0) {
    Info "There was nothing left to remove."
} else {
    Info "Removed:"
    foreach ($r in $removed) { Info "    $r" }
}
if (-not $deleteSettings -and ((Test-Path -LiteralPath $Conf) -or (Test-Path -LiteralPath $LegacyConf))) {
    Write-Host ""
    Info "Your settings were kept, in $Conf."
}
if ($keptLogs) {
    Write-Host ""
    Info "Your log files were kept, in $keptLogs."
    Info "Delete that folder too if you don't want them."
}
if ($logDir -and (Test-Path -LiteralPath $logDir)) {
    Write-Host ""
    Info "Logs in your chosen log folder were left as they are:"
    Info "    $logDir"
}
Write-Host ""
Info "Last step - close this window, then delete the Snipwright folder itself:"
Info "    $Root"
Write-Host ""
Info "Python, FFmpeg and MKVToolNix are still installed: they are separate"
Info "programs that other software may use.  Remove them from Settings > Apps"
Info "if you know nothing else needs them."
Pause-Exit
