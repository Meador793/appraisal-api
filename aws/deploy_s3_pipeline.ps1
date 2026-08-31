# deploy_s3_pipeline.ps1
#
# One-shot deploy of the S3-triggered batch pipeline: bucket, ECR repo,
# Lambda function, IAM role, and the S3 trigger that wires them together.
#
# Run from the repo root (where Dockerfile.batch lives):
#     .\aws\deploy_s3_pipeline.ps1
#
# Safe to re-run. Every step checks whether its resource already exists
# before creating it, so if step 6 fails you can fix the problem and run the
# whole script again rather than tracking down which step to resume from.
#
# Prerequisites:
#   - AWS CLI v2 installed and `aws configure` already run
#   - Docker Desktop running
#   - This script sitting in <repo root>\aws\, next to Dockerfile.batch one
#     level up

$ErrorActionPreference = "Stop"

# ==========================================================================
# CONFIGURATION — the only section you should need to edit
# ==========================================================================
$REGION       = "us-east-2"
$BUCKET       = "appraisal-jobs-$((Get-Random -Maximum 99999))"   # must be globally unique; regenerate if it collides
$ECR_REPO     = "appraisal-batch"
$IMAGE_TAG    = "v1"
$FUNCTION     = "appraisal-batch"
$ROLE_NAME    = "appraisalBatchRole"
$MEMORY_MB    = 3008    # also sets vCPU count on Lambda -- see DEPLOY_S3_PIPELINE.md
$TIMEOUT_SEC  = 900     # Lambda's maximum

# ==========================================================================
Write-Host "`n=== Appraisal Batch Pipeline — AWS Deploy ===" -ForegroundColor Cyan
Write-Host "Region: $REGION   Bucket: $BUCKET   Function: $FUNCTION`n"

# --------------------------------------------------------------- preflight
Write-Host "[1/9] Checking prerequisites..." -ForegroundColor Cyan

try { docker version --format '{{.Server.Version}}' | Out-Null }
catch { Write-Host "Docker is not running. Start Docker Desktop and re-run this script." -ForegroundColor Red; exit 1 }

if (-not (Test-Path ".\Dockerfile.batch")) {
    Write-Host "Dockerfile.batch not found. Run this script from the repo root:" -ForegroundColor Red
    Write-Host "    cd path\to\appraisal-api" -ForegroundColor Yellow
    Write-Host "    .\aws\deploy_s3_pipeline.ps1" -ForegroundColor Yellow
    exit 1
}

$ACCOUNT = (aws sts get-caller-identity --query Account --output text 2>$null)
if (-not $ACCOUNT) {
    Write-Host "AWS CLI is not configured. Run 'aws configure' first and paste in an IAM user's access key." -ForegroundColor Red
    exit 1
}
Write-Host "  AWS account: $ACCOUNT" -ForegroundColor Green
$REGISTRY = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

# --------------------------------------------------------------- S3 bucket
Write-Host "`n[2/9] Creating S3 bucket..." -ForegroundColor Cyan
$exists = aws s3api head-bucket --bucket $BUCKET 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  s3://$BUCKET already exists, reusing it." -ForegroundColor Yellow
} else {
    if ($REGION -eq "us-east-1") {
        aws s3api create-bucket --bucket $BUCKET --region $REGION | Out-Null
    } else {
        aws s3api create-bucket --bucket $BUCKET --region $REGION `
            --create-bucket-configuration LocationConstraint=$REGION | Out-Null
    }
    aws s3api put-public-access-block --bucket $BUCKET `
        --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" | Out-Null
    Write-Host "  Created s3://$BUCKET (public access blocked)." -ForegroundColor Green
}

# --------------------------------------------------------------- ECR repo
Write-Host "`n[3/9] Creating ECR repository..." -ForegroundColor Cyan
aws ecr describe-repositories --repository-names $ECR_REPO --region $REGION 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    aws ecr create-repository --repository-name $ECR_REPO --region $REGION | Out-Null
    Write-Host "  Created ECR repo $ECR_REPO." -ForegroundColor Green
} else {
    Write-Host "  ECR repo $ECR_REPO already exists, reusing it." -ForegroundColor Yellow
}

# --------------------------------------------------------- build and push
Write-Host "`n[4/9] Building and pushing the Lambda image..." -ForegroundColor Cyan
Write-Host "  This step takes a few minutes the first time (XGBoost, pandas, etc.)." -ForegroundColor DarkGray

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY
if ($LASTEXITCODE -ne 0) { Write-Host "ECR login failed." -ForegroundColor Red; exit 1 }

docker build --platform linux/amd64 -f Dockerfile.batch -t "${ECR_REPO}:${IMAGE_TAG}" .
if ($LASTEXITCODE -ne 0) { Write-Host "Docker build failed." -ForegroundColor Red; exit 1 }

docker tag "${ECR_REPO}:${IMAGE_TAG}" "${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
docker push "${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
if ($LASTEXITCODE -ne 0) { Write-Host "Docker push failed." -ForegroundColor Red; exit 1 }
Write-Host "  Pushed ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}" -ForegroundColor Green

# --------------------------------------------------------------- IAM role
Write-Host "`n[5/9] Creating IAM role for Lambda..." -ForegroundColor Cyan

$trustPolicy = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
$trustFile = New-TemporaryFile
Set-Content -Path $trustFile -Value $trustPolicy -NoNewline

aws iam get-role --role-name $ROLE_NAME 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    aws iam create-role --role-name $ROLE_NAME `
        --assume-role-policy-document "file://$trustFile" | Out-Null
    aws iam attach-role-policy --role-name $ROLE_NAME `
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole | Out-Null
    Write-Host "  Created role $ROLE_NAME." -ForegroundColor Green
} else {
    Write-Host "  Role $ROLE_NAME already exists, reusing it." -ForegroundColor Yellow
}

# Inline policy scoped to exactly this bucket -- not AmazonS3FullAccess.
$s3Policy = @"
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],"Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/*"]}]}
"@
$s3PolicyFile = New-TemporaryFile
Set-Content -Path $s3PolicyFile -Value $s3Policy -NoNewline
aws iam put-role-policy --role-name $ROLE_NAME --policy-name appraisalBatchS3Access `
    --policy-document "file://$s3PolicyFile" | Out-Null
Write-Host "  Attached S3 access scoped to s3://$BUCKET only." -ForegroundColor Green

Write-Host "  Waiting for IAM role propagation (roles are eventually consistent)..." -ForegroundColor DarkGray
Start-Sleep -Seconds 12

# ----------------------------------------------------------- Lambda function
Write-Host "`n[6/9] Creating the Lambda function..." -ForegroundColor Cyan
$ROLE_ARN = "arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
$IMAGE_URI = "${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"

aws lambda get-function --function-name $FUNCTION --region $REGION 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    aws lambda create-function `
        --function-name $FUNCTION `
        --package-type Image `
        --code ImageUri=$IMAGE_URI `
        --role $ROLE_ARN `
        --timeout $TIMEOUT_SEC `
        --memory-size $MEMORY_MB `
        --environment "Variables={JOBS_BUCKET=$BUCKET,OUTPUT_BUCKET=$BUCKET,N_BOOTSTRAP=300}" `
        --region $REGION | Out-Null
    Write-Host "  Created function $FUNCTION." -ForegroundColor Green

    Write-Host "  Waiting for the function to become active..." -ForegroundColor DarkGray
    aws lambda wait function-active --function-name $FUNCTION --region $REGION
} else {
    Write-Host "  Function $FUNCTION already exists — updating its image." -ForegroundColor Yellow
    aws lambda update-function-code --function-name $FUNCTION --image-uri $IMAGE_URI --region $REGION | Out-Null
    aws lambda wait function-updated --function-name $FUNCTION --region $REGION
}

# --------------------------------------------------------------- S3 trigger
Write-Host "`n[7/9] Wiring up the S3 trigger..." -ForegroundColor Cyan
$FUNCTION_ARN = (aws lambda get-function --function-name $FUNCTION --region $REGION --query 'Configuration.FunctionArn' --output text)

aws lambda add-permission --function-name $FUNCTION `
    --statement-id s3invoke --action lambda:InvokeFunction `
    --principal s3.amazonaws.com --source-arn "arn:aws:s3:::$BUCKET" `
    --region $REGION 2>&1 | Out-Null
# Exit code ignored deliberately: this fails harmlessly on re-run because the
# permission already exists, and there's no clean "does this exist" check.

$notifConfig = @"
{"LambdaFunctionConfigurations":[{"LambdaFunctionArn":"$FUNCTION_ARN","Events":["s3:ObjectCreated:*"],"Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"incoming/"}]}}}]}
"@
$notifFile = New-TemporaryFile
Set-Content -Path $notifFile -Value $notifConfig -NoNewline
# NOTE: put-bucket-notification-configuration REPLACES the bucket's entire
# trigger config, it does not add to it. Harmless on a fresh bucket. If you
# later add a second trigger to this same bucket by hand in the console,
# re-running this script will silently delete it -- edit the JSON above to
# include both configurations instead of running this step again.
aws s3api put-bucket-notification-configuration --bucket $BUCKET `
    --notification-configuration "file://$notifFile"
Write-Host "  Uploads under incoming/ now trigger $FUNCTION." -ForegroundColor Green

# --------------------------------------------------------------------- test
Write-Host "`n[8/9] Verifying with a synthetic test file..." -ForegroundColor Cyan
$testCsv = @"
Status,Close Price,Main Level SqFt,Upper SqFt,Apprx Below Grade Fin SqFt,Bedrooms,Baths Full,Baths Half,Garage Spaces,Fireplaces,Lot Size SqFt,Year Built,Close Date,Area,Concessions
Sold,410000,1500,900,700,4,3,1,2,1,11000,2005,2025-06-15,Carmel,0
Sold,455000,1650,950,750,4,3,1,2,1,11500,2008,2025-07-01,Carmel,0
Sold,380000,1400,850,650,3,2,1,2,0,10500,2001,2025-05-20,Westfield,0
"@
$testFile = Join-Path $env:TEMP "deploy-test.csv"
Set-Content -Path $testFile -Value $testCsv -NoNewline

aws s3 cp $testFile "s3://$BUCKET/incoming/analysis/deploy-test.csv" | Out-Null
Write-Host "  Uploaded test file. Waiting up to 90s for Lambda to process it..." -ForegroundColor DarkGray

$found = $false
foreach ($i in 1..18) {
    Start-Sleep -Seconds 5
    $listing = aws s3 ls "s3://$BUCKET/outputs/analysis/" --recursive 2>$null
    if ($listing) { $found = $true; break }
    $failed = aws s3 ls "s3://$BUCKET/failed/" --recursive 2>$null
    if ($failed) { break }
}

if ($found) {
    Write-Host "  SUCCESS. Output files:" -ForegroundColor Green
    aws s3 ls "s3://$BUCKET/outputs/analysis/" --recursive
} else {
    Write-Host "  No output yet. Check the failed/ folder and the Lambda logs:" -ForegroundColor Yellow
    aws s3 ls "s3://$BUCKET/failed/" --recursive
    Write-Host "    aws logs tail /aws/lambda/$FUNCTION --since 5m --region $REGION" -ForegroundColor Yellow
}

# --------------------------------------------------------------------- done
Write-Host "`n[9/9] Deployment complete." -ForegroundColor Cyan
Write-Host "==============================================================="
Write-Host "  Bucket:        s3://$BUCKET"
Write-Host "  Analysis jobs: s3://$BUCKET/incoming/analysis/<file.csv>"
Write-Host "  Valuation jobs:s3://$BUCKET/incoming/valuations/<file.csv>"
Write-Host "  Results land:  s3://$BUCKET/outputs/<job_type>/<timestamp-name>/"
Write-Host "  Logs:          aws logs tail /aws/lambda/$FUNCTION --follow --region $REGION"
Write-Host "==============================================================="
Write-Host "`nSave this bucket name -- $BUCKET -- you'll use it for every upload." -ForegroundColor Yellow
Write-Host "To promote a model to production, see 'Promoting an analysis to a"
Write-Host "deployed model' in DEPLOY_S3_PIPELINE.md."

Remove-Item $trustFile, $s3PolicyFile, $notifFile, $testFile -ErrorAction SilentlyContinue
