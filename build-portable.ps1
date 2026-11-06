param(
    [string]$Version = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not $Version) {
    $line = Select-String -Path "ppw_bench.py" -Pattern '^__version__ = "([^"]+)"' |
        Select-Object -First 1
    if (-not $line) {
        throw "Cannot read __version__ from ppw_bench.py"
    }
    $Version = $line.Matches[0].Groups[1].Value
}
$Tag = "v$Version"
Write-Host "Building PPW Bench $Tag" -ForegroundColor Cyan

if (Get-Command python -ErrorAction SilentlyContinue) {
    $Python = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $Python = "py"
} else {
    throw "Python not found"
}

Write-Host "==> Building bench.exe"
& gcc -O2 -static bench.c -o bench.exe
if ($LASTEXITCODE -ne 0) {
    throw "gcc failed with exit code $LASTEXITCODE"
}

if (-not $SkipInstall) {
    Write-Host "==> Installing PyInstaller"
    & $Python -m pip install --quiet --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "pip install pyinstaller failed with exit code $LASTEXITCODE"
    }
}

Write-Host "==> Building portable exe"
& $Python -m PyInstaller --noconfirm --clean --onefile --console --name ppw-bench `
    --add-binary "bench.exe;." ppw_bench.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $PWD "dist\ppw-bench.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Missing build artifact: $exe"
}
& $exe --version
if ($LASTEXITCODE -ne 0) {
    throw "Portable exe failed the version check"
}

Write-Host "==> Packing ZIP"
$stageName = "ppw-bench-$Tag-win64-portable"
$stage = Join-Path $PWD "dist\$stageName"
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Path $stage | Out-Null
Copy-Item -LiteralPath $exe $stage
Copy-Item -LiteralPath "README.md", "README.en.md", "LICENSE" $stage

$zip = Join-Path $PWD "dist\$stageName.zip"
if (Test-Path -LiteralPath $zip) {
    Remove-Item -LiteralPath $zip -Force
}
Compress-Archive -LiteralPath $stage -DestinationPath $zip

$hashFile = Join-Path $PWD "dist\SHA256SUMS.txt"
$lines = foreach ($file in @($exe, $zip)) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower()
    "$hash  $(Split-Path -Leaf $file)"
}
$lines | Set-Content -LiteralPath $hashFile -Encoding ascii

Write-Host ""
Write-Host "Done:" -ForegroundColor Green
Write-Host "  $exe"
Write-Host "  $zip"
Write-Host "  $hashFile"
