$ErrorActionPreference = "Stop"

function Invoke-MemleafPython {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $script:MemleafPython @script:MemleafPythonPrefix @Arguments
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

Invoke-MemleafPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"

$oldHermesHome = $env:HERMES_HOME
try {
    $env:HERMES_HOME = $hermesHome

    & $script:MemleafPython @script:MemleafPythonPrefix -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Invoke-MemleafPython -m ensurepip --upgrade
    }

    Write-Host "Installing memleaf from PyPI..."
    Invoke-MemleafPython -m pip install --upgrade memleaf

    Write-Host "Configuring Hermes to use memleaf..."
    Invoke-MemleafPython -m memleaf install

    Write-Host ""
    Write-Host "memleaf is installed and configured for Hermes."
    Write-Host "Restart Hermes to use memleaf."
} finally {
    if ($null -eq $oldHermesHome) {
        Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue
    } else {
        $env:HERMES_HOME = $oldHermesHome
    }
}
