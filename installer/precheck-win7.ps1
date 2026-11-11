<# Run on the counter PC BEFORE installing the Windows 7 build.
   Read-only: it installs, changes and sends nothing. It answers:
     1. Can setup create "WhatsApp Printer"?  (XPS Document Writer driver present)
     2. Does the billing software print through Windows, so the printer
        dropdown is there?  (Windows program, or old DOS program on LPT1)
   and writes everything to whatsapp-printer-check.txt on the desktop.
   Written for the PowerShell 2.0 that ships with Windows 7.

   Run it from a normal Command Prompt:
     powershell -ExecutionPolicy Bypass -File precheck-win7.ps1
#>
param([string[]] $ShortcutFolders = @())

$ErrorActionPreference = 'Continue'
$report = New-Object System.Collections.ArrayList
function Say([string] $text = '') { [void]$report.Add($text); Write-Host $text }
function Heading([string] $text) { Say ''; Say ('== ' + $text + ' ==') }

# --- Windows -----------------------------------------------------------------
Heading 'Windows'
$os = Get-WmiObject Win32_OperatingSystem
Say ('{0}  version {1}  {2}  {3}' -f $os.Caption, $os.Version, $os.CSDVersion, $os.OSArchitecture)
Say ('PowerShell {0}' -f $PSVersionTable.PSVersion)
$v = [Environment]::OSVersion.Version
if ($v.Major -eq 6 -and $v.Minor -eq 1 -and $v.Build -ge 7601) {
    Say 'OK   Windows 7 SP1: use the win7-x86 installer.'
} elseif ($v.Major -ge 10) {
    Say 'NOTE This is Windows 10 or later: use the normal installer, not win7-x86.'
} else {
    Say 'FAIL Windows 7 without SP1 (or older): the installer needs SP1.'
}

# --- Print Spooler -------------------------------------------------------------
Heading 'Print Spooler'
$spooler = Get-Service -Name Spooler -ErrorAction SilentlyContinue
if ($spooler -and $spooler.Status -eq 'Running') {
    Say 'OK   Print Spooler is running.'
} else {
    Say ('FAIL Print Spooler is not running (status: {0}). No printer can be created.' -f $spooler.Status)
}

# --- 1. XPS Document Writer ---------------------------------------------------
Heading '1. Can setup create "WhatsApp Printer"?'
$drivers = @(Get-WmiObject Win32_PrinterDriver -ErrorAction SilentlyContinue |
    ForEach-Object { ($_.Name -split ',')[0] } | Sort-Object -Unique)
$xpsDriver = $drivers | Where-Object { $_ -like 'Microsoft XPS Document Writer*' } | Select-Object -First 1
if ($xpsDriver) {
    Say ('OK   Driver "{0}" is installed. Setup can create "WhatsApp Printer".' -f $xpsDriver)
} else {
    Say 'FAIL The Microsoft XPS Document Writer driver is NOT installed.'
    Say '     Setup would fall back to "save the receipt into a folder".'
    Say '     Fix: Control Panel > Programs > Turn Windows features on or off >'
    Say '          tick "XPS Services", OK, restart, then run this check again.'
}

$printers = @(Get-WmiObject Win32_Printer -ErrorAction SilentlyContinue)
$existing = $printers | Where-Object { $_.Name -eq 'WhatsApp Printer' }
if ($existing) {
    Say ('NOTE "WhatsApp Printer" already exists (driver {0}, port {1}).' -f $existing.DriverName, $existing.PortName)
}
if (Test-Path 'C:\ProgramData\WAPrinter') {
    Say 'NOTE C:\ProgramData\WAPrinter exists: an earlier version was installed here.'
}

# --- 2. Billing software --------------------------------------------------------
Heading 'Printers on this PC'
foreach ($p in $printers) {
    $mark = '  '
    if ($p.Default) { $mark = '* ' }
    Say ('{0}{1}   [driver: {2}]   [port: {3}]' -f $mark, $p.Name, $p.DriverName, $p.PortName)
}
Say '(* = default printer)'

Heading 'LPT redirection (old DOS programs print this way)'
$netUse = @(net use 2>$null | Where-Object { $_ -match 'LPT\d' })
if ($netUse.Count -gt 0) {
    foreach ($line in $netUse) { Say ('     ' + $line.Trim()) }
    Say 'NOTE An LPT port is redirected: a DOS program may be printing through it.'
} else {
    Say 'None.'
}
$dosRunning = Get-Process -Name ntvdm -ErrorAction SilentlyContinue
if ($dosRunning) { Say 'NOTE A DOS program is running right now (ntvdm.exe).' }

function Get-ProgramKind([string] $path) {
    $ext = [IO.Path]::GetExtension($path).ToLower()
    if ($ext -eq '.bat' -or $ext -eq '.cmd') { return 'batch file (look inside to see what it starts)' }
    if ($ext -eq '.pif') { return 'DOS program (.pif)' }
    if ($ext -ne '.exe' -and $ext -ne '.com') { return $null }
    if (-not (Test-Path $path)) { return 'target missing' }
    if ($ext -eq '.com') { return 'DOS program (.com)' }
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead($path)
        $reader = New-Object IO.BinaryReader($stream)
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) { return 'unknown' }
        $stream.Position = 0x3c
        $offset = $reader.ReadInt32()
        if ($offset -le 0 -or $offset + 6 -gt $stream.Length) { return 'DOS program' }
        $stream.Position = $offset
        $sig = $reader.ReadUInt16()
        if ($sig -eq 0x4550) {
            [void]$reader.ReadUInt16()
            $machine = $reader.ReadUInt16()
            if ($machine -eq 0x8664) { return 'Windows program (64-bit)' }
            return 'Windows program (32-bit)'
        }
        if ($sig -eq 0x454E) { return 'old 16-bit Windows program' }
        return 'DOS program'
    } catch {
        return ('could not read: ' + $_)
    } finally {
        if ($stream) { $stream.Close() }
    }
}

Heading '2. Programs on the desktop (is the billing software a Windows or DOS program?)'
$folders = @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:PUBLIC 'Desktop')) + $ShortcutFolders
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in $folders) {
    if (-not (Test-Path $folder)) { continue }
    foreach ($item in Get-ChildItem $folder -Filter *.lnk -ErrorAction SilentlyContinue) {
        $target = $shell.CreateShortcut($item.FullName).TargetPath
        if (-not $target) { continue }
        $kind = Get-ProgramKind $target
        if ($kind) { Say ('{0,-28} {1,-34} {2}' -f $item.BaseName, $kind, $target) }
    }
}
Say ''
Say 'Windows program -> it has a print dialog: pick "WhatsApp Printer" there.'
Say 'DOS program     -> no printer dropdown; it prints to LPT1. Send this report back first.'

# --- Save ----------------------------------------------------------------------
$out = Join-Path ([Environment]::GetFolderPath('Desktop')) 'whatsapp-printer-check.txt'
$report | Out-File -FilePath $out -Encoding UTF8
Write-Host ''
Write-Host ('Saved to ' + $out + ' -- nothing on this PC was changed.')
