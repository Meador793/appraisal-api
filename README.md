# Appraisal Adjustment API

Market-derived adjustments and indicated value for residential property, from
gradient-boosted trees on closed MLS sales. Phase 1 (notebook + artifacts) and
Phase 2 (containerised API) of the ML-in-production path, with Phase 3
deployment instructions.

---

## What is code and what is documentation

Reasonable thing to check. Here is the split:

**Runnable code — this is the product:**

```
app/features.py               feature engineering (shared by notebook AND API)
app/adjustments.py            marginal-effect adjustment engine
app/model.py                  loads artifacts from S3 at startup
app/schemas.py                request/response validation
app/auth.py                   API key checking
app/report.py                 PDF generation
app/logging_sink.py           buffered prediction logging to S3
app/main.py                   the FastAPI service
Dockerfile                    packages all of the above into an image
requirements.txt              pinned dependencies
run_local.ps1                 one-command build-and-run on Windows
test_api.py                   32 integration checks
scripts/generate_api_key.py   key generator
ecs-task-definition.json      AWS deployment config
.github/workflows/deploy.yml  CI/CD
notebooks/Phase1_*.ipynb      the training notebook, with outputs
clients/python_client.py      copy-paste client for any Python app
clients/base44_backend_function.ts    Base44 server-side proxy
clients/base44_frontend.jsx           Base44 UI component
```

**Documentation:** `README.md`, `DEPLOY.md`, `INTEGRATION.md`. Three files out
of twenty.

**The `.py` files are what gets deployed.** The Dockerfile's `COPY app/ ./app/`
puts them inside a Linux image; that image runs on ECS. You never upload
individual Python files to a server.

---

## Start here

### 1. Train the model

Open `notebooks/Phase1_Appraisal_XGB_vs_RF.ipynb`. Edit the `CONFIG` dictionary
in Step 2 to point at your MLS export. Run top to bottom.

If your Excel file is not found, the notebook generates synthetic data so the
whole pipeline still runs — useful for wiring up Phases 2 to 4, worthless for
anything else, and it prints a loud banner so you cannot forget which mode you
are in.

Step 16 writes five artifacts. Step 17 verifies a fresh reload reproduces the
same prediction.

### 2. Run the API on your machine

```powershell
.\run_local.ps1
```

Then open **http://localhost:8000/docs** for an interactive API explorer.

### 3. Deploy

`DEPLOY.md`, written for Windows 11 + Docker Desktop.

### 4. Connect another app

`INTEGRATION.md`, including the Base44 setup.

---

## The model

**XGBoost with monotone constraints**, benchmarked against a Random Forest on
a time-based holdout, with an OLS cross-check on every adjustment.

Tree ensembles have no coefficients, so adjustments come from **counterfactual
marginal effects**: take every closed sale, add one unit of the characteristic,
hold everything else fixed, re-predict, average the difference. That estimates
the same quantity a regression coefficient does, without assuming the effect is
identical for every property.

Three things this gets right that a naive port would not:

**Monotone constraints.** Unconstrained boosting on a few hundred sales produces
grids where the third garage bay is worth less than the second. Real noise in
the sample, indefensible in a report. Constraints forbid the reversal during
training.

**An OLS cross-check column.** A boosted model is the better *predictor* and the
worse *instrument* — boosting spreads credit across correlated features and
shrinks each toward zero. Where XGBoost and OLS agree, the adjustment is robust.
Where they diverge, the correlation matrix tells you whether it is nonlinearity
or instability.

**`Share_Responsive`.** Trees are step functions, so for some sales a one-unit
bump crosses no split point and the prediction does not move. This column
reports the fraction that responded. A low share means the average is carried
by a minority of records — a tree-specific failure mode with no regression
equivalent, and a quiet one.

Confidence intervals are percentile bootstraps, and the decision rule is
unchanged from ordinary regression: **an interval spanning zero does not go in
the grid.**

---

## Avoiding training/serving skew

`app/features.py` is **imported** by both the notebook and the API. Not copied.
There is no manual step where the two can drift apart.

Two further guards: `app/model.py` refuses to start if `reference.parquet`
column order disagrees with `metadata.json`, and `build_serving_row` reindexes
every request against the recorded training order. The notebook's Step 17 asserts
that a reloaded model reproduces the in-memory prediction exactly.

This matters because the failure is silent. Swap two columns and XGBoost does
not raise — it returns a confident, wrong dollar value.

---

## Endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | Service and model status (no auth — ECS calls this) |
| `GET` | `/model-info` | Version, metrics, XGBoost vs RF comparison |
| `GET` | `/adjustment-grid` | The market grid alone |
| `POST` | `/predict` | Indicated value + adjustments, JSON |
| `POST` | `/report` | The same analysis as a downloadable **PDF** |
| `GET` | `/docs` | Interactive explorer |

All except `/health` and `/docs` require the `X-API-Key` header.

---

## Tests

```powershell
$env:LOCAL_MODEL_DIR = ".\notebooks\artifacts\v1"
$env:API_KEYS = "test_key_abc123"
python test_api.py
```

32 checks: auth (401/403/200, multiple keys), prediction shape, monotonicity of
scenario output, validation returning 422 rather than 500, out-of-range and
unknown-location warnings, and PDF magic bytes, EOF marker, headers, and size.

---

## Cost

Fargate is not free tier. Roughly **$9/month** for a 0.25 vCPU task running 24/7,
plus ~$16/month for a load balancer you can skip. Scale the service to 0 when
you stop working and the whole project runs **$2–6**. Set AWS Budgets alerts at
$5 and $20 before you deploy anything.

---

## Known limitations

- **No HTTPS** without an ALB, so the API key travels in plaintext. Fine for
  learning, not for real client data.
- **No rate limiting.** A leaked key works until rotated.
- **Tree models do not extrapolate.** A subject outside the training range is
  valued at the edge of it. The API warns; it cannot fix.
- **Condition, quality, updates, and view are not in an MLS export** and are
  often the largest real difference between two comparables.
- **This is a statistical support tool**, not a value opinion, and does not
  satisfy USPAP on its own.

---

## Reusing this on another dataset

Edit `CONFIG` in Step 2 of the notebook — the `cols` mapping is there so a
renamed MLS column costs you one line, not a rewrite. Then give the dataset its
own `model_version` and run top to bottom.

Three things the pipeline handles for you:

- **A field your export lacks.** Set it to `None` in `cols`. The feature is
  dropped from the model and recorded in metadata as absent, rather than
  modelled as a column of zeros. That distinction matters: "the market pays
  nothing for a fireplace" and "this export has no fireplace column" must not
  look the same in an adjustment grid.
- **A field with no variation.** If every sale is a 2-car garage, the feature is
  dropped for the same reason.
- **Version collisions.** Writing a second dataset's artifacts over a first is
  refused with an error naming both datasets. Reusing `v1` across markets would
  otherwise leave a deployed API quietly serving the wrong one.

### Comparing several datasets

```powershell
python scripts\compare_datasets.py
```

Edit the `DATASETS` list at the top. Each entry trains its **own** model and
writes its **own** artifacts — nothing is pooled. The script prints a model
comparison, the adjustments side by side, and the data-adequacy check per market.

**Do not compare R-squared across datasets.** It is the share of price variation
explained, so a market with wide price dispersion scores higher than a tightly
banded one even when the model is worse. In testing, a deliberately
range-restricted export produced the *best* MAE (5.62% of mean price) and the
*worst* R-squared (0.378) at the same time. Compare **MAE as a percentage of
mean price** across markets, and the **XGBoost-minus-RF gap** within each one.
