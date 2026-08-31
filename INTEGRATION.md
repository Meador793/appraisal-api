# Integrating another application with this API

Everything an outside app needs. The working client code is in `clients/` —
this document explains what it does and why.

---

## The four values another app needs

| Value | Example | Where it comes from |
|---|---|---|
| **Base URL** | `http://3.145.22.101:8000` | The public IP of your running ECS task (see DEPLOY.md step 7) |
| **Header name** | `X-API-Key` | Fixed |
| **API key** | `apr_qhqJxIO5mNS33sN3eUDugai7DAYPwmYg1WbKJhd2QUI` | `python scripts/generate_api_key.py` |
| **Content type** | `application/json` | Fixed, for POSTs |

That is the entire contract. Every endpoint except `/health` requires the key.

Generate a **separate key for each consumer**. `API_KEYS` accepts a
comma-separated list, so you can revoke Base44's key without breaking your own
scripts. Rotating without downtime: add the new key, redeploy, move consumers
over, remove the old key, redeploy again.

> The base URL changes whenever the Fargate task is replaced. If an integration
> stops connecting, check the IP before you check anything else.

---

## Endpoints

| Method | Path | Returns | Auth |
|---|---|---|---|
| `GET` | `/health` | Service and model status | No |
| `GET` | `/model-info` | Version, metrics, XGBoost vs RF comparison, valid locations | Yes |
| `GET` | `/adjustment-grid` | The market grid alone, no subject needed | Yes |
| `POST` | `/predict` | Indicated value + adjustments, JSON | Yes |
| `POST` | `/report` | The same analysis as a **PDF** | Yes |
| `GET` | `/docs` | Interactive API explorer | No |
| `GET` | `/openapi.json` | Machine-readable schema | No |

`/openapi.json` is worth knowing about: paste that URL into Base44's AI chat,
or into Postman's import, and the tool generates the client for you.

---

## Request shape

```json
{
  "subject": {
    "main_sqft": 1800,
    "upper_sqft": 900,
    "bsmt_fin_sqft": 800,
    "bedrooms": 4,
    "baths_full": 3,
    "baths_half": 1,
    "garage_spaces": 3,
    "fireplaces": 1,
    "lot_sqft": 12000,
    "year_built": 2005,
    "location": "Zionsville",
    "effective_date": "2026-08-30",
    "address": "1234 Example Lane, Carmel IN 46032",
    "file_number": "2026-0817"
  },
  "scenarios": [
    { "feature": "garage_spaces", "values": [1, 2, 3] }
  ]
}
```

Every field is optional except a size — you must supply `gla_sqft`, or
`main_sqft` and/or `upper_sqft`. Anything else you omit is filled with the
training sample's median, and the response lists exactly what was filled in
`defaulted_fields`. Nothing is silently invented.

`address`, `file_number`, `client_name`, and `appraiser_name` appear on the PDF
and are **never** used as model inputs.

`location` must match a level from training. `GET /model-info` returns
`location_levels` — populate a dropdown from it rather than letting users type,
because an unmatched location silently falls back to the baseline (the response
warns you, but a dropdown prevents the problem entirely).

---

## Response shape

```json
{
  "indicated_value": 657153.0,
  "value_range_low": 627418.0,
  "value_range_high": 686888.0,
  "model_version": "v1",
  "location_used": "Zionsville",
  "location_matched": true,
  "defaulted_fields": [],
  "warnings": [],
  "adjustment_grid": [
    {
      "feature": "gla_sqft",
      "unit": "per 100 sq ft of above-grade GLA",
      "adjustment": 12718.2,
      "ci_lower_95": 12180.9,
      "ci_upper_95": 13297.8,
      "share_responsive": 0.99,
      "use_in_grid": "YES - supported"
    }
  ],
  "percent_grid": [ ... ],
  "location_adjustments": [ ... ],
  "contributions": [ ... ],
  "scenarios": { ... }
}
```

**Display `warnings` and `defaulted_fields` in your UI.** They are the
difference between a number an appraiser can rely on and one that was quietly
pinned to the edge of the training data. Do not log them to the console and move
on. Likewise, `use_in_grid` is a decision, not decoration — an adjustment whose
confidence interval spans zero has not been shown to differ from no effect, and
showing it in the same style as a supported one is misleading.

### Status codes

| Code | Meaning | What to do |
|---|---|---|
| `200` | Fine | — |
| `401` | No `X-API-Key` header | Add the header |
| `403` | Key rejected | Check against `API_KEYS` in the task definition |
| `422` | Validation failure | `detail` names the offending field |
| `503` | Service up, model not loaded | Check the task role's S3 permissions |
| Timeout | Task scaled to 0, or IP changed | Scale to 1, or look up the new IP |

---

## Base44

Base44 is the case that most needs getting right, because the obvious approach
is insecure.

### The rule: the API key never goes in frontend code

Anything in your React frontend ships to the user's browser. An API key there is
not protected by minification, by an environment variable, or by only being used
inside a `fetch` call. Open devtools → Network → click the request → read the
headers. Whoever finds it can run your ECS task on your bill until you rotate.

Base44's answer is **backend functions** (Builder plan or higher): TypeScript
that runs on Base44's servers, with credentials stored as **Secrets** under
Dashboard → Code. The browser calls the function; the function calls your API
with the key.

```
Browser (React)  ──►  Base44 backend function  ──►  Your ECS API
   no key                 holds the key              validates the key
```

### Setup

**1.** Dashboard → Code → Secrets, create two:

```
APPRAISAL_API_URL   http://<your-ecs-public-ip>:8000
APPRAISAL_API_KEY   apr_...   (the key you generated for Base44)
```

**2.** Dashboard → Code → Functions, create a function named `appraise` and
paste in `clients/base44_backend_function.ts`. It handles CORS preflight,
forwards only known fields, passes PDF bytes straight through, and returns a
readable error when the ECS service is scaled to zero.

**3.** Build the UI. Either paste `clients/base44_frontend.jsx` into a page
component, or hand both files to the Base44 AI chat with:

> I have a backend function called `appraise` that proxies to a property
> valuation API. Build me a form that collects the subject property fields,
> calls the function, shows the indicated value and adjustment grid, and has a
> button to download the PDF report.

**4.** Open the security group. Base44's servers call from their IPs, not yours,
so a security group locked to your `/32` will time out. See the security group
section in DEPLOY.md for the tradeoff.

### The PDF download in a browser

A browser cannot "save" a response by itself. Convert the bytes to a Blob,
create a temporary object URL, click a hidden link, then **revoke the URL** —
skipping the revoke leaks memory on every report generated. That is what the
`downloadPdf` function in `clients/base44_frontend.jsx` does.

---

## Other integration paths

### Python (Django, Flask, Streamlit, a script)

`clients/python_client.py` is a self-contained class with no dependency on the
rest of this repo. Copy it in.

```python
from python_client import AppraisalClient

client = AppraisalClient("http://3.145.22.101:8000", "apr_yourkey")
result = client.predict({"main_sqft": 1800, "upper_sqft": 900, "bedrooms": 4})
print(result["indicated_value"])

client.report_pdf(subject, "reports/2026-0817.pdf")
```

It uses a `requests.Session`, which reuses the TCP connection — on a batch of a
few hundred properties that is the difference between seconds and minutes.

### PowerShell

```powershell
$body = '{"subject":{"main_sqft":1800,"upper_sqft":900,"bedrooms":4,"baths_full":3}}'
$headers = @{ "X-API-Key" = "apr_yourkey" }

Invoke-RestMethod -Method Post "http://YOUR-IP:8000/predict" `
    -Headers $headers -ContentType "application/json" -Body $body

Invoke-WebRequest -Method Post "http://YOUR-IP:8000/report" `
    -Headers $headers -ContentType "application/json" -Body $body -OutFile report.pdf
```

### Zapier, Make, Power Automate

Any tool with a generic "HTTP request" step works. Method `POST`, URL
`http://YOUR-IP:8000/predict`, header `X-API-Key`, body the JSON above. These
platforms store credentials server-side, so the key is safe there.

### Excel / Power Query

Power Query can POST, but it stores credentials in the workbook, which then gets
emailed around. Prefer the Python client writing a `.xlsx`, or a Power Automate
flow that keeps the key on the server.

---

## Security checklist before you share the URL

- [ ] A distinct key per consumer, each 32+ random bytes from `secrets.token_urlsafe`
- [ ] Keys in AWS Secrets Manager, referenced as `secrets` in the task definition — **not** `environment`
- [ ] No key in any file committed to git (`.env` is in `.dockerignore`; add it to `.gitignore` too)
- [ ] No key in frontend JavaScript, ever
- [ ] `CORS_ORIGINS` set to your actual app origin, not `*`
- [ ] Budget alerts active at $5 and $20
- [ ] Service scaled to 0 when you are not using it

### What this API deliberately does not have

Honest gaps, worth being able to name if someone asks:

- **No rate limiting.** A leaked key can be used without limit until you rotate
  it. Real production puts an API Gateway or ALB with WAF in front.
- **No HTTPS.** Traffic to `http://IP:8000` is plaintext, so the API key travels
  in the clear. Fine on a learning project; not fine with real client data. The
  fix is an ALB with an ACM certificate, which is the $16/month you skipped.
- **No per-key audit trail beyond a fingerprint.** Prediction logs record the
  first 8 hex of the key's SHA-256, enough to tell consumers apart, not enough
  for a compliance audit.

Being able to explain *why* you made those tradeoffs is worth more in an
interview than having quietly avoided them.
