# ---------------------------------------------------------------------------
# test_local.ps1  —  Build and invoke the CodeCommit Lambda locally via Docker
#
# Usage:
#   cd c:\Users\draiy\Desktop\pr-agent
#   .\infra\test_local.ps1
#
# Required: fill in OPENAI_KEY and REPO_NAME below before running.
# ---------------------------------------------------------------------------

# ── CONFIG — edit these two lines ──────────────────────────────────────────
$OPENAI_KEY   = "sk-your-openai-key-here"   # your real OpenAI key
$REPO_NAME    = "my-repo"                   # CodeCommit repo name
$PR_ID        = "1"                         # a PR that exists in that repo
# ───────────────────────────────────────────────────────────────────────────

$AWS_REGION   = "ap-south-1"
$IMAGE        = "pr-agent-codecommit:local"
$CONTAINER    = "pr-agent-test"
$PORT         = 9000

# Read AWS credentials from the configured CLI profile
$AWS_ACCESS_KEY_ID     = aws configure get aws_access_key_id
$AWS_SECRET_ACCESS_KEY = aws configure get aws_secret_access_key

# ---------------------------------------------------------------------------
# 1. Update the test event with the real repo name and PR ID
# ---------------------------------------------------------------------------
$eventFile = "$PSScriptRoot\sample_event.json"
$event = Get-Content $eventFile -Raw | ConvertFrom-Json
$event.detail.repositoryNames = @($REPO_NAME)
$event.detail.pullRequestId   = $PR_ID
$event.region                 = $AWS_REGION
$updatedEvent = $event | ConvertTo-Json -Depth 10
$updatedEventFile = "$PSScriptRoot\sample_event_run.json"
$updatedEvent | Set-Content $updatedEventFile -Encoding UTF8
Write-Host "`n[1/4] Test event written to $updatedEventFile" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 2. Build the Docker image
# ---------------------------------------------------------------------------
Write-Host "`n[2/4] Building Docker image '$IMAGE' ..." -ForegroundColor Cyan
docker build `
    --target codecommit_lambda `
    --platform linux/amd64 `
    -t $IMAGE `
    -f docker\Dockerfile.lambda `
    .

if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker build failed." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Start the Lambda container
# ---------------------------------------------------------------------------
Write-Host "`n[3/4] Starting Lambda container on port $PORT ..." -ForegroundColor Cyan

# Stop any leftover container from a previous run
docker rm -f $CONTAINER 2>$null

docker run -d `
    --name $CONTAINER `
    -p "${PORT}:8080" `
    -e OPENAI__KEY=$OPENAI_KEY `
    -e CONFIG__MODEL="gpt-4o" `
    -e CONFIG__GIT_PROVIDER="codecommit" `
    -e CONFIG__LOG_LEVEL="DEBUG" `
    -e AWS_DEFAULT_REGION=$AWS_REGION `
    -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID `
    -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY `
    $IMAGE

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to start container." -ForegroundColor Red
    exit 1
}

# Give the Runtime Interface Emulator a moment to start
Start-Sleep -Seconds 2

# ---------------------------------------------------------------------------
# 4. Invoke the Lambda with the test event
# ---------------------------------------------------------------------------
Write-Host "`n[4/4] Invoking Lambda with test event ..." -ForegroundColor Cyan

$response = Invoke-RestMethod `
    -Method Post `
    -Uri "http://localhost:${PORT}/2015-03-31/functions/function/invocations" `
    -ContentType "application/json" `
    -InFile $updatedEventFile

Write-Host "`nResponse:" -ForegroundColor Green
$response | ConvertTo-Json -Depth 5

# ---------------------------------------------------------------------------
# Show live logs then stop container
# ---------------------------------------------------------------------------
Write-Host "`nContainer logs:" -ForegroundColor Cyan
docker logs $CONTAINER

Write-Host "`nStopping container ..." -ForegroundColor Cyan
docker rm -f $CONTAINER | Out-Null

# Clean up temp event file
Remove-Item $updatedEventFile -ErrorAction SilentlyContinue
