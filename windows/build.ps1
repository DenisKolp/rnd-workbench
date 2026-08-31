param(
    [string]$Python = "py",
    [string]$NodePackageManager = "npm",
    [switch]$WithVoice
)

$ErrorActionPreference = "Stop"
$ProjectDirectory = Split-Path -Parent $PSScriptRoot
$BackendOutput = Join-Path $ProjectDirectory "windows\dist\backend"
$ElectronDirectory = Join-Path $ProjectDirectory "windows\electron"
Set-Location $ProjectDirectory

if ($env:OS -ne "Windows_NT") {
    throw "The Windows portable package must be built and smoke-tested on Windows."
}

function Get-PythonLauncherArguments {
    $Leaf = Split-Path -Leaf $Python
    if ($Leaf -match "^py(?:\.exe)?$") { return @("-3.11") }
    return @()
}

function Assert-PythonPackageVersion {
    param([string]$Package, [string]$Expected)
    $Actual = & $Python @PythonLauncherArguments -c "import importlib.metadata as m; print(m.version('$Package'))" 2>$null
    if ($LASTEXITCODE -ne 0 -or $Actual.Trim() -ne $Expected) {
        throw "$Package==$Expected is required. Create an isolated environment from windows\requirements-build.txt or windows\requirements-voice.txt."
    }
}

$PythonLauncherArguments = Get-PythonLauncherArguments
Assert-PythonPackageVersion -Package "numpy" -Expected "2.2.6"
Assert-PythonPackageVersion -Package "pyinstaller" -Expected "6.15.0"
if ($WithVoice) {
    Assert-PythonPackageVersion -Package "faster-whisper" -Expected "1.2.0"
}

# Build the temporary Python JSONL bridge as a self-contained executable.  A
# future Java core can replace this file while keeping the Electron contract.

New-Item -ItemType Directory -Force -Path $BackendOutput | Out-Null
New-Item -ItemType Directory -Force -Path "build\windows-spec" | Out-Null

$PyInstallerArguments = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--console",
    "--paths", "src",
    "--distpath", $BackendOutput,
    "--workpath", "build\windows-backend",
    "--specpath", "build\windows-spec",
    "--name", "rnd-workbench-backend"
)
if ($WithVoice) {
    $PyInstallerArguments += @(
        "--collect-all", "faster_whisper",
        "--collect-all", "ctranslate2",
        "--collect-all", "tokenizers"
    )
}
$PyInstallerArguments += "windows\backend_entry.py"
& $Python @PythonLauncherArguments -m PyInstaller @PyInstallerArguments
if ($LASTEXITCODE -ne 0) { throw "Backend packaging failed." }

Set-Location $ElectronDirectory
& $NodePackageManager ci
if ($LASTEXITCODE -ne 0) { throw "Electron dependencies installation failed." }
& $NodePackageManager run build:win
if ($LASTEXITCODE -ne 0) { throw "Electron packaging failed." }

Write-Host "Windows Electron pilot package is in windows\dist\electron"
