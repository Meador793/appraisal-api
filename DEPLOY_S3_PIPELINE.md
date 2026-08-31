# S3-Triggered Pipeline — Deployment

Upload a file to one bucket, get results in another. No API key, no public IP,
no always-on container.

```
   You upload                                      Results appear
        │                                                 ▲
        ▼                                                 │
s3://appraisal-jobs/incoming/analysis/carmel-2026.csv     │
        │                                                 │
        │  S3 PutObject event                             │
        ▼                                                 │
   Lambda (container image from ECR)                      │
        │                                                 │
        │  trains XGBoost + Random Forest, builds PDF/XLSX │
        ▼                                                 │
s3://appraisal-jobs/outputs/analysis/2026-08-30-1412-carmel-2026/
        market_report.pdf
        adjustment_analysis.xlsx
        summary.json
        run_log.txt
        artifacts/{model.json, metadata.json, reference.parquet, adjustment_grid.json}
```

---

## Two job types, routed by folder

| Drop the file here | What happens |
|---|---|
| `incoming/analysis/` | Trains XGBoost and Random Forest on the export, compares them, produces the adjustment grid with confidence intervals, and writes deployable model artifacts. The notebook, run headless. |
| `incoming/valuations/` | Scores a list of subject properties against an already-trained model. Trains nothing. Needs `MODEL_BUCKET` / `MODEL_PREFIX` set. |

The folder is the router because it's the one thing you can't forget to supply
— you have to put the file *somewhere*.

Anything dropped elsewhere is ignored rather than guessed at.

### Per-file column overrides

Upload `carmel-2026.config.json` next to `carmel-2026.csv` and it merges over
the defaults. A market with renamed columns needs no redeploy:

```json
{
  "cols": {
    "bsmt_fin_sqft": "Below Grade Finished SF",
    "fireplaces": null
  },
  "min_location_count": 10,
  "train_through": "2025-06-30"
}
```

`null` means the field doesn't exist in that export. It's dropped from the model
and recorded as absent — not valued at zero.

---

## Setup

Set your variables once per PowerShell session:

```powershell
$env:AWS_REGION = "us-east-2"
$ACCOUNT  = (aws sts get-caller-identity --query Account --output text)
$JOBS     = "yourname-appraisal-jobs"      # must be globally unique
$REGISTRY = "$ACCOUNT.dkr.ecr.$env:AWS_REGION.amazonaws.com"
```

### 1. Bucket

One bucket with folders is simpler than three buckets and costs the same. Use
separate buckets only if different people should see inputs and outputs.

```powershell
aws s3 mb "s3://$JOBS" --region $env:AWS_REGION

# Block public access — this bucket will hold client property data
aws s3api put-public-access-block --bucket $JOBS `
    --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

### 2. Build and push the batch image

```powershell
aws ecr create-repository --repository-name appraisal-batch --region $env:AWS_REGION

aws ecr get-login-password --region $env:AWS_REGION | `
    docker login --username AWS --password-stdin $REGISTRY

docker build --platform linux/amd64 -f Dockerfile.batch -t appraisal-batch:v1 .
docker tag appraisal-batch:v1 "$REGISTRY/appraisal-batch:v1"
docker push "$REGISTRY/appraisal-batch:v1"
```

### 3. Execution role for the Lambda

```powershell
aws iam create-role --role-name appraisalBatchRole `
    --assume-role-policy-document '{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"lambda.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}'

aws iam attach-role-policy --role-name appraisalBatchRole `
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

Then add an inline policy granting `s3:GetObject` and `s3:PutObject` on
`arn:aws:s3:::yourname-appraisal-jobs/*`. Scope it to that bucket — not
`AmazonS3FullAccess`.

### 4. Create the function

```powershell
aws lambda create-function `
    --function-name appraisal-batch `
    --package-type Image `
    --code ImageUri="$REGISTRY/appraisal-batch:v1" `
    --role "arn:aws:iam::${ACCOUNT}:role/appraisalBatchRole" `
    --timeout 900 `
    --memory-size 3008 `
    --environment "Variables={JOBS_BUCKET=$JOBS,OUTPUT_BUCKET=$JOBS,N_BOOTSTRAP=300}" `
    --region $env:AWS_REGION
```

**Memory is also CPU on Lambda** — they're allocated together. At 3008 MB you
get roughly 2 vCPUs, which matters because XGBoost training and the bootstrap
are CPU-bound. At 512 MB the same job takes many times longer and can time out,
so the low setting is not the cheap one.

**Timeout 900s** is the maximum. A typical few-thousand-row export finishes in
under a minute.

### 5. Wire up the trigger

```powershell
aws lambda add-permission --function-name appraisal-batch `
    --statement-id s3invoke --action lambda:InvokeFunction `
    --principal s3.amazonaws.com --source-arn "arn:aws:s3:::$JOBS"
```

Then in the S3 console: bucket → Properties → Event notifications → Create.
Event type **All object create events**, prefix **`incoming/`**, destination
your Lambda.

> **Set the prefix.** Without it, the outputs the function writes land back in
> the same bucket and trigger the function again — which writes more outputs,
> which trigger it again. `batch.py` ignores anything outside
> `incoming/analysis/` and `incoming/valuations/` as a second line of defence,
> but the prefix filter is what stops the invocation from happening at all.

### 6. Test

```powershell
aws s3 cp .\carmel_2026.csv "s3://$JOBS/incoming/analysis/carmel_2026.csv"

# wait ~60s, then
aws s3 ls "s3://$JOBS/outputs/analysis/" --recursive
aws s3 cp "s3://$JOBS/outputs/analysis/<folder>/market_report.pdf" .
```

Watch it run:

```powershell
aws logs tail /aws/lambda/appraisal-batch --follow --region $env:AWS_REGION
```

---

## Failures are outputs too

A job that fails writes `failed/<timestamp>-<name>/error.txt` and returns
success to the trigger.

That's deliberate. If the handler raised, Lambda would retry the same broken CSV
twice more on its own schedule, and your only record would be three identical
stack traces in CloudWatch. An error file sitting next to your input, naming the
column that was missing, is a better artifact than a retry storm.

Common contents of that file:

| Message | Cause |
|---|---|
| `Required column(s) not found: ['Close Price']` | Column names differ — add a sidecar config |
| `Could not decode ... as UTF-8, cp1252, or latin-1` | Unusual encoding; re-save as UTF-8 CSV |
| `No model available` | A valuation job with no `MODEL_PREFIX` set |
| `Task timed out after 900.00 seconds` | Lower `N_BOOTSTRAP`, or move to ECS |

---

## Promoting an analysis to a deployed model

Every analysis job writes an `artifacts/` folder that is exactly what the API
loads. To serve a new market:

```powershell
aws s3 cp "s3://$JOBS/outputs/analysis/2026-08-30-1412-carmel-2026/artifacts/" `
          "s3://$JOBS/models/carmel-v1/" --recursive
```

Then set `MODEL_PREFIX=models/carmel-v1` on whatever serves it.

**Promotion is a deliberate, manual step, and should stay that way.** An
automatic path from "someone uploaded a CSV" to "production is serving a new
model" means a bad export silently changes every adjustment in every report.
Look at `summary.json` and the range-restriction flags first.

---

## Cost

| | |
|---|---|
| Lambda | 1M requests + 400,000 GB-seconds free per month, permanently. A 60s job at 3 GB is 180 GB-seconds — roughly **2,000 free runs a month**. |
| S3 | 5 GB free for 12 months, then ~$0.023/GB/month |
| ECR | 500 MB free for 12 months; this image is ~1 GB, so ~$0.05/month |

**Realistically $0–1/month.** Nothing runs between uploads, so there is nothing
to remember to scale down. That is the main practical advantage over the
always-on ECS service, alongside having no API key to rotate and no public IP
that changes on every restart.

---

## Keeping the API as well

The two are not exclusive and they share `app/`. Run both if you want the
learning value of an ECS service (task definitions and the two IAM roles are
the part of AWS worth understanding) plus the convenience of file-drop batch
jobs. `DEPLOY.md` covers the API; this document covers the pipeline.

If you only want one: **this one.** It does everything the API does except
respond in real time, and real-time isn't a requirement when the consumer is
you uploading a spreadsheet.
