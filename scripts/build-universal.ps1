param([string]$Python = "python")

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "build.ps1") -Python $Python `
    -Name "WSL-Drive-Toolbox-Universal" -DistPath "dist-universal"
