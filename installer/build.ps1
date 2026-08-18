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

    # NOTE: the entry points are the shims in packaging\, never the modules in
    # src\waprinter\. PyInstaller runs its entry script as __main__, and those
    # modules use relative imports, which fail instantly when run that way.
    # Frozen --windowed that failure is invisible: the exe just does nothing.
    # pywebview loads its GUI backend by name at runtime, so PyInstaller cannot
    # see it from the imports; --collect-all also brings in the JS bridge files
    # it injects into each window. Without these the frozen app starts and then
    # fails the moment it tries to open a window.
    Write-Host '== Freezing the agent ==' -ForegroundColor Cyan
    if (Test-Path 'dist') { Remove-Item -Recurse -Force 'dist' }
    Invoke-Step (
        "$Python -m PyInstaller --noconfirm --clean --windowed " +
        "--name waprinter-agent " +
        "--paths src " +
        "--hidden-import win32timezone " +
        "--hidden-import uvicorn.logging " +
        "--hidden-import uvicorn.loops.auto " +
        "--hidden-import uvicorn.protocols.http.auto " +
        "--hidden-import uvicorn.protocols.websockets.auto " +
        "--hidden-import uvicorn.lifespan.on " +
        "--collect-all webview " +
        "--hidden-import webview.platforms.edgechromium " +
        "--hidden-import webview.platforms.winforms " +
        "--hidden-import clr " +
        "packaging\waprinter_agent.py"
    )

    Write-Host '== Freezing the CLI ==' -ForegroundColor Cyan
    Invoke-Step (
        "$Python -m PyInstaller --noconfirm --clean --console " +
        "--name waprinter --distpath dist\cli --paths src " +
        "packaging\waprinter_cli.py"
    )

    # Run what was just built. A frozen app can fail on imports that work fine
    # from source, and --windowed hides it completely, so the build must not be
    # allowed to call that a success. This exact check would have caught the
    # broken installer that shipped before.
    Write-Host '== Smoke testing the frozen executables ==' -ForegroundColor Cyan
    $cliExe   = 'dist\cli\waprinter\waprinter.exe'
    $agentExe = 'dist\waprinter-agent\waprinter-agent.exe'
    foreach ($exe in @($cliExe, $agentExe)) {
        if (-not (Test-Path $exe)) { throw "PyInstaller produced no $exe" }
    }

    & $cliExe --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Smoke test failed: $cliExe --help returned $LASTEXITCODE" }
    Write-Host '  ok    waprinter.exe --help'

    # --windowed means no console output, so the exit code is the signal.
    $agent = Start-Process -FilePath $agentExe -ArgumentList '--selftest' -Wait -PassThru
    if ($agent.ExitCode -ne 0) {
        $crash = Join-Path $env:PROGRAMDATA 'WAPrinter\logs\crash.txt'
        if (Test-Path $crash) { Write-Host (Get-Content $crash -Raw) -ForegroundColor Red }
        throw "Smoke test failed: waprinter-agent.exe --selftest returned $($agent.ExitCode)"
    }
    Write-Host '  ok    waprinter-agent.exe --selftest'

    Copy-Tesseract


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
