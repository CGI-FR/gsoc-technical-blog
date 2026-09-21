param(
    [string]$PythonVersion = "3.14",
    [string]$OutputRoot = "release",
    [string]$ExistingWheelsDir = "wheels"
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    param([string]$Version)

    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) {
        throw "Python Launcher 'py' was not found. Install Python $Version or add it to PATH."
    }

    & py "-$Version" --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python $Version is not available through the Python Launcher. Try: py --list"
    }

    & py "-$Version" -c "import struct, sys; sys.exit(0 if struct.calcsize('P') * 8 == 64 else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Python $Version must be 64-bit to build the Windows wheelhouse."
    }
}

function Copy-CompatibleWheels {
    param(
        [string]$SourceDir,
        [string]$DestinationDir,
        [string]$Version
    )

    if (-not (Test-Path $SourceDir)) {
        return
    }

    $cpTag = "cp$($Version.Replace('.', ''))"
    $patterns = @(
        "*-py3-none-any.whl",
        "*-$cpTag-$cpTag-win_amd64.whl",
        "*-cp310-abi3-win_amd64.whl"
    )

    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $SourceDir -Filter $pattern -File -ErrorAction SilentlyContinue |
            Copy-Item -Destination $DestinationDir -Force
    }
}

Resolve-Python -Version $PythonVersion

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$initPath = Join-Path $root "ost_pst_parser\__init__.py"
$initText = Get-Content -Raw -Path $initPath
if ($initText -notmatch "__version__\s*=\s*['""]([^'""]+)['""]") {
    throw "Unable to read package version from ost_pst_parser/__init__.py"
}
$packageVersion = $Matches[1]

$distName = "ost-pst-parser-$packageVersion-windows-py$PythonVersion"
$outputDir = Join-Path $root $OutputRoot
$stageDir = Join-Path $outputDir $distName
$wheelhouse = Join-Path $stageDir "wheels"
$zipPath = Join-Path $outputDir "$distName.zip"

if (Test-Path $stageDir) {
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Get-ChildItem -Path $root -Directory -Filter "*.egg-info" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

$localBuildDir = Join-Path $root "build"
if (Test-Path $localBuildDir) {
    Remove-Item -LiteralPath $localBuildDir -Recurse -Force
}

New-Item -ItemType Directory -Force $wheelhouse | Out-Null

Copy-CompatibleWheels -SourceDir (Join-Path $root $ExistingWheelsDir) -DestinationDir $wheelhouse -Version $PythonVersion

Write-Host "Building wheelhouse for Python $PythonVersion..."
& py "-$PythonVersion" -c "import setuptools, wheel" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing missing Python build tools..."
    & py "-$PythonVersion" -m pip install setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install setuptools/wheel for Python $PythonVersion."
    }
}

& py "-$PythonVersion" -m pip wheel --wheel-dir $wheelhouse --find-links $wheelhouse .
if ($LASTEXITCODE -ne 0) {
    throw "Wheel build failed. If libpff-python failed, build its cp$($PythonVersion.Replace('.', '')) Windows wheel first and place it in '$ExistingWheelsDir'."
}

$cpTag = "cp$($PythonVersion.Replace('.', ''))"
$requiredWheels = @(
    "ost_pst_parser-*-py3-none-any.whl",
    "libpff_python-*-$cpTag-$cpTag-win_amd64.whl",
    "pyside6-*-cp310-abi3-win_amd64.whl",
    "pyside6_addons-*-cp310-abi3-win_amd64.whl",
    "pyside6_essentials-*-cp310-abi3-win_amd64.whl",
    "shiboken6-*-cp310-abi3-win_amd64.whl"
)

foreach ($pattern in $requiredWheels) {
    if (-not (Get-ChildItem -Path $wheelhouse -Filter $pattern -File -ErrorAction SilentlyContinue)) {
        throw "Missing required wheel '$pattern' in $wheelhouse"
    }
}

Copy-Item -LiteralPath "Install Outlook Reader.bat" -Destination $stageDir -Force
Copy-Item -LiteralPath "Launch Outlook Reader.bat" -Destination $stageDir -Force
Copy-Item -LiteralPath "README.md" -Destination $stageDir -Force

$manifestPath = Join-Path $stageDir "WHEELHOUSE_CONTENTS.txt"
@(
    "Outlook Evidence Viewer Windows distribution",
    "Package version: $packageVersion",
    "Target Python: $PythonVersion 64-bit",
    "",
    "Receiver instructions:",
    "1. Extract this zip.",
    "2. Double-click Install Outlook Reader.bat.",
    "3. Drag a PST/OST/SQLite file onto Launch Outlook Reader.bat, or run outlook-reader from the created .venv.",
    "",
    "Included wheels:"
) | Set-Content -Path $manifestPath -Encoding UTF8

Get-ChildItem -Path $wheelhouse -Filter "*.whl" -File |
    Sort-Object Name |
    ForEach-Object { " - $($_.Name)" } |
    Add-Content -Path $manifestPath -Encoding UTF8

Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Created distributable zip:"
Write-Host "  $zipPath"
Write-Host ""
Write-Host "Ship this zip to colleagues. They should extract it and double-click Install Outlook Reader.bat."
