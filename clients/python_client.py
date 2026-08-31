"""
Python client for the Appraisal Adjustment API.

Run it:
    pip install requests
    set APPRAISAL_API_URL=http://<your-ecs-ip>:8000      (Windows CMD)
    set APPRAISAL_API_KEY=<your key>
    python python_client.py

In PowerShell use  $env:APPRAISAL_API_URL = "http://..."  instead of `set`.

Copy the AppraisalClient class into any Python project -- Django, Flask, a
Streamlit dashboard, an Excel-writing script. It has no dependency on the rest
of this repository.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests


class AppraisalAPIError(RuntimeError):
    """Raised for any non-2xx response, with the server's message attached."""


class AppraisalClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: int = 30):
        self.base_url = (base_url or os.environ["APPRAISAL_API_URL"]).rstrip("/")
        self.api_key = api_key or os.environ["APPRAISAL_API_KEY"]
        self.timeout = timeout
        # A Session reuses the TCP connection across calls. On a batch of a few
        # hundred properties this is the difference between seconds and minutes.
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------ core
    def _request(self, method: str, path: str, **kw):
        try:
            r = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kw)
        except requests.exceptions.ConnectionError as exc:
            raise AppraisalAPIError(
                f"Could not reach {self.base_url}. If the ECS service is scaled to 0 tasks, "
                "scale it to 1. If the task was replaced, its public IP changed."
            ) from exc

        if r.status_code == 401:
            raise AppraisalAPIError("401: no X-API-Key header was sent.")
        if r.status_code == 403:
            raise AppraisalAPIError("403: the key was rejected. Check it against the API_KEYS "
                                    "value in the ECS task definition.")
        if r.status_code == 422:
            raise AppraisalAPIError(f"422 validation error: {r.json().get('detail')}")
        if r.status_code == 503:
            raise AppraisalAPIError(f"503: the service is up but the model did not load. {r.text}")
        if not r.ok:
            raise AppraisalAPIError(f"{r.status_code}: {r.text[:400]}")
        return r

    # --------------------------------------------------------------- methods
    def health(self) -> dict:
        """No API key needed. This is what ECS itself calls."""
        return requests.get(f"{self.base_url}/health", timeout=self.timeout).json()

    def model_info(self) -> dict:
        return self._request("GET", "/model-info").json()

    def adjustment_grid(self) -> dict:
        """The market evidence on its own -- no subject property required."""
        return self._request("GET", "/adjustment-grid").json()

    def predict(self, subject: dict, scenarios: list | None = None, **opts) -> dict:
        body = {"subject": subject, **opts}
        if scenarios:
            body["scenarios"] = scenarios
        return self._request("POST", "/predict", json=body).json()

    def report_pdf(self, subject: dict, out_path: str | Path,
                   scenarios: list | None = None,
                   title: str = "Market-Derived Adjustment Analysis") -> Path:
        """Fetch the PDF and write it to disk. Returns the path."""
        body = {"subject": subject, "report_title": title}
        if scenarios:
            body["scenarios"] = scenarios
        r = self._request("POST", "/report", json=body)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.content)
        return out


# ==========================================================================
# Example usage
# ==========================================================================
if __name__ == "__main__":
    client = AppraisalClient()

    print("Health:", client.health())

    info = client.model_info()
    print(f"\nModel {info['model_version']} trained {info['trained_at']}")
    print(f"Trained on {info['n_training_sales']:,} closed sales")
    print(f"Known locations: {', '.join(info['location_levels'])}")
    cmp_ = info["model_comparison"]
    print(f"XGBoost R2 {cmp_['xgboost']['r2']:.4f}  vs  "
          f"Random Forest R2 {cmp_['random_forest']['r2']:.4f}")

    subject = {
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
        "file_number": "2026-0817",
        "client_name": "First Meridian Bank",
        "appraiser_name": "J. Doe, SRA",
    }

    scenarios = [
        {"feature": "garage_spaces", "values": [1, 2, 3]},
        {"feature": "baths_full", "values": [2, 3, 4]},
    ]

    result = client.predict(subject, scenarios=scenarios)

    def money(v):
        return ("-$" if v < 0 else "$") + f"{abs(v):,.0f}"

    print(f"\nINDICATED VALUE: {money(result['indicated_value'])}")
    print(f"Supported range: {money(result['value_range_low'])} to "
          f"{money(result['value_range_high'])}")

    # Always surface these. A warning is the difference between a number you can
    # rely on and one that was quietly pinned to the edge of the training data.
    for w in result["warnings"]:
        print(f"  WARNING: {w}")
    if result["defaulted_fields"]:
        print(f"  Filled with sample medians: {', '.join(result['defaulted_fields'])}")

    print("\nADJUSTMENT GRID")
    for row in result["adjustment_grid"]:
        flag = "" if row["use_in_grid"].startswith("YES") else "   <-- NOT SUPPORTED"
        print(f"  {money(row['adjustment']):>12}  {row['unit']:<36} "
              f"[{money(row['ci_lower_95'])} to {money(row['ci_upper_95'])}]{flag}")

    print("\nPAIRED SCENARIOS")
    for feature, rows in (result.get("scenarios") or {}).items():
        print(f"  {feature}:")
        for r in rows:
            step = "" if r["difference"] is None else f"   (step {money(r['difference'])})"
            print(f"    {r['value']:>6.0f} -> {money(r['indicated_value'])}{step}")

    path = client.report_pdf(subject, "reports/subject-2026-0817.pdf", scenarios=scenarios)
    print(f"\nPDF written to {path.resolve()} ({path.stat().st_size:,} bytes)")
