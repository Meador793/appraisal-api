# run_local.ps1 -- build and run the container on Windows 11.
#
#   Right-click PowerShell -> Run, then from the project folder:
#       .\run_local.ps1
#
# If PowerShell blocks the script ("running scripts is disabled on this
# system"), that is Windows' default execution policy, not a problem with the
# file. Allow local scripts for your user only:
#       Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# Prerequisite: Docker Desktop installed and RUNNING (whale icon in the system
# tray, not just installed). WSL2 is enabled by the Docker Desktop installer.

$ErrorActionPreference = "Stop"

$IMAGE     = "appraisal-api:local"
$PORT      = 8000
$ARTIFACTS = Join-Path $PSScriptRoot "notebooks\artifacts\v1"

# ---------------------------------------------------------------- checks
Write-Host "Checking Docker..." -ForegroundColor Cyan
try {
    docker version --format '{{.Server.Version}}' | Out-Null
} catch {
    Write-Host "Docker is not running. Start Docker Desktop and wait for the whale icon to stop animating." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $ARTIFACTS)) {
    Write-Host "No artifacts found at $ARTIFACTS" -ForegroundColor Red
    Write-Host "Run the Phase 1 notebook through to Step 16 first -- it writes the model files." -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------- API key
if (-not $env:API_KEYS) {
    Write-Host "API_KEYS not set, generating a throwaway key for this session..." -ForegroundColor Yellow
    $env:API_KEYS = "apr_local_" + [Convert]::ToBase64String((1..24 | ForEach-Object { Get-Random -Max 256 })).Replace("+","").Replace("/","").Replace("=","")
    Write-Host "  API key: $env:API_KEYS" -ForegroundColor Green
}

# ---------------------------------------------------------------- build
Write-Host "`nBuilding image..." -ForegroundColor Cyan
docker build -t $IMAGE $PSScriptRoot
if ($LASTEXITCODE -ne 0) { Write-Host "Build failed." -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- run
docker rm -f appraisal-local 2>$null | Out-Null

Write-Host "`nStarting container on http://localhost:$PORT ..." -ForegroundColor Cyan
# The volume mount serves the model from your machine instead of S3, so Phase 2
# works with no AWS account at all. :ro makes it read-only -- the container has
# no business writing to your artifacts folder.
docker run -d --name appraisal-local `
    -p "${PORT}:8000" `
    -e API_KEYS="$env:API_KEYS" `
    -e LOCAL_MODEL_DIR=/app/artifacts `
    -v "${ARTIFACTS}:/app/artifacts:ro" `
    $IMAGE

Write-Host "Waiting for the model to load..." -ForegroundColor Cyan
$ok = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 2
    try {
        $h = Invoke-RestMethod "http://localhost:$PORT/health" -TimeoutSec 3
        if ($h.model_loaded) { $ok = $true; break }
    } catch { }
}

if (-not $ok) {
    Write-Host "`nThe container did not become healthy. Logs:" -ForegroundColor Red
    docker logs appraisal-local --tail 40
    exit 1
}

Write-Host "`nRunning. Model version $($h.model_version)" -ForegroundColor Green
Write-Host "  Interactive API docs:  http://localhost:$PORT/docs"
Write-Host "  API key:               $env:API_KEYS"
Write-Host "`nTry a prediction:" -ForegroundColor Cyan
Write-Host @"
  `$body = '{"subject":{"main_sqft":1800,"upper_sqft":900,"bedrooms":4,"baths_full":3,"garage_spaces":3,"year_built":2005}}'
  Invoke-RestMethod -Method Post http://localhost:$PORT/predict ``
      -Headers @{"X-API-Key"="$env:API_KEYS"} -ContentType "application/json" -Body `$body
"@
Write-Host "`nDownload a PDF:" -ForegroundColor Cyan
Write-Host @"
  Invoke-WebRequest -Method Post http://localhost:$PORT/report ``
      -Headers @{"X-API-Key"="$env:API_KEYS"} -ContentType "application/json" ``
      -Body `$body -OutFile report.pdf
"@
Write-Host "`nStop it with:  docker rm -f appraisal-local" -ForegroundColor DarkGray
