<#
.SYNOPSIS
    Creates (or removes) the "WhatsApp Printer" queue.

.DESCRIPTION
    The queue uses Microsoft's inbox "Microsoft Print To PDF" driver bound to
    Local Ports whose names are file paths. Windows then writes each print job
    straight to that path as a PDF, silently — no Save-As dialog, and no
    third-party print driver.

    That last point is the reason for this design. Microsoft is retiring
    third-party V3/V4 print drivers: since January 2026 new ones are no longer
    published to Windows Update, from July 2026 the inbox IPP driver is
    preferred, and Windows Protected Print mode uninstalls queues that depend on
    third-party drivers outright. A queue built on the inbox driver is unaffected
    by all of it, and needs no EV code-signing certificate.

    Several ports are created because a Local Port always writes to the same
    filename. With one port, two prints in quick succession would collide; the
    service round-robins across these and moves each file out as it lands.

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
$DriverName = 'Microsoft Print To PDF'

function Assert-Administrator {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated PowerShell session.'
    }
}

function Get-PortPaths {
    1..$PortCount | ForEach-Object { Join-Path $SpoolPath "job$_.pdf" }
}

function Install-WhatsAppPrinter {
    Write-Host "Creating spool folder $SpoolPath"
    New-Item -ItemType Directory -Path $SpoolPath -Force | Out-Null

    # Everyone who prints must be able to write here; the service reads and
    # deletes. Without this, printing from a standard user account fails
    # silently with an empty job.
    $acl  = Get-Acl $SpoolPath
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        'Users', 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -Path $SpoolPath -AclObject $acl

    if (-not (Get-PrinterDriver -Name $DriverName -ErrorAction SilentlyContinue)) {
        throw "The inbox driver '$DriverName' is not present. Enable the " +
              "'Microsoft Print to PDF' Windows feature and re-run."
    }

    $ports = Get-PortPaths
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
        Set-Printer -Name $PrinterName -PortName $ports[0] -DriverName $DriverName
    } else {
        Write-Host "Creating printer '$PrinterName'"
        Add-Printer -Name $PrinterName -DriverName $DriverName -PortName $ports[0]
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

function Uninstall-WhatsAppPrinter {
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

    Write-Host ''
    Write-Host 'Printer removed.' -ForegroundColor Green
    Write-Host "Job history and captured PDFs were left in $SpoolPath's parent folder."
    Write-Host 'Delete C:\ProgramData\WAPrinter by hand if you want them gone.'
}

Assert-Administrator
if ($Uninstall) { Uninstall-WhatsAppPrinter } else { Install-WhatsAppPrinter }
