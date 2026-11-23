param(
    [string]$Python = "python",
    [string]$DistPath = ".",
    [switch]$ReleaseZip
)

$ErrorActionPreference = "Stop"
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "Build the Windows executable on Windows."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $platform = & $Python -c "import sysconfig; print(sysconfig.get_platform())"
    if ($LASTEXITCODE -ne 0 -or $platform -ne "win-amd64") { throw "Use x64 Windows Python." }
    $outputDirectory = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DistPath)
    $name = "一键连接WSL"
    & $Python -m PyInstaller --noconfirm --windowed --onefile `
        --name $name --distpath $outputDirectory --specpath build `
        --add-data "${projectRoot}/toolbox/wsl_agent.py:toolbox" `
        --add-data "${projectRoot}/toolbox/gateway.py:toolbox" `
        --add-data "${projectRoot}/toolbox/app_checks.py:toolbox" `
        start.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
    $executable = Join-Path $outputDirectory "$name.exe"
    Write-Host "Built $executable"

    if ($ReleaseZip) {
        $version = & $Python -c "from toolbox import __version__; print(__version__)"
        if ($LASTEXITCODE -ne 0) { throw "Could not determine package version." }
        $archiveName = "WSL-Drive-Toolbox-v$version-windows-x64.zip"
        $archive = Join-Path $outputDirectory $archiveName
        Compress-Archive -LiteralPath $executable, (Join-Path $projectRoot "LICENSE") -DestinationPath $archive -Force
        $digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        Set-Content -LiteralPath "$archive.sha256" -Value "$digest  $archiveName" -Encoding ascii
        Write-Host "Packaged $archive"
    }
} finally {
    Pop-Location
}
