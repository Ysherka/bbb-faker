param([switch]$SkipBrowser, [string]$Release = '0.1.1')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install dependencies first.' }
if ($Release -notmatch '^\d+\.\d+\.\d+(?:-[a-zA-Z0-9]+)?$') { throw 'Invalid release identifier.' }
$releaseRoot = Join-Path $projectRoot "dist\releases\$Release"
$outputPath = Join-Path $releaseRoot 'BBBCompanion'
if (Test-Path -LiteralPath (Join-Path $outputPath 'data')) { throw 'Output contains user data. Choose another -Release identifier.' }
$workRoot = Join-Path $projectRoot "build\$Release"
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
Push-Location $projectRoot
try {
    $staticData = (Join-Path $projectRoot 'bbb_companion\static') + ';bbb_companion/static'
    $observerData = (Join-Path $projectRoot 'bbb_companion\observe.js') + ';bbb_companion'
    & $pythonPath -m PyInstaller --noconfirm --clean --onedir --console --name BBBCompanion --distpath $releaseRoot --workpath (Join-Path $workRoot 'work') --specpath $workRoot --paths $projectRoot --add-data $staticData --add-data $observerData --collect-data tzdata --collect-all uvicorn launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    if (-not $SkipBrowser) {
        $browserExecutable = & $pythonPath -c "from playwright.sync_api import sync_playwright`nwith sync_playwright() as p:`n    print(p.chromium.executable_path)"
        if ($LASTEXITCODE -ne 0) { throw 'Cannot locate Chromium.' }
        $browserDirectory = Split-Path -Parent (Split-Path -Parent $browserExecutable.Trim())
        if (-not (Test-Path -LiteralPath $browserExecutable.Trim())) { throw 'Run python -m playwright install chromium first.' }
        $browserTarget = Join-Path $outputPath 'browsers'
        New-Item -ItemType Directory -Path $browserTarget -Force | Out-Null
        Copy-Item -LiteralPath $browserDirectory -Destination $browserTarget -Recurse -Force
        $cacheRoot = Split-Path -Parent $browserDirectory
        Get-ChildItem -LiteralPath $cacheRoot -Directory | Where-Object { $_.Name -like 'chromium_headless_shell-*' -or $_.Name -like 'ffmpeg-*' -or $_.Name -like 'winldd-*' } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $browserTarget -Recurse -Force }
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $outputPath -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config.example.json') -Destination $outputPath -Force
    $archivePath = Join-Path $projectRoot "dist\BBBCompanion-$Release-Windows.zip"
    Compress-Archive -LiteralPath $outputPath -DestinationPath $archivePath -Force
    Write-Output $archivePath
} finally {
    Pop-Location
}
