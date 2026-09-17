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
    # A 64-bit tesseract.exe cannot be launched by a 32-bit install on a 32-bit
    # Windows, and it is the default install that gets staged here. Bundling it
    # anyway produces a build whose OCR fails only at the counter, which is the
    # one place nobody can debug it.
    if ($Target -eq 'win7-x86' -and -not $PSBoundParameters.ContainsKey('TesseractDir')) {
        throw "The $Target build needs a 32-bit Tesseract. Pass -TesseractDir " +
              "<path to a 32-bit install>, or -SkipOcr to hold scanned pages " +
              "for a person instead."
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

function Copy-Ucrt {
    # python38.dll links against the Universal CRT (ucrtbase.dll and the
    # api-ms-win-crt-* forwarders). Windows 10 and Server ship it, so PyInstaller
    # treats it as part of the OS and collects none of it -- and a Windows 7
    # counter without KB2999226 then shows "Error loading Python DLL ...
    # LoadLibrary: The specified module could not be found" before any of our
    # code runs. The smoke test below cannot catch that on this runner, which
    # has the UCRT, so the check here has to be on the files themselves.
    # Microsoft ships these for app-local deployment in the Windows SDK.
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Redist'
    $source = @(Get-ChildItem $kits -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'ucrt\DLLs\x86' }) +
        (Join-Path $kits 'ucrt\DLLs\x86') |
        Where-Object { Test-Path (Join-Path $_ 'ucrtbase.dll') } |
        Sort-Object -Descending | Select-Object -First 1
    if (-not $source) {
        throw "No x86 Universal CRT redistributable under $kits. Install the " +
              "Windows 10/11 SDK; without it the $Target build cannot start on 7."
    }

    Write-Host "== Bundling the Universal CRT from $source ==" -ForegroundColor Cyan
    foreach ($dist in 'dist\waprinter-agent', 'dist\cli\waprinter') {
        Copy-Item (Join-Path $source '*.dll') $dist -Force
        foreach ($dll in 'ucrtbase.dll', 'api-ms-win-crt-runtime-l1-1-0.dll') {
            if (-not (Test-Path (Join-Path $dist $dll))) {
                throw "Bundling the Universal CRT left no $dll in $dist"
            }
        }
    }
}

try {
    Write-Host "== Installing build dependencies ($Target) ==" -ForegroundColor Cyan
    if ($Target -eq 'win7-x86') {
        # -c, not a requirements file: the project still declares what it needs,
        # and these only cap what pip is allowed to resolve that to.
        $constraints = 'installer\constraints-win7.txt'
        Invoke-Step "$Python -m pip install --upgrade pip"
        Invoke-Step "$Python -m pip install -c $constraints pyinstaller"
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
            Write-Warning ("Expected Python 3.8 for $Target; got $bits. " +
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
    if ($Target -eq 'win7-x86') { Copy-Ucrt }

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
    & $ISCC "/DTarget=$Target" 'installer\setup.iss'
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
