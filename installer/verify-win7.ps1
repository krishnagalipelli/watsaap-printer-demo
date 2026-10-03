<# Run on a clean Windows 7 SP1 VM after installing the win7-x86 build.
   Startup and OCR only: no customer data, credentials or sending. #>
param([string] $InstallDir = '')
$ErrorActionPreference = 'Stop'
$version = [Environment]::OSVersion.Version
if ($version.Major -ne 6 -or $version.Minor -ne 1 -or $version.Build -lt 7601) {
    throw 'Run this check on Windows 7 SP1, not on a modern Windows build machine.'
}
if (-not $InstallDir) {
    $base = $env:ProgramFiles
    if (${env:ProgramFiles(x86)}) { $base = ${env:ProgramFiles(x86)} }
    $InstallDir = Join-Path $base 'WhatsAppPrinter'
}
foreach ($folder in @($InstallDir, (Join-Path $InstallDir 'cli'))) {
    foreach ($dll in @('python3.dll', 'python38.dll', 'msvcp140.dll', 'ucrtbase.dll', 'api-ms-win-crt-runtime-l1-1-0.dll', 'vcruntime140.dll')) {
        if (-not (Test-Path (Join-Path $folder $dll))) { throw "Missing runtime file: $folder\$dll" }
        $reader = New-Object System.IO.BinaryReader([System.IO.File]::OpenRead((Join-Path $folder $dll)))
        try {
            $reader.BaseStream.Position = 0x3c
            $offset = $reader.ReadInt32()
            $reader.BaseStream.Position = $offset
            if ($reader.ReadUInt32() -ne 0x00004550 -or $reader.ReadUInt16() -ne 0x014c) {
                throw "Runtime is not an x86 PE file: $folder\$dll"
            }
        } finally { $reader.Close() }
    }
}
$previousHome = $env:WAPRINTER_HOME
try {
    $env:WAPRINTER_HOME = Join-Path $env:TEMP ('waprinter-check-' + [Guid]::NewGuid().ToString('N'))
    & (Join-Path $InstallDir 'cli\waprinter.exe') --help
    if ($LASTEXITCODE -ne 0) { throw 'CLI startup failed.' }
    $result = Start-Process (Join-Path $InstallDir 'waprinter-agent.exe') -ArgumentList '--selftest --check-ocr' -Wait -PassThru
    if ($result.ExitCode -ne 0) { throw "Startup/OCR failed. Check $env:WAPRINTER_HOME\logs\crash.txt" }
    Write-Host 'Windows 7 startup and raster OCR passed. No messages were sent.'
    Write-Host 'Now check PDF folder capture and each template in test mode.'
} finally {
    $env:WAPRINTER_HOME = $previousHome
}
