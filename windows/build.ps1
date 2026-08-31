param(
    [string]$Python = "py",
    [string]$NodePackageManager = "npm",
    [string]$Gradle = "gradle",
    [switch]$WithVoice
)

$ErrorActionPreference = "Stop"
$ProjectDirectory = Split-Path -Parent $PSScriptRoot
$BackendOutput = Join-Path $ProjectDirectory "windows\dist\backend"
$JavaCoreOutput = Join-Path $ProjectDirectory "windows\dist\java-core"
$LegalOutput = Join-Path $ProjectDirectory "windows\dist\licenses"
$JavaCoreDistribution = Join-Path $ProjectDirectory "core-java\build\install\rnd-workbench-core"
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

# Build the Java policy companion and a bounded JRE image. The Python process
# remains the latency-critical ML bridge and invokes this companion only for
# metadata-only routing and integration policy decisions.

& $Gradle --no-daemon -p core-java clean test installDist
if ($LASTEXITCODE -ne 0) { throw "Java core build failed." }

$Jlink = $null
if ($env:JAVA_HOME) {
    $Candidate = Join-Path $env:JAVA_HOME "bin\jlink.exe"
    if (Test-Path $Candidate -PathType Leaf) { $Jlink = $Candidate }
}
if (-not $Jlink) {
    $JlinkCommand = Get-Command jlink -ErrorAction SilentlyContinue
    if ($JlinkCommand) { $Jlink = $JlinkCommand.Source }
}
if (-not $Jlink) { throw "JDK 21 jlink is required." }
$JlinkVersion = (& $Jlink --version).Trim()
if ($LASTEXITCODE -ne 0 -or $JlinkVersion -notmatch "^21(?:\.|$)") {
    throw "JDK 21 jlink is required."
}

if (Test-Path $JavaCoreOutput) {
    Remove-Item -Recurse -Force $JavaCoreOutput
}
New-Item -ItemType Directory -Force -Path $JavaCoreOutput | Out-Null
Copy-Item -Recurse -Force (Join-Path $JavaCoreDistribution "lib") (Join-Path $JavaCoreOutput "lib")
& $Jlink `
    --add-modules "java.base,java.desktop,java.instrument,java.logging,java.management,java.naming,java.net.http,java.security.jgss,java.sql,java.transaction.xa,java.xml,jdk.crypto.ec,jdk.unsupported" `
    --strip-debug `
    --no-header-files `
    --no-man-pages `
    --compress=zip-6 `
    --output (Join-Path $JavaCoreOutput "runtime")
if ($LASTEXITCODE -ne 0) { throw "Java runtime image build failed." }

$BridgeJournal = Join-Path ([System.IO.Path]::GetTempPath()) ("rnd-workbench-java-bridge-" + [guid]::NewGuid().ToString("N") + ".sqlite3")
try {
    $env:PYTHONPATH = Join-Path $ProjectDirectory "src"
    & $Python @PythonLauncherArguments scripts\verify_java_core_bridge.py `
        --java (Join-Path $JavaCoreOutput "runtime\bin\java.exe") `
        --lib-dir (Join-Path $JavaCoreOutput "lib") `
        --journal $BridgeJournal
    if ($LASTEXITCODE -ne 0) { throw "Java/Python bridge verification failed." }
} finally {
    foreach ($Path in @($BridgeJournal, "$BridgeJournal-wal", "$BridgeJournal-shm")) {
        if (Test-Path $Path -PathType Leaf) { Remove-Item -Force $Path }
    }
}

if (Test-Path $LegalOutput) {
    Remove-Item -Recurse -Force $LegalOutput
}
New-Item -ItemType Directory -Force -Path (Join-Path $LegalOutput "texts") | Out-Null
Copy-Item -Force LICENSE (Join-Path $LegalOutput "RnD-Workbench-MIT.txt")
Copy-Item -Force THIRD_PARTY_NOTICES.md (Join-Path $LegalOutput "THIRD_PARTY_NOTICES.md")
Copy-Item -Recurse -Force third_party\licenses\* (Join-Path $LegalOutput "texts")

# Build the Python JSONL/ML bridge as a self-contained executable.

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
