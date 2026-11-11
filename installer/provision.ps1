<#
.SYNOPSIS
    Creates (or removes) the "WhatsApp Printer" queue.

.DESCRIPTION
    The queue uses an inbox Windows print driver bound to Local Ports whose
    names are file paths. Windows then writes each print job straight to that
    path, silently -- no Save-As dialog, and no third-party print driver.

    That last point is the reason for this design. Microsoft is retiring
    third-party V3/V4 print drivers: since January 2026 new ones are no longer
    published to Windows Update, from July 2026 the inbox IPP driver is
    preferred, and Windows Protected Print mode uninstalls queues that depend on
    third-party drivers outright. A queue built on the inbox driver is unaffected
    by all of it, and needs no EV code-signing certificate.

    Which inbox driver depends on the Windows version:

      Windows 10 and later  Microsoft Print To PDF, writing job<n>.pdf.
      Windows 7 and 8       Microsoft XPS Document Writer, writing job<n>.xps.
                            There is no inbox PDF driver before Windows 10, and
                            no PrintManagement cmdlets either, so the port is
                            created through the Local Port monitor and the queue
                            through WMI -- both of which Windows 7 has. The
                            agent converts each XPS as it is captured; see
                            src/waprinter/capture/xps.py.

    Several ports are created because a Local Port always writes to the same
    filename. With one port, two prints in quick succession would collide; the
    agent round-robins across these and moves each file out as it lands.

    Windows 10 and 11 normally use the PrintManagement cmdlets (Get-Printer,
    Add-Printer). Those sit on a WMI provider in root\StandardCimv2 that can be
    unregistered on an otherwise healthy machine -- every cmdlet then fails with
    "Invalid namespace" while printing itself works. This first re-registers the
    provider from Windows' own MOF, and if that does not bring it back, builds
    the PDF queue by the Windows 7 route instead, which uses neither.

    If the XPS Document Writer has been turned off on a Windows 7 machine, no
    printer can be created and this falls back to PDF folder capture: the agent
    reads any PDF exported into the spool folder.

.PARAMETER Uninstall
    Remove the printer, its ports, and the spool folder.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File provision.ps1
    powershell -ExecutionPolicy Bypass -File provision.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string] $PrinterName = 'WhatsApp Printer',
    [string] $SpoolPath   = 'C:\ProgramData\WAPrinter\spool',
    [int]    $PortCount   = 4,
    [string] $LogFolder   = 'C:\ProgramData\WAPrinter\logs',
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

# Setup runs this hidden, so whatever it printed was lost and a failure reached
# the counter as one fixed sentence that named the wrong cause. Everything goes
# to provision.log, and the reason for a failure to provision-error.txt, which
# setup reads back and shows.
$LogFile   = Join-Path $LogFolder 'provision.log'
$ErrorFile = Join-Path $LogFolder 'provision-error.txt'

# Windows 7 and 8 report 6.x. Everything that differs between the two queues
# hangs off this one test.
$Legacy        = [Environment]::OSVersion.Version.Major -lt 10
$PortExtension = if ($Legacy) { 'xps' } else { 'pdf' }
$PdfDriver     = 'Microsoft Print To PDF'
$LocalPortKey  = 'HKLM:\SYSTEM\CurrentControlSet\Control\Print\Monitors\Local Port\Ports'

function Assert-Administrator {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated PowerShell session.'
    }
}

function Get-PortPaths {
    1..$PortCount | ForEach-Object { Join-Path $SpoolPath "job$_.$PortExtension" }
}

function Get-WmiDriverName {
    param([string] $Like)

    # Win32_PrinterDriver names read "Microsoft Print To PDF,4,Windows x64".
    # Asked through root\cimv2, so it answers even when the PrintManagement
    # provider does not.
    $drivers = @(Get-WmiObject -Class Win32_PrinterDriver -ErrorAction SilentlyContinue |
        ForEach-Object { ($_.Name -split ',')[0] })
    return ($drivers | Where-Object { $_ -like $Like } | Select-Object -First 1)
}

function Get-XpsDriverName {
    # Windows 7 calls it "Microsoft XPS Document Writer"; 8 and later ship a v4
    # driver under a longer name. Ask the spooler rather than hardcoding either.
    return (Get-WmiDriverName -Like 'Microsoft XPS Document Writer*')
}

function Test-PrintManagement {
    # Get-Printer with -ErrorAction SilentlyContinue returns nothing both when
    # there is no such printer and when the provider is missing, which is how
    # a broken provider read as "Microsoft Print To PDF is not present".
    try {
        Get-Printer -ErrorAction Stop | Out-Null
        return $true
    } catch {
        Write-Warning "The PrintManagement cmdlets do not work here ($_)"
        return $false
    }
}

function Repair-PrintManagement {
    # Re-registers the provider from the MOF Windows itself ships. It changes
    # nothing else, and is the documented fix for "Invalid namespace" from
    # Get-Printer.
    $wbem = Join-Path $env:SystemRoot 'System32\wbem'
    $mof  = Join-Path $wbem 'PrintManagementProvider.mof'
    if (-not (Test-Path $mof)) {
        Write-Warning "$mof is missing, so the provider cannot be re-registered."
        return $false
    }
    Write-Host "Re-registering the PrintManagement provider from $mof"
    try {
        & (Join-Path $wbem 'mofcomp.exe') $mof 2>&1 | ForEach-Object { Write-Host "  $_" }
        $mfl = Join-Path $wbem (Join-Path ([Globalization.CultureInfo]::CurrentUICulture.Name) 'PrintManagementProvider.mfl')
        if (Test-Path $mfl) {
            & (Join-Path $wbem 'mofcomp.exe') $mfl 2>&1 | ForEach-Object { Write-Host "  $_" }
        }
    } catch {
        Write-Warning "mofcomp failed: $_"
        return $false
    }
    return (Test-PrintManagement)
}

function Add-SpoolerType {
    if ('WAPrinter.Spooler' -as [type]) { return }
    # XcvData on the Local Port monitor is how a port is added without the
    # PrintManagement cmdlets, which Windows 7 does not have.
    # No -UsingNamespace here. Add-Type's own template already opens
    # System.Runtime.InteropServices, and naming it again emits the using
    # twice; the .NET 3.5 compiler on Windows 7 treats that as an error, the
    # type never compiles, and every port quietly falls back to the registry.
    Add-Type -Namespace 'WAPrinter' -Name 'Spooler' -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct PrinterDefaults {
    public IntPtr pDatatype;
    public IntPtr pDevMode;
    public int DesiredAccess;
}

[DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool OpenPrinter(string pPrinterName, out IntPtr phPrinter,
                                      ref PrinterDefaults pDefault);

[DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool XcvDataW(IntPtr hXcv, string pszDataName, byte[] pInputData,
                                   uint cbInputData, IntPtr pOutputData, uint cbOutputData,
                                   out uint pcbOutputNeeded, out uint pdwStatus);

[DllImport("winspool.drv", SetLastError = true)]
public static extern bool ClosePrinter(IntPtr hPrinter);
'@
}

function Invoke-LocalPortMonitor {
    param([ValidateSet('AddPort', 'DeletePort')] [string] $Action, [string] $Port)

    Add-SpoolerType
    $defaults = New-Object 'WAPrinter.Spooler+PrinterDefaults'
    $defaults.DesiredAccess = 1   # SERVER_ACCESS_ADMINISTER
    $handle = [IntPtr]::Zero
    if (-not [WAPrinter.Spooler]::OpenPrinter(',XcvMonitor Local Port', [ref] $handle, [ref] $defaults)) {
        $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "Could not open the Local Port monitor (error $code)."
    }
    try {
        $bytes = [Text.Encoding]::Unicode.GetBytes($Port + [char] 0)
        $needed = 0
        $status = 0
        $ok = [WAPrinter.Spooler]::XcvDataW($handle, $Action, $bytes, $bytes.Length,
                                           [IntPtr]::Zero, 0, [ref] $needed, [ref] $status)
        # 183 is ERROR_ALREADY_EXISTS on add, and a missing port on delete is
        # equally not a problem.
        if (-not $ok -or ($status -ne 0 -and $status -ne 183)) {
            throw "$Action returned status $status"
        }
    } finally {
        [WAPrinter.Spooler]::ClosePrinter($handle) | Out-Null
    }
}

function Test-LocalPort {
    param([string] $Port)

    # Both routes land in the monitor's own list, so this verifies either.
    $ports = Get-ItemProperty -Path $LocalPortKey -ErrorAction SilentlyContinue
    if (-not $ports) { return $false }
    return ($null -ne $ports.PSObject.Properties[$Port])
}

function Add-LocalPort {
    param([string] $Port)

    # True when the monitor took it. False means the caller should fall back,
    # and the fallback is batched: it restarts the spooler, and doing that
    # once per port takes far longer than the whole install should.
    try {
        Invoke-LocalPortMonitor -Action AddPort -Port $Port
        Write-Host "Created port: $Port"
        return $true
    } catch {
        Write-Warning "The Local Port monitor refused $Port ($_)"
        return $false
    }
}

function Add-LocalPortsViaRegistry {
    param([string[]] $Ports)

    # The monitor keeps its port list here, and the spooler rereads it on
    # restart. Slower than XcvData, but it needs no compiler and no P/Invoke.
    if (-not (Test-Path $LocalPortKey)) { New-Item -Path $LocalPortKey -Force | Out-Null }
    foreach ($port in $Ports) {
        New-ItemProperty -Path $LocalPortKey -Name $port -PropertyType String -Value '' -Force | Out-Null
    }
    Write-Host "Restarting the spooler to pick up $($Ports.Count) port(s)"
    Restart-Service -Name Spooler -Force
    # The spooler takes a moment to come back before it will accept a printer
    # bound to one of these.
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        if ((Get-Service -Name Spooler).Status -eq 'Running') { break }
        Start-Sleep -Seconds 1
    }
    Start-Sleep -Seconds 2
}

function Remove-LocalPort {
    param([string] $Port)

    try {
        Invoke-LocalPortMonitor -Action DeletePort -Port $Port
        Write-Host "Removed port: $Port"
    } catch {
        Write-Warning "Could not remove $Port ($_)"
    }
    if (Test-Path $LocalPortKey) {
        Remove-ItemProperty -Path $LocalPortKey -Name $Port -ErrorAction SilentlyContinue
    }
}

function New-SpoolFolder {
    Write-Host "Creating spool folder $SpoolPath"
    New-Item -ItemType Directory -Path $SpoolPath -Force | Out-Null

    # Everyone who prints must be able to write here; the agent reads and
    # deletes. Without this, printing from a standard user account fails
    # silently with an empty job.
    $acl  = Get-Acl $SpoolPath
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        'Users', 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -Path $SpoolPath -AclObject $acl
}

function New-LegacyPrinter {
    param([string] $Driver, [string] $Port)

    try {
        $printer = ([WMIClass] 'Win32_Printer').CreateInstance()
        $printer.DeviceID   = $PrinterName
        $printer.DriverName = $Driver
        $printer.PortName   = $Port
        $printer.Put() | Out-Null
        return
    } catch {
        Write-Warning "WMI could not create the queue ($_); trying prnmngr.vbs."
    }

    # Microsoft's own printer admin script, shipped with Windows 7. It does the
    # same job through a different path, and is worth trying before giving up
    # on a machine where WMI reports nothing more useful than "Generic failure".
    $script = Get-ChildItem (Join-Path $env:SystemRoot 'System32\Printing_Admin_Scripts') `
        -Filter 'prnmngr.vbs' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($script) {
        & cscript.exe //nologo $script.FullName -a -p $PrinterName -m $Driver -r $Port
        if ($LASTEXITCODE -eq 0) { return }
        Write-Warning "prnmngr.vbs exited with $LASTEXITCODE"
    } else {
        Write-Warning 'prnmngr.vbs was not found on this machine.'
    }

    throw ("The queue '$PrinterName' could not be created on driver '$Driver' " +
           "and port '$Port'. Check that the Print Spooler service is running " +
           "and that the driver is installed.")
}

function Install-LegacyQueue {
    # Windows 7 and 8: the XPS Document Writer, created through WMI because
    # Add-Printer does not exist here.
    $driver = Get-XpsDriverName
    if (-not $driver) {
        Write-Warning 'The Microsoft XPS Document Writer is not installed, so no'
        Write-Warning 'printer can be created. Turn on the "XPS Services" Windows'
        Write-Warning 'feature and run setup again for a printer in the print dialog.'
        Write-Host "PDF folder capture ready instead: $SpoolPath"
        return
    }
    Install-ClassicQueue -Driver $driver
}

function Install-ClassicPdfQueue {
    # Windows 10 and 11 whose PrintManagement provider is broken: the same PDF
    # queue as Install-ModernQueue, made the way Windows 7 makes one.
    $driver = Get-WmiDriverName -Like $PdfDriver
    if (-not $driver) {
        throw ("The driver '$PdfDriver' is not installed. Turn on 'Microsoft " +
               "Print to PDF' in Windows Features (optionalfeatures.exe) and run " +
               "setup again.")
    }
    Install-ClassicQueue -Driver $driver
}

function Install-ClassicQueue {
    # Ports through the Local Port monitor, the queue through Win32_Printer:
    # neither needs the PrintManagement cmdlets.
    param([string] $Driver)

    $driver = $Driver
    Write-Host "Using the inbox driver '$driver'"

    $ports = @(Get-PortPaths)
    $pending = @($ports | Where-Object { -not (Add-LocalPort $_) })
    if ($pending.Count) {
        Add-LocalPortsViaRegistry $pending
        # Verify rather than assume, but only the ones written by hand: the
        # monitor reporting success is authority enough for the rest, and a
        # registry read that disagreed would fail a working install. A printer
        # bound to a port the spooler does not know about fails with "Generic
        # failure", which says nothing about why.
        $missing = @($pending | Where-Object { -not (Test-LocalPort $_) })
        if ($missing.Count) {
            throw ("These printer ports could not be created, so the queue " +
                   "cannot be bound to them: " + ($missing -join ', '))
        }
    }

    $existing = Get-WmiObject -Class Win32_Printer -Filter "Name='$PrinterName'" -ErrorAction SilentlyContinue
    if ($existing -and $existing.DriverName -eq $driver -and $existing.PortName -eq $ports[0]) {
        Write-Host "Printer '$PrinterName' is already set up on $($ports[0])"
    } elseif ($existing) {
        Write-Host "Printer '$PrinterName' already exists; repointing it."
        $existing.DriverName = $driver
        $existing.PortName   = $ports[0]
        $existing.Put() | Out-Null
    } else {
        Write-Host "Creating printer '$PrinterName'"
        New-LegacyPrinter -Driver $driver -Port $ports[0]
    }

    if (-not $Legacy) { Enable-JobTitleLog }

    Write-Host ''
    Write-Host "Done. '$PrinterName' is now in the Windows print dialog." -ForegroundColor Green
}

function Enable-JobTitleLog {
    Write-Host 'Enabling the PrintService operational log (for job titles)'
    try {
        wevtutil sl Microsoft-Windows-PrintService/Operational /enabled:true | Out-Null
    } catch {
        Write-Warning "Could not enable the PrintService log: $_"
        Write-Warning 'Capture still works; jobs will just have no document title.'
    }
}

function Start-Spooler {
    # Every route below goes through the spooler. Stopped, the driver check
    # finds nothing and setup blamed Microsoft Print to PDF for it.
    $service = Get-Service -Name Spooler
    if ($service.Status -eq 'Running') { return }
    $mode = (Get-WmiObject -Class Win32_Service -Filter "Name='Spooler'").StartMode
    if ($mode -eq 'Disabled') {
        throw ("The Print Spooler service is disabled, so no printer can be " +
               "added. Set it to Automatic in services.msc and run setup again.")
    }
    Write-Host "The Print Spooler is $($service.Status); starting it"
    Start-Service -Name Spooler
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        if ((Get-Service -Name Spooler).Status -eq 'Running') { Start-Sleep -Seconds 2; return }
        Start-Sleep -Seconds 1
    }
    throw 'The Print Spooler service would not start. Restart Windows and run setup again.'
}

function Install-PdfDriver {
    if (Get-PrinterDriver -Name $PdfDriver -ErrorAction SilentlyContinue) { return }

    # The driver package stays in the driver store when the queue that used
    # it is deleted, so adding it back is quick and usually all that is needed.
    Write-Host "The driver '$PdfDriver' is not installed; adding it from the driver store"
    try { Add-PrinterDriver -Name $PdfDriver } catch { Write-Warning "Add-PrinterDriver: $_" }
    if (Get-PrinterDriver -Name $PdfDriver -ErrorAction SilentlyContinue) { return }

    Write-Host "Turning on the 'Microsoft Print to PDF' Windows feature"
    try {
        Enable-WindowsOptionalFeature -Online -FeatureName 'Printing-PrintToPDFServices-Features' `
            -All -NoRestart | Out-Null
    } catch {
        Write-Warning "Enable-WindowsOptionalFeature: $_"
    }
    if (Get-PrinterDriver -Name $PdfDriver -ErrorAction SilentlyContinue) { return }

    throw ("The driver '$PdfDriver' is not installed and could not be added. Turn " +
           "on 'Microsoft Print to PDF' in Windows Features (optionalfeatures.exe) " +
           "and run setup again.")
}

function Clear-QueuedJobs {
    $jobs = @(Get-PrintJob -PrinterName $PrinterName -ErrorAction SilentlyContinue)
    if (-not $jobs.Count) { return }
    Write-Warning ("Removing $($jobs.Count) print job(s) stuck in '$PrinterName'; " +
                   'print those receipts again once setup finishes.')
    foreach ($job in $jobs) {
        try { Remove-PrintJob -InputObject $job } catch { Write-Warning "Remove-PrintJob: $_" }
    }
    Start-Sleep -Seconds 2
}

function Reset-ModernPrinter {
    param([string] $Port)

    # Installing over an earlier version lands here. A queue holding a job the
    # spooler cannot finish refuses to change port or driver, so clear it and
    # try again, and as a last resort build the queue afresh.
    try {
        Set-Printer -Name $PrinterName -PortName $Port -DriverName $PdfDriver
        return
    } catch {
        Write-Warning "Could not repoint '$PrinterName' ($_)"
    }
    Clear-QueuedJobs
    try {
        Set-Printer -Name $PrinterName -PortName $Port -DriverName $PdfDriver
        return
    } catch {
        Write-Warning "Still could not repoint it ($_); creating it again"
    }
    Remove-Printer -Name $PrinterName
    Add-Printer -Name $PrinterName -DriverName $PdfDriver -PortName $Port
}

function Install-ModernQueue {
    Install-PdfDriver

    $ports = @(Get-PortPaths)
    foreach ($port in $ports) {
        if (Get-PrinterPort -Name $port -ErrorAction SilentlyContinue) {
            Write-Host "Port already exists: $port"
        } else {
            Write-Host "Creating port: $port"
            Add-PrinterPort -Name $port
        }
    }

    $printer = Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue
    if (-not $printer) {
        Write-Host "Creating printer '$PrinterName'"
        Add-Printer -Name $PrinterName -DriverName $PdfDriver -PortName $ports[0]
    } elseif ($printer.DriverName -eq $PdfDriver -and $printer.PortName -eq $ports[0]) {
        # The usual upgrade. Touching a queue that is already right only gives
        # it a chance to fail.
        Write-Host "Printer '$PrinterName' is already set up on $($ports[0])"
    } else {
        Write-Host ("Printer '$PrinterName' is on '$($printer.DriverName)' / " +
                    "'$($printer.PortName)'; repointing it.")
        Reset-ModernPrinter -Port $ports[0]
    }

    # Print directly rather than holding jobs in the queue, so files reach the
    # spool folder as soon as rendering finishes.
    # Preferences only: the queue works without them, so neither is a reason
    # to fail setup. A CIM error is terminating whatever -ErrorAction says.
    try { Set-PrintConfiguration -PrinterName $PrinterName -PaperSize A4 } catch {
        Write-Warning "Could not set A4 paper: $_"
    }
    try { Set-Printer -Name $PrinterName -KeepPrintedJobs $false } catch {
        Write-Warning "Could not turn off keeping printed jobs: $_"
    }

    Enable-JobTitleLog

    Write-Host ''
    Write-Host "Done. '$PrinterName' is now in the Windows print dialog." -ForegroundColor Green
}

function Install-WhatsAppPrinter {
    Start-Spooler
    New-SpoolFolder
    if ($Legacy) {
        Install-LegacyQueue
    } elseif ((Test-PrintManagement) -or (Repair-PrintManagement)) {
        Install-ModernQueue
    } else {
        Write-Warning 'Creating the queue without the PrintManagement cmdlets.'
        Install-ClassicPdfQueue
    }
}

function Uninstall-ClassicQueue {
    $existing = Get-WmiObject -Class Win32_Printer -Filter "Name='$PrinterName'" -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing printer '$PrinterName'"
        $existing.Delete() | Out-Null
    }
    foreach ($port in (Get-PortPaths)) { Remove-LocalPort $port }
}

function Uninstall-ModernQueue {
    if (Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue) {
        Write-Host "Removing printer '$PrinterName'"
        Remove-Printer -Name $PrinterName
    }

    foreach ($port in Get-PortPaths) {
        if (Get-PrinterPort -Name $port -ErrorAction SilentlyContinue) {
            Write-Host "Removing port: $port"
            # The spooler can hold a port briefly after the printer goes.
            for ($attempt = 1; $attempt -le 5; $attempt++) {
                try {
                    Remove-PrinterPort -Name $port
                    break
                } catch {
                    if ($attempt -eq 5) { Write-Warning "Could not remove $port : $_" }
                    Start-Sleep -Seconds 2
                }
            }
        }
    }
}

function Uninstall-WhatsAppPrinter {
    if ($Legacy -or -not (Test-PrintManagement)) { Uninstall-ClassicQueue } else { Uninstall-ModernQueue }

    Write-Host ''
    Write-Host 'Printer removed.' -ForegroundColor Green
    Write-Host "Job history and captured PDFs were left in $SpoolPath's parent folder."
    Write-Host 'Delete C:\ProgramData\WAPrinter by hand if you want them gone.'
}

$transcribing = $false
try {
    New-Item -ItemType Directory -Path $LogFolder -Force | Out-Null
    Remove-Item -Path $ErrorFile -Force -ErrorAction SilentlyContinue
    Start-Transcript -Path $LogFile -Append -Force | Out-Null
    $transcribing = $true
} catch {
    Write-Warning "No log this time: $_"
}

$exitCode = 0
try {
    Assert-Administrator
    if ($Uninstall) { Uninstall-WhatsAppPrinter } else { Install-WhatsAppPrinter }
} catch {
    $exitCode = 1
    $reason = "$($_.Exception.Message)".Trim()
    Write-Host "FAILED: $reason" -ForegroundColor Red
    Write-Host ($_.InvocationInfo.PositionMessage)
    # ASCII: setup reads this back as a plain string.
    try { Set-Content -Path $ErrorFile -Value $reason -Encoding ASCII } catch { }
}

if ($transcribing) { try { Stop-Transcript | Out-Null } catch { } }
exit $exitCode
