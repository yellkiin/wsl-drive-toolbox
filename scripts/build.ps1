param(
    [string]$Python = "python",
    [string]$Name = "WSL-Drive-Toolbox",
    [string]$DistPath = "dist"
)

$ErrorActionPreference = "Stop"
if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "请在 Windows 上打包 Windows 应用。"
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --windowed --onedir `
        --name $Name --distpath $DistPath `
        --add-data "toolbox/wsl_agent.py:toolbox" `
        --add-data "toolbox/gateway.py:toolbox" `
        --add-data "toolbox/app_checks.py:toolbox" `
        start.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败，请检查上面的错误。" }

    $Bundle = Join-Path $DistPath $Name
    Copy-Item -LiteralPath "LICENSE", "README.md" -Destination $Bundle
    $ImageDirectory = Join-Path $Bundle "docs/images"
    New-Item -ItemType Directory -Path $ImageDirectory -Force | Out-Null
    Copy-Item -LiteralPath "docs/images/proxy-demo.png", "docs/images/disk-demo.png", "docs/images/universal-proxy-demo.png" -Destination $ImageDirectory
    Copy-Item -LiteralPath "docs/validation.md" -Destination (Join-Path $Bundle "docs")
    Copy-Item -LiteralPath "docs/universal-preview.md" -Destination (Join-Path $Bundle "docs")
    Copy-Item -LiteralPath "docs/universal-validation.md" -Destination (Join-Path $Bundle "docs")
    Write-Host "打包完成：$Bundle。请分发整个目录。"
} finally {
    Pop-Location
}
