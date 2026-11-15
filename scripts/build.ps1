param([string]$Python = "python")

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") {
    throw "请在 Windows 上打包 Windows 应用。"
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --windowed --onedir `
        --name WSL-Drive-Toolbox `
        --add-data "toolbox/wsl_agent.py:toolbox" `
        start.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败，请检查上面的错误。" }

    $Bundle = Join-Path $ProjectRoot "dist/WSL-Drive-Toolbox"
    Copy-Item -LiteralPath "LICENSE", "README.md" -Destination $Bundle
    $ImageDirectory = Join-Path $Bundle "docs/images"
    New-Item -ItemType Directory -Path $ImageDirectory -Force | Out-Null
    Copy-Item -LiteralPath "docs/images/proxy-demo.png", "docs/images/disk-demo.png" -Destination $ImageDirectory
    Copy-Item -LiteralPath "docs/validation.md" -Destination (Join-Path $Bundle "docs")
    Write-Host "打包完成：$Bundle。请分发整个目录。"
} finally {
    Pop-Location
}
