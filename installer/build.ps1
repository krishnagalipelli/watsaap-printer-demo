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

.PARAMETER Target
    x64       - the normal build, frozen against whatever Python is on PATH.
    win7-x86  - 32-bit, for counters still on Windows 7. Must be run with a
                32-bit Python 3.8: 3.9 dropped Windows 7, and 216
                (ERROR_EXE_MACHINE_TYPE_MISMATCH) is what a 64-bit build gives
                when someone double-clicks it there. The pins that keep the
                frozen output startable on 7 are in constraints-win7.txt.
#>

[CmdletBinding()]
param(
    [string] $Python       = 'py -3.12',
    [string] $ISCC         = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    [string] $TesseractDir = 'C:\Program Files\Tesseract-OCR',
    [switch] $SkipOcr,
    [ValidateSet('x64', 'win7-x86')]
    [string] $Target = 'x64'
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
    # PyMuPDF invokes its embedded OCR engine. Only language data is external;
    # copying another tesseract.exe adds size and an unnecessary architecture
    # dependency. The same traineddata works in both payloads.
    foreach ($file in 'eng.traineddata', 'osd.traineddata') {
        $source = Join-Path $TesseractDir "tessdata\$file"
        if (-not (Test-Path $source)) { throw "Required OCR data missing: $source" }
        Copy-Item $source "$vendor\tessdata" -Force
    }

}

function Copy-Win7Runtimes {
    # Use a bounded SDK/toolset, never DLLs from System32 or the newest SDK.
    # Modern runners can contain x64 Python and VC runtimes requiring Win8+.
    $ucrt = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Redist\10.0.19041.0\ucrt\DLLs\x86'
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) { throw 'Install Visual Studio with the v142 (14.29) C++ tools.' }
    $vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.ComponentGroup.VC.Tools.142.x86.x64 -property installationPath
    if (-not $vs) { throw 'Visual Studio v142 (14.29) tools were not found.' }
    $vc = Get-ChildItem (Join-Path $vs 'VC\Redist\MSVC') -Directory -Filter '14.29.*' |
        ForEach-Object { Get-Item (Join-Path $_.FullName 'x86\Microsoft.VC142.CRT') -ErrorAction SilentlyContinue } |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $vc) { throw 'No x86 Microsoft.VC142.CRT redistributable found.' }
    if (-not (Test-Path (Join-Path $ucrt 'ucrtbase.dll'))) {
        throw 'Install Windows SDK 10.0.19041.0, including its x86 UCRT redistributable.'
    }
    $pythonHome = & cmd /c "$Python -c `"import sys;print(sys.base_prefix)`""
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate Python 3.8 runtime.' }
    $sources = @{}
    foreach ($file in (Get-ChildItem $ucrt -Filter '*.dll')) { $sources[$file.Name] = $file.FullName }
    foreach ($file in (Get-ChildItem $vc.FullName -Filter '*.dll')) { $sources[$file.Name] = $file.FullName }
    foreach ($name in 'python3.dll', 'python38.dll') {
        $file = Join-Path $pythonHome $name
        if (-not (Test-Path $file)) { throw "Missing interpreter runtime: $file" }
        $sources[$name] = $file
    }
    foreach ($dist in 'dist\waprinter-agent', 'dist\cli\waprinter') {
        # Replace nested copies too: they can take precedence over the root copy.
        foreach ($file in (Get-ChildItem $dist -Recurse -Filter '*.dll')) {
            if ($sources.ContainsKey($file.Name)) { Copy-Item $sources[$file.Name] $file.FullName -Force }
        }
        foreach ($name in $sources.Keys) { Copy-Item $sources[$name] (Join-Path $dist $name) -Force }
    }
    Invoke-Step "$Python packaging\verify_win7_payload.py dist\waprinter-agent dist\cli\waprinter"
}

try {
    Write-Host "== Installing build dependencies ($Target) ==" -ForegroundColor Cyan
    if ($Target -eq 'win7-x86') {
        # -c, not a requirements file: the project still declares what it needs,
        # and these only cap what pip is allowed to resolve that to.
        $constraints = 'installer\constraints-win7.txt'
        Invoke-Step "$Python -m pip install --upgrade pip"
        Invoke-Step "$Python -m pip install -c $constraints pyinstaller pefile==2023.2.7"
        Invoke-Step "$Python -m pip install -c $constraints -e `".[dev,windows]`""

        # A 64-bit interpreter here would freeze a 64-bit exe and every check
        # below would still pass, all the way to the counter.
        Write-Host '== Confirming the interpreter is 32-bit ==' -ForegroundColor Cyan
        $bits = & cmd /c "$Python -c `"import struct,sys;print(struct.calcsize('P')*8,sys.version_info[:2])`""
        if ($LASTEXITCODE -ne 0) { throw 'Could not ask Python for its word size.' }
        Write-Host "  $Python reports $bits"
        if ($bits -notmatch '^32 ') {
            throw "$Target needs a 32-bit Python; this one reports $bits. " +
                  "Use an x86 install (setup-python's architecture: x86)."
        }
        if ($bits -notmatch '\(3, 8\)') {
            throw ("Expected Python 3.8 for $Target; got $bits. " +
                           "Anything newer will not start on Windows 7.")
        }
    } else {
        Invoke-Step "$Python -m pip install --upgrade pip pyinstaller"
        Invoke-Step "$Python -m pip install -e `".[dev,windows]`""
    }

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
    Write-Host '== Freezing the agent ==' -ForegroundColor Cyan
    if (Test-Path 'dist') { Remove-Item -Recurse -Force 'dist' }
    Invoke-Step (
        "$Python -m PyInstaller --noconfirm --clean --windowed " +
        "--name waprinter-agent " +
        "--paths src " +
        "--hidden-import win32timezone " +
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
    if ($Target -eq 'win7-x86') { Copy-Win7Runtimes }

    Copy-Tesseract
    if (-not $SkipOcr) {
        $env:TESSDATA_PREFIX = Join-Path $root 'installer\vendor\tesseract\tessdata'
    }

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
    $smokeArgs = @('--selftest')
    if (-not $SkipOcr) { $smokeArgs += '--check-ocr' }
    $agent = Start-Process -FilePath $agentExe -ArgumentList $smokeArgs -Wait -PassThru
    if ($agent.ExitCode -ne 0) {
        $crash = Join-Path $env:PROGRAMDATA 'WAPrinter\logs\crash.txt'
        if (Test-Path $crash) { Write-Host (Get-Content $crash -Raw) -ForegroundColor Red }
        throw "Smoke test failed: waprinter-agent.exe --selftest returned $($agent.ExitCode)"
    }
    Write-Host '  ok    waprinter-agent.exe --selftest'

    Write-Host '== Compiling the installer ==' -ForegroundColor Cyan
    if (-not (Test-Path $ISCC)) {
        throw "Inno Setup not found at $ISCC. Install it or pass -ISCC <path>."
    }
    $ocrFlag = if ($SkipOcr) { "/DOcrEnabled=0" } else { "/DOcrEnabled=1" }
    & $ISCC $ocrFlag "/DTarget=$Target" 'installer\setup.iss'
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
