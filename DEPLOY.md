# Deploying — Windows 11 to ECS Fargate

## First, what actually gets deployed

You do not upload `.py` files to a server. The Dockerfile line `COPY app/ ./app/`
copies your Python **into an image**, and the image is the deployable unit:

```
app/*.py + requirements.txt
        │
        │  docker build          (on your Windows machine)
        ▼
   image: appraisal-api:v1       (Python, dependencies, and Linux, in one file)
        │
        │  docker push           (to AWS ECR, a private image registry)
        ▼
   ECR repository
        │
        │  ECS task definition points at the image
        ▼
   Fargate runs the container    ← your Python, executing in AWS
```

Your Windows machine never runs the Python for production. It builds a Linux
container and ships it. The `.md` files in this repo are instructions; the
`.py`, `.ts`, `.jsx`, `Dockerfile`, and `.ipynb` files are the product.

**Good news on Windows:** your machine is x86_64, the same architecture Fargate
defaults to. The `exec format error` trap that catches Apple Silicon users does
not apply to you. You can drop `--platform linux/amd64` from the build commands,
though leaving it in costs nothing and makes the command portable.

---

## Prerequisites

| Tool | Install | Check |
|---|---|---|
| Docker Desktop | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) — the installer enables WSL2 for you | `docker version` |
| AWS CLI v2 | [awscli.amazonaws.com/AWSCLIV2.msi](https://awscli.amazonaws.com/AWSCLIV2.msi) | `aws --version` |
| Python 3.12 | python.org or the Microsoft Store | `python --version` |

Docker Desktop must be **running**, not just installed — look for the whale in
your system tray. A stopped Docker Desktop produces `error during connect: ...
The system cannot find the file specified`, which reads like a missing file and
is actually a stopped service.

Use **PowerShell**, not Command Prompt. The multi-line commands below use the
backtick (`` ` ``) continuation character, which is PowerShell syntax.

---

## Before you touch AWS

Do these three things first. They take ten minutes and the third one is the
difference between a $4 project and a surprise bill.

1. **Enable MFA on your root account.** Then create an IAM user with
   `AdministratorAccess`, and work as that user from here on. Never use root
   for daily work.
2. **`aws configure`** — paste the IAM user's access key, and set your region.
   Pick one region and stay in it. This guide assumes `us-east-2`.
3. **Set AWS Budgets alerts at $5 and $20.** Billing console → Budgets → Create
   budget → Cost budget. This is the single most valuable ten minutes in the
   whole project.

### What this actually costs

ECS Fargate is **not free tier**. A 0.25 vCPU / 0.5 GB task running 24/7 costs
roughly **$9/month**, plus about **$16/month** if you put an Application Load
Balancer in front of it.

Two ways to keep it near zero:

- **Skip the ALB.** Give the task a public IP and hit it directly. Not how you
  would run real production, but it removes the single biggest line item and
  teaches you exactly the same things.
- **Scale the service to 0 tasks whenever you stop working.** One click in the
  console. Do it every single time.

ECR gives you 500 MB of storage free for 12 months, which holds several image
versions comfortably.

**Expected total if you are disciplined: $2–6 for the entire project.**

---

## Phase 2 — run the container on your own machine first

Do not deploy code you have not seen work locally. Debugging a container
through CloudWatch logs is much slower than debugging it in your own terminal.

```powershell
cd C:\path\to\appraisal-api
.\run_local.ps1
```

That script builds the image, mounts your `notebooks\artifacts\v1` folder into
the container as a read-only volume (so it serves the model from disk, no AWS
account needed), waits for `/health` to report the model loaded, and prints an
API key plus copy-paste test commands.

If PowerShell refuses to run the script, that is the default execution policy:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Open **http://localhost:8000/docs** — FastAPI generates an interactive page
where you can fill in a subject property and hit Execute. Click "Authorize"
first and paste the key.

**Done when:** `docker run` starts, `/health` returns `model_loaded: true`, and
`POST /predict` returns a sensible number.

---

## Phase 3 — get it into AWS

Set your variables once at the top of the PowerShell session. Everything below
reuses them.

```powershell
$env:AWS_REGION  = "us-east-2"
$ACCOUNT         = (aws sts get-caller-identity --query Account --output text)
$REPO            = "appraisal-api"
$BUCKET          = "yourname-appraisal-models"   # must be globally unique
$REGISTRY        = "$ACCOUNT.dkr.ecr.$env:AWS_REGION.amazonaws.com"
```

### 1. S3 bucket for the artifacts

```powershell
aws s3 mb "s3://$BUCKET" --region $env:AWS_REGION
aws s3 cp .\notebooks\artifacts\v1\ "s3://$BUCKET/models/v1/" --recursive
aws s3 ls "s3://$BUCKET/models/v1/"
```

You should see five files: `model.json`, `model_log.json`, `metadata.json`,
`adjustment_grid.json`, `reference.parquet`.

Alternatively, set `CONFIG["s3_bucket"]` in the notebook and re-run Step 16 — it
uploads for you.

### 2. Store the API keys in Secrets Manager

```powershell
python .\scripts\generate_api_key.py 2
```

Generate two: one for Base44, one for your own scripts. Then:

```powershell
aws secretsmanager create-secret `
    --name appraisal-api-keys `
    --secret-string "apr_KEY_ONE,apr_KEY_TWO" `
    --region $env:AWS_REGION
```

**Why Secrets Manager and not a plain environment variable in the task
definition:** anyone who can call `DescribeTaskDefinition` can read
`environment` values in plain text. A `secrets` entry stores only an ARN, and
ECS injects the value at container start.

### 3. ECR repository, and push the image

```powershell
aws ecr create-repository --repository-name $REPO --region $env:AWS_REGION

aws ecr get-login-password --region $env:AWS_REGION | `
    docker login --username AWS --password-stdin $REGISTRY

docker build --platform linux/amd64 -t "${REPO}:v1" .
docker tag "${REPO}:v1" "${REGISTRY}/${REPO}:v1"
docker push "${REGISTRY}/${REPO}:v1"
```

The first push uploads roughly 800 MB and takes a few minutes. Later pushes only
send changed layers, so if you edit `main.py` and rebuild, the push is seconds —
that is what the requirements-before-code ordering in the Dockerfile buys you.

### 4. The two IAM roles — this is the part that trips up everyone

They sound similar and do completely different things.

| Role | Used by | Grants |
|---|---|---|
| **Execution role** | ECS itself, before your container starts | Pull the image from ECR, write to CloudWatch Logs, read the secret |
| **Task role** | **Your Python code**, while running | Read the model from S3, write prediction logs to S3 |

Getting this wrong produces two distinct failures. A broken execution role means
the task never starts (`CannotPullContainerError`). A broken task role means the
container starts and then `/health` returns 503 with an `AccessDenied` from S3.

Create the execution role from AWS's managed policy:

```powershell
aws iam create-role --role-name appraisalExecutionRole `
    --assume-role-policy-document '{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}'

aws iam attach-role-policy --role-name appraisalExecutionRole `
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
```

It also needs permission to read the specific secret. In the IAM console, add an
inline policy to `appraisalExecutionRole` allowing
`secretsmanager:GetSecretValue` on your secret's ARN.

Create the task role, scoped to only your buckets:

```powershell
aws iam create-role --role-name appraisalTaskRole `
    --assume-role-policy-document '{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}'
```

Then attach an inline policy granting `s3:GetObject` on
`arn:aws:s3:::yourname-appraisal-models/*` and `s3:PutObject` on your prediction
log bucket. Do not attach `AmazonS3FullAccess` — the whole point of the task
role is that a compromised container can reach exactly two prefixes.

**There are no AWS access keys in the container. Ever.** `boto3` picks up
temporary credentials from the task role automatically.

### 5. Task definition

`ecs-task-definition.json` in this repo is a filled-in template. Replace
`ACCOUNT_ID`, `REGION`, and `BUCKET`, then:

```powershell
aws logs create-log-group --log-group-name /ecs/appraisal-api --region $env:AWS_REGION
aws ecs register-task-definition --cli-input-json file://ecs-task-definition.json
```

Note `MODEL_BUCKET` and `MODEL_PREFIX` in there — those are what switch the
container from local-disk mode to S3 mode. `API_KEYS` appears under `secrets`,
not `environment`.

### 6. Cluster, security group, service

```powershell
aws ecs create-cluster --cluster-name appraisal-cluster
```

Create a security group allowing inbound TCP 8000 **from your IP only**, not
`0.0.0.0/0`:

```powershell
$MYIP = (Invoke-RestMethod https://checkip.amazonaws.com).Trim()
$VPC  = (aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text)

$SG = (aws ec2 create-security-group --group-name appraisal-sg `
    --description "Appraisal API" --vpc-id $VPC --query GroupId --output text)

aws ec2 authorize-security-group-ingress --group-id $SG `
    --protocol tcp --port 8000 --cidr "$MYIP/32"
```

When you wire up Base44 you will need to widen this, because Base44's servers
call from their IPs, not yours. Two options: allow `0.0.0.0/0` on port 8000 and
rely on the API key as your only control, or put the service behind an ALB with
a proper certificate. For a learning project the first is acceptable **because
every endpoint except `/health` requires a key** — but know that you are then
relying entirely on that key, which is why it is 32 random bytes and not
`password123`.

Create the service with a public IP and no load balancer:

```powershell
$SUBNET = (aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" `
    --query "Subnets[0].SubnetId" --output text)

aws ecs create-service `
    --cluster appraisal-cluster `
    --service-name appraisal-svc `
    --task-definition appraisal-api `
    --desired-count 1 `
    --launch-type FARGATE `
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=ENABLED}"
```

### 7. Find your URL and test it

Fargate tasks get a new public IP every time they start. There is no stable
hostname without an ALB, so you look it up:

```powershell
$TASK = (aws ecs list-tasks --cluster appraisal-cluster --query "taskArns[0]" --output text)
$ENI  = (aws ecs describe-tasks --cluster appraisal-cluster --tasks $TASK `
    --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" --output text)
$IP   = (aws ec2 describe-network-interfaces --network-interface-ids $ENI `
    --query "NetworkInterfaces[0].Association.PublicIp" --output text)

Write-Host "API is at http://${IP}:8000"
Invoke-RestMethod "http://${IP}:8000/health"
```

**Write that IP down for Base44** — and remember it changes whenever the task is
replaced. If your Base44 app suddenly cannot connect, this is almost always why.

**Done when:** you `curl` a public AWS URL and get a prediction back.

---

## Turn it off when you stop

```powershell
aws ecs update-service --cluster appraisal-cluster --service appraisal-svc --desired-count 0
```

And back on:

```powershell
aws ecs update-service --cluster appraisal-cluster --service appraisal-svc --desired-count 1
```

Make this a habit. It is the difference between $4 and $40.

---

## GitHub Actions: push to `main`, service updates

`.github/workflows/deploy.yml` in this repo builds, pushes to ECR, and forces a
new ECS deployment on every push to `main`. Add these repository secrets under
Settings → Secrets and variables → Actions:

- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — a **deploy-only** IAM user, not
  your admin user
- `AWS_REGION`, `ECR_REPOSITORY`, `ECS_CLUSTER`, `ECS_SERVICE`

**Done when:** pushing a commit updates the running service with no manual steps.

---

## When it breaks

| Symptom | Cause |
|---|---|
| `error during connect ... system cannot find the file specified` | Docker Desktop is not running |
| `CannotPullContainerError` | Execution role cannot read ECR, or the image tag is wrong |
| Task starts then stops, no logs | The CloudWatch log group does not exist — create it first |
| `/health` returns 503 with `AccessDenied` | Task role cannot read the S3 model prefix |
| `/health` returns 503 with `NoSuchKey` | `MODEL_PREFIX` does not match where you uploaded |
| Every request returns 503 "No API keys configured" | The `secrets` entry did not resolve; check the secret ARN |
| `exec format error` | An arm64 image on x86_64 Fargate — not your problem on Windows |
| Connection times out from Base44 | Security group only allows your IP, or the task IP changed |

Read the logs before guessing:

```powershell
aws logs tail /ecs/appraisal-api --follow --region $env:AWS_REGION
```
