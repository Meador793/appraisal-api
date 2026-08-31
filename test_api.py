"""
In-process integration test. Runs the real app through FastAPI's TestClient --
same routing, same auth, same PDF builder, no network.

    LOCAL_MODEL_DIR=./artifacts/v1 API_KEYS=test_key_abc123 python test_api.py
"""
import json
import os
import sys

os.environ.setdefault("LOCAL_MODEL_DIR", os.path.join(os.path.dirname(__file__), "artifacts/v1"))
os.environ.setdefault("API_KEYS", "test_key_abc123,second_key_xyz")

from fastapi.testclient import TestClient

from app.main import app

KEY = {"X-API-Key": "test_key_abc123"}
PAYLOAD = {
    "subject": {
        "main_sqft": 1800, "upper_sqft": 900, "bsmt_fin_sqft": 800,
        "bedrooms": 4, "baths_full": 3, "baths_half": 1, "garage_spaces": 3,
        "fireplaces": 1, "lot_sqft": 12000, "year_built": 2005,
        "location": "Zionsville", "effective_date": "2025-08-01",
        "address": "1234 Example Lane, Carmel IN 46032", "file_number": "2026-0817",
        "client_name": "First Meridian Bank", "appraiser_name": "J. Doe, SRA",
    },
    "scenarios": [
        {"feature": "garage_spaces", "values": [1, 2, 3]},
        {"feature": "baths_full", "values": [2, 3, 4]},
    ],
}

results = []


def check(label, ok, extra=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


with TestClient(app) as client:
    print("HEALTH AND AUTH")
    r = client.get("/health")
    check("health 200 with model loaded", r.status_code == 200 and r.json()["model_loaded"])
    check("no key -> 401", client.get("/model-info").status_code == 401)
    check("bad key -> 403", client.get("/model-info", headers={"X-API-Key": "nope"}).status_code == 403)
    check("good key -> 200", client.get("/model-info", headers=KEY).status_code == 200)
    check("second configured key also works",
          client.get("/model-info", headers={"X-API-Key": "second_key_xyz"}).status_code == 200)

    print("\nPREDICT")
    r = client.post("/predict", json=PAYLOAD, headers=KEY)
    check("predict 200", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
    d = r.json()
    check("indicated value is plausible", 100_000 < d["indicated_value"] < 5_000_000,
          f"${d['indicated_value']:,.0f}")
    check("location matched", d["location_matched"] and d["location_used"] == "Zionsville")
    check("nothing silently defaulted", d["defaulted_fields"] == [], str(d["defaulted_fields"]))
    check("adjustment grid returned", len(d["adjustment_grid"]) == 10)
    check("percent grid returned", len(d["percent_grid"]) == 10)
    check("contributions returned", len(d["contributions"]) > 0)
    check("both scenarios returned", set(d["scenarios"]) == {"garage_spaces", "baths_full"})
    gar = [s["indicated_value"] for s in d["scenarios"]["garage_spaces"]]
    check("garage scenario is monotone (constraint held)", gar == sorted(gar), str(gar))

    print("\nVALIDATION AND WARNINGS")
    r = client.post("/predict", json={"subject": {"bedrooms": 3}}, headers=KEY)
    check("missing size -> 422 not 500", r.status_code == 422)
    r = client.post("/predict", json={"subject": {"gla_sqft": 2000, "bedrooms": -4}}, headers=KEY)
    check("negative bedrooms -> 422", r.status_code == 422)
    r = client.post("/predict", headers=KEY, json={
        "subject": {"gla_sqft": 2000}, "scenarios": [{"feature": "pool", "values": [0, 1]}]})
    check("unknown scenario feature -> 422 naming valid features",
          r.status_code == 422 and "gla_sqft" in r.text)
    r = client.post("/predict", json={
        "subject": {"gla_sqft": 9500, "location": "Broad Ripple"}}, headers=KEY)
    w = r.json()["warnings"]
    check("out-of-range GLA warns", any("outside the training range" in x for x in w))
    check("unknown location warns and falls back",
          any("not a level in the training data" in x for x in w)
          and not r.json()["location_matched"])
    check("defaulted fields reported", len(r.json()["defaulted_fields"]) > 5)

    print("\nGRID ENDPOINT")
    r = client.get("/adjustment-grid", headers=KEY)
    check("adjustment-grid 200", r.status_code == 200)
    check("grid has dollar/percent/location", all(k in r.json() for k in ["dollar", "percent", "location"]))

    print("\nPDF REPORT")
    r = client.post("/report", json=PAYLOAD, headers=KEY)
    check("report 200", r.status_code == 200, r.text[:200] if r.status_code != 200 else "")
    body = r.content
    check("content-type is application/pdf", r.headers["content-type"] == "application/pdf")
    check("starts with %PDF magic bytes", body[:4] == b"%PDF")
    check("has EOF marker", b"%%EOF" in body[-2048:])
    check("attachment filename set", "attachment; filename=" in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition", ""))
    check("X-Indicated-Value header present", "x-indicated-value" in {k.lower() for k in r.headers})
    check("CORS expose-headers set for browser fetch()",
          "Content-Disposition" in r.headers.get("access-control-expose-headers", ""))
    check("PDF is a real size", len(body) > 8000, f"{len(body):,} bytes")
    with open("/tmp/report.pdf", "wb") as fh:
        fh.write(body)

    r2 = client.post("/report", json={**PAYLOAD, "delivery": "inline"}, headers=KEY)
    check("inline delivery honoured", "inline; filename=" in r2.headers.get("content-disposition", ""))
    r3 = client.post("/report", json=PAYLOAD)
    check("report requires a key", r3.status_code == 401)

print("\n" + "=" * 60)
print(f"{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
