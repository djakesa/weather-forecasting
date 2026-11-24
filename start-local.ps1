$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$projectPython = Join-Path $projectDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw "Environnement absent. Créez .venv puis installez requirements.txt."
}
Set-Location -LiteralPath $projectDirectory
Write-Host 'Atlas Solaire — http://127.0.0.1:8000' -ForegroundColor Green
Write-Host 'Ctrl+C pour arrêter le backend et le frontend locaux.'
& $projectPython (Join-Path $projectDirectory 'app.py')
exit $LASTEXITCODE
