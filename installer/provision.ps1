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
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

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

function Get-XpsDriverName {
    # Windows 7 calls it "Microsoft XPS Document Writer"; 8 and later ship a v4
    # driver under a longer name. Ask the spooler rather than hardcoding either.
    $drivers = @(Get-WmiObject -Class Win32_PrinterDriver -ErrorAction SilentlyContinue |
        ForEach-Object { ($_.Name -split ',')[0] })
    return ($drivers | Where-Object { $_ -like 'Microsoft XPS Document Writer*' } |
        Select-Object -First 1)
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
           "and that the XPS Document Writer is installed.")
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
    if ($existing) {
        Write-Host "Printer '$PrinterName' already exists; repointing it."
        $existing.DriverName = $driver
        $existing.PortName   = $ports[0]
        $existing.Put() | Out-Null
    } else {
        Write-Host "Creating printer '$PrinterName'"
        New-LegacyPrinter -Driver $driver -Port $ports[0]
    }

    Write-Host ''
    Write-Host "Done. '$PrinterName' is now in the Windows print dialog." -ForegroundColor Green
}

function Install-ModernQueue {
    if (-not (Get-PrinterDriver -Name $PdfDriver -ErrorAction SilentlyContinue)) {
        throw "The inbox driver '$PdfDriver' is not present. Enable the " +
              "'Microsoft Print to PDF' Windows feature and re-run."
    }

    $ports = @(Get-PortPaths)
    foreach ($port in $ports) {
        if (Get-PrinterPort -Name $port -ErrorAction SilentlyContinue) {
            Write-Host "Port already exists: $port"
        } else {
            Write-Host "Creating port: $port"
            Add-PrinterPort -Name $port
        }
    }

    if (Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue) {
        Write-Host "Printer '$PrinterName' already exists; repointing it."
        Set-Printer -Name $PrinterName -PortName $ports[0] -DriverName $PdfDriver
    } else {
        Write-Host "Creating printer '$PrinterName'"
        Add-Printer -Name $PrinterName -DriverName $PdfDriver -PortName $ports[0]
    }

    # Print directly rather than holding jobs in the queue, so files reach the
    # spool folder as soon as rendering finishes.
    Set-PrintConfiguration -PrinterName $PrinterName -PaperSize A4 -ErrorAction SilentlyContinue
    Set-Printer -Name $PrinterName -KeepPrintedJobs $false -ErrorAction SilentlyContinue

    Write-Host 'Enabling the PrintService operational log (for job titles)'
    try {
        wevtutil sl Microsoft-Windows-PrintService/Operational /enabled:true | Out-Null
    } catch {
        Write-Warning "Could not enable the PrintService log: $_"
        Write-Warning 'Capture still works; jobs will just have no document title.'
    }

    Write-Host ''
    Write-Host "Done. '$PrinterName' is now in the Windows print dialog." -ForegroundColor Green
}

function Install-WhatsAppPrinter {
    New-SpoolFolder
    if ($Legacy) { Install-LegacyQueue } else { Install-ModernQueue }
}

function Uninstall-LegacyQueue {
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
    if ($Legacy) { Uninstall-LegacyQueue } else { Uninstall-ModernQueue }

    Write-Host ''
    Write-Host 'Printer removed.' -ForegroundColor Green
    Write-Host "Job history and captured PDFs were left in $SpoolPath's parent folder."
    Write-Host 'Delete C:\ProgramData\WAPrinter by hand if you want them gone.'
}

Assert-Administrator
if ($Uninstall) { Uninstall-WhatsAppPrinter } else { Install-WhatsAppPrinter }
