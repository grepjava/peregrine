# Builds the Windows wheels, one per interpreter, and smoke-tests each: it is
# installed into a fresh venv whose PATH has no Swift and no OpenSSL, as a
# user's would not, and `peregrine` serves a request through two workers.
#
#   pwsh -NoProfile -File scripts\build-wheels-windows.ps1 [-Versions 3.11,3.15t] [-Out dist]
#
# Needs Swift 6.1+, Visual Studio's C++ build tools, OpenSSL for Windows (the
# full x64 package; OPENSSL_DIR if it is not in the default place) and uv,
# which supplies every interpreter. setup.py does the rest: it finds the MSVC
# environment, builds the extension and vendors the DLLs it loads.
param(
    [string[]]$Versions = @("3.11", "3.12", "3.13", "3.14", "3.14t", "3.15", "3.15t"),
    [string]$Out = (Join-Path $PSScriptRoot "..\dist"),
    [int]$Port = 8297
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Out = [IO.Path]::GetFullPath($Out)
New-Item -ItemType Directory -Force $Out | Out-Null
$env:PEREGRINE_REQUIRE_RELOCATE = "1"
$savedPath = $env:PATH
$failed = @()

foreach ($v in $Versions) {
    $env:PATH = $savedPath
    $tag = $v -replace '\.', ''
    $work = Join-Path $env:LOCALAPPDATA "peregrine-wheel-$tag"
    $env:PEREGRINE_SCRATCH_PATH = "$work\swift"
    $log = "$work\build.log"
    Write-Output "=== $v"
    $python = (& uv python find --managed-python $v).Trim()
    Remove-Item -Recurse -Force "$work\build-venv", "$work\run-venv" -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $work | Out-Null
    & uv venv -q --python $python "$work\build-venv"
    & uv pip install -q --python "$work\build-venv\Scripts\python.exe" setuptools wheel build
    # setuptools reuses build\lib between interpreters; start each one clean.
    Remove-Item -Recurse -Force (Join-Path $root "build") -ErrorAction SilentlyContinue
    Push-Location $root
    & "$work\build-venv\Scripts\python.exe" -m build --wheel --no-isolation --outdir $Out *> $log
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Write-Output "build failed; see $log"; $failed += $v; continue }
    $abi = "cp" + ($tag -replace 't$', '') + "-cp$tag-"
    $wheel = Get-ChildItem $Out -Filter "peregrine_server-*-$abi*win_amd64.whl" |
        Sort-Object LastWriteTime | Select-Object -Last 1
    Write-Output "  $($wheel.Name)"

    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;" + (Split-Path (Get-Command uv).Source)
    & uv venv -q --python $python "$work\run-venv"
    & uv pip install -q --python "$work\run-venv\Scripts\python.exe" $wheel.FullName
    Write-Output ("  " + (& "$work\run-venv\Scripts\peregrine.exe" --version))
    $server = Start-Process -FilePath "$work\run-venv\Scripts\peregrine.exe" -PassThru -NoNewWindow `
        -ArgumentList "--port $Port --workers 2 --python-path $root\benchmarks\contract asgi:app" `
        -RedirectStandardError "$work\serve.log"
    $body = ""
    for ($i = 0; $i -lt 80 -and $body -eq ""; $i++) {
        $body = (& curl.exe -s --max-time 1 "http://127.0.0.1:$Port/user/42") -join ""
        if ($body -eq "") { Start-Sleep -Milliseconds 250 }
    }
    Stop-Process -Id $server.Id -Force
    if ($body -eq "42") { Write-Output "  smoke OK" }
    else { Write-Output "  smoke FAILED [$body]; see $work\serve.log"; $failed += $v }
}
$env:PATH = $savedPath
if ($failed.Count -gt 0) { Write-Output "FAILED: $($failed -join ', ')"; exit 1 }
Write-Output "all $($Versions.Count) wheels built and served"
