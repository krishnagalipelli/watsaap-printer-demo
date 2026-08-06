<#
.SYNOPSIS
    Freeze the agent and compile the installer.

.DESCRIPTION
    Run on Windows with Python 3.12 and Inno Setup installed. Produces
    installer\Output\WhatsAppPrinter-Setup-<version>.exe.

    One executable is built, not two. Earlier versions had a Windows service for
    the spool watcher plus a tray app for the UI; that split cannot work now that
    the flow depends on a window appearing when someone prints, because a service
    runs in session 0 and has no desktop. Everything lives in the agent, which
    starts at logon in the user's own session.

.PARAMETER TesseractDir
    An existing Tesseract-OCR install to bundle, for invoices that print as an
    image. Install the UB Mannheim build once on this machine.

.PARAMETER SkipOcr
    Build without OCR. Scanned pages will be held with an explanation.
#>

[CmdletBinding()]
param(
    [string] $Python       = 'py -3.12',
    [string] $ISCC         = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    [string] $TesseractDir = 'C:\Program Files\Tesseract-OCR',
    [switch] $SkipOcr
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

function Invoke-Step {
    param([string] $Command)
    Write-Host "> $Command" -ForegroundColor DarkGray
    & cmd /c $Command
    if ($LASTEXITCODE -ne 0) { throw "Failed: $Command" }
}

function Copy-Tesseract {
    $vendor = Join-Path $root 'installer\vendor\tesseract'
    if (Test-Path $vendor) { Remove-Item -Recurse -Force $vendor }
    New-Item -ItemType Directory -Path "$vendor\tessdata" -Force | Out-Null

    if ($SkipOcr) {
        Write-Warning 'Building without OCR; scanned invoices will be held.'
        return
    }
    if (-not (Test-Path $TesseractDir)) {
        throw "Tesseract not found at $TesseractDir. Install it, or pass " +
              "-TesseractDir <path> / -SkipOcr."
    }

    Write-Host "== Staging Tesseract from $TesseractDir ==" -ForegroundColor Cyan
    Copy-Item "$TesseractDir\*.exe" $vendor -Force
    Copy-Item "$TesseractDir\*.dll" $vendor -Force -ErrorAction SilentlyContinue
    # English only. The full tessdata set is several hundred MB and Indian
    # invoices are printed in English regardless of the business's language.
    foreach ($file in 'eng.traineddata', 'osd.traineddata') {
        $source = Join-Path $TesseractDir "tessdata\$file"
        if (Test-Path $source) {
            Copy-Item $source "$vendor\tessdata" -Force
        } else {
            Write-Warning "Missing $file - OCR quality will suffer."
        }
    }
}

try {
    Write-Host '== Installing build dependencies ==' -ForegroundColor Cyan
    Invoke-Step "$Python -m pip install --upgrade pip pyinstaller"
    Invoke-Step "$Python -m pip install -e `".[dev,windows]`""

    if ($env:CI) {
        Write-Host '== Skipping tests (already run in CI) ==' -ForegroundColor Yellow
    } else {
        Write-Host '== Running tests ==' -ForegroundColor Cyan
        Invoke-Step "$Python -m pytest -q"
    }

    Write-Host '== Freezing the agent ==' -ForegroundColor Cyan
    if (Test-Path 'dist') { Remove-Item -Recurse -Force 'dist' }
    Invoke-Step (
        "$Python -m PyInstaller --noconfirm --clean --windowed " +
        "--name waprinter-agent " +
        "--hidden-import win32timezone " +
        "--hidden-import uvicorn.logging " +
        "--hidden-import uvicorn.loops.auto " +
        "--hidden-import uvicorn.protocols.http.auto " +
        "--hidden-import uvicorn.protocols.websockets.auto " +
        "--hidden-import uvicorn.lifespan.on " +
        "src\waprinter\agent.py"
    )

    Write-Host '== Freezing the CLI ==' -ForegroundColor Cyan
    Invoke-Step (
        "$Python -m PyInstaller --noconfirm --clean --console " +
        "--name waprinter --distpath dist\cli " +
        "src\waprinter\cli.py"
    )

    Copy-Tesseract

    Write-Host '== Building Baileys Service (Node.js) ==' -ForegroundColor Cyan
    Push-Location 'src\baileys_service'
    try {
        Invoke-Step "npm install --no-fund --no-audit"
        # Using @yao-pkg/pkg (maintained fork of pkg) that supports Node 20+
        Invoke-Step "npx --yes @yao-pkg/pkg . --targets node20-win-x64 --output ..\..\dist\baileys-service.exe"
    } finally {
        Pop-Location
    }

    Write-Host '== Compiling the installer ==' -ForegroundColor Cyan
    if (-not (Test-Path $ISCC)) {
        throw "Inno Setup not found at $ISCC. Install it or pass -ISCC <path>."
    }
    & $ISCC 'installer\setup.iss'
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }

    Write-Host ''
    Write-Host 'Built: installer\Output\' -ForegroundColor Green
    Get-ChildItem 'installer\Output\*.exe' | ForEach-Object {
        Write-Host ("  {0} ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB))
    }
}
finally {
    Pop-Location
}
