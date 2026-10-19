# Build ElevateXTrafficInjector.exe (onefile, windowed) with PyInstaller.
#
# Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File .\build.ps1
#
# IMPORTANT: the SimConnect Python package ships a native SimConnect.dll that
# PyInstaller's --collect-submodules does NOT bundle. Without it the frozen app
# fails at runtime with "SimConnect not found" (it looks for the DLL next to the
# module inside the temp _MEIxxxxx folder). We add it explicitly with
# --add-binary, and verify afterwards that it actually made it into the exe.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# --- locate the native DLL that must ship next to the SimConnect package ---
$simDll = (python -c "import SimConnect, os; print(os.path.join(os.path.dirname(SimConnect.__file__), 'SimConnect.dll'))").Trim()
if (-not (Test-Path -LiteralPath $simDll)) {
    throw "SimConnect.dll not found at '$simDll'. Install the package: pip install SimConnect"
}
Write-Host "Bundling SimConnect.dll from: $simDll"

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name ElevateXTrafficInjector `
    --icon icon.ico `
    --add-data "icon.png;." `
    --collect-submodules SimConnect `
    --add-binary "$simDll;SimConnect" `
    elevatex_dashboard.py
if (-not $?) { throw "PyInstaller build failed." }

# --- verify the native DLL is really inside the produced exe ---
$exe = "dist\ElevateXTrafficInjector.exe"
$bundled = (python -c "from PyInstaller.archive.readers import CArchiveReader; r = CArchiveReader(r'$exe'); print(any('SimConnect.dll' in n for n in r.toc))").Trim()
if ($bundled -ne "True") {
    throw "SimConnect.dll was NOT bundled into $exe - the build is broken."
}

Write-Host ""
Write-Host "Build OK: $exe (SimConnect.dll verified)" -ForegroundColor Green
