$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$distDir = Join-Path $projectDir "dist"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Ambiente Python não encontrado em $pythonExe"
}

Push-Location $projectDir
try {
    & $pythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --console `
        --name ChefinhoTrade `
        --collect-all iqoptionapi `
        bot.py

    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao gerar o executável."
    }

    foreach ($configFile in @(".env", "settings.json")) {
        $source = Join-Path $projectDir $configFile
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $distDir -Force
        }
    }

    Write-Host "Executável criado em: $distDir\ChefinhoTrade.exe"
}
finally {
    Pop-Location
}
