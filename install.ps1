$ErrorActionPreference = "Stop"

function Invoke-MemleafPython {
    param([Parameter(Mandatory = $true)][string[]]$PythonArgs)

    & $script:MemleafPython @script:MemleafPythonPrefix @PythonArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

$hermesHome = $env:HERMES_HOME
if ([string]::IsNullOrWhiteSpace($hermesHome)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is unavailable and HERMES_HOME is not set."
    }
    $hermesHome = Join-Path $env:LOCALAPPDATA "hermes"
}
$hermesHome = [System.IO.Path]::GetFullPath($hermesHome)

$managedPython = Join-Path $hermesHome "hermes-agent\venv\Scripts\python.exe"
$script:MemleafPythonPrefix = @()

if (Test-Path -LiteralPath $managedPython -PathType Leaf) {
    $script:MemleafPython = $managedPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $script:MemleafPython = $pythonCommand.Source
    } else {
        $pyCommand = Get-Command py -ErrorAction SilentlyContinue
        if ($pyCommand) {
            $script:MemleafPython = $pyCommand.Source
            $script:MemleafPythonPrefix = @("-3.11")
        } else {
            throw "Python 3.11+ was not found. Reinstall Hermes with its official Windows installer or install Python 3.11+."
        }
    }
}

Invoke-MemleafPython -PythonArgs @(
    "-c",
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
)

$oldHermesHome = $env:HERMES_HOME
try {
    $env:HERMES_HOME = $hermesHome

    Write-Host "Installing or upgrading memleaf from PyPI..."

    $managedUv = Join-Path $hermesHome "bin\uv.exe"
    $uvCommand = $null
    if (Test-Path -LiteralPath $managedUv -PathType Leaf) {
        $uvCommand = $managedUv
    } else {
        $foundUv = Get-Command uv -ErrorAction SilentlyContinue
        if ($foundUv) {
            $uvCommand = $foundUv.Source
        }
    }

    if ($uvCommand -and $script:MemleafPythonPrefix.Count -eq 0) {
        & $uvCommand pip install --python $script:MemleafPython --upgrade memleaf
        if ($LASTEXITCODE -ne 0) {
            throw "uv could not install memleaf (exit code $LASTEXITCODE)"
        }
    } else {
        & $script:MemleafPython @script:MemleafPythonPrefix -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Invoke-MemleafPython -PythonArgs @("-m", "ensurepip", "--upgrade")
        }
        Invoke-MemleafPython -PythonArgs @("-m", "pip", "install", "--upgrade", "memleaf")
    }

    Write-Host "Configuring Hermes to use memleaf..."
    Invoke-MemleafPython -PythonArgs @("-m", "memleaf", "install")

    Write-Host ""
    Write-Host "memleaf is installed or upgraded and configured for Hermes."
    Write-Host "Restart Hermes to use memleaf."
} finally {
    if ($null -eq $oldHermesHome) {
        Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue
    } else {
        $env:HERMES_HOME = $oldHermesHome
    }
}
