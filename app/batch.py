"""
S3-triggered batch processing.

    Upload a file  ->  s3://JOBS_BUCKET/incoming/analysis/carmel-2026.csv
    Get results at ->  s3://OUTPUT_BUCKET/outputs/analysis/2026-08-30-1412-carmel-2026/

No API key, no public IP, no always-on container. You drop a file in a bucket
and results appear in another one.

TWO JOB TYPES, routed by the folder you drop the file into
----------------------------------------------------------
  incoming/analysis/     A new market export. Trains XGBoost and Random Forest,
                         compares them, produces the adjustment grid with
                         confidence intervals, and writes deployable model
                         artifacts. This is the notebook, run headless.

  incoming/valuations/   A list of subject properties. Scores each one against
                         an ALREADY-TRAINED model (MODEL_PREFIX) and returns a
                         valuation per row. Does not train anything.

The prefix is the router because it is the one piece of information you cannot
forget to supply -- you have to put the file somewhere.

FAILURES ARE OUTPUTS TOO
------------------------
A job that fails writes an error file to failed/ and returns success to the
trigger. That is deliberate. If the handler raised, Lambda would retry the same
broken CSV twice more on its own schedule, and the only record of what went
wrong would be three identical stack traces in CloudWatch. A written error file
that you find next to your input is a better artifact than a retry storm.
"""

from __future__ import annotations

import io
import json
import os
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_plus

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- config
JOBS_BUCKET = os.getenv("JOBS_BUCKET")
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET") or JOBS_BUCKET
OUTPUT_PREFIX = os.getenv("OUTPUT_PREFIX", "outputs")
FAILED_PREFIX = os.getenv("FAILED_PREFIX", "failed")
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")

# Column mapping. Overridable per deployment without rebuilding the image,
# and overridable per FILE by uploading a sidecar config (see _load_config).
DEFAULT_COLS = {
    "main_sqft": "Main Level SqFt",
    "upper_sqft": "Upper SqFt",
    "bsmt_fin_sqft": "Apprx Below Grade Fin SqFt",
    "bedrooms": "Bedrooms",
    "baths_full": "Baths Full",
    "baths_half": "Baths Half",
    "garage_spaces": "Garage Spaces",
    "fireplaces": "Fireplaces",
    "lot_sqft": "Lot Size SqFt",
    "year_built": "Year Built",
    "close_date": "Close Date",
    "location": "Area",
    "concessions": "Concessions",
}

BASE_CONFIG = {
    "sheet_name": 0,
    "status_col": "Status",
    "status_keep": "Sold",
    "target": "Close Price",
    "net_concessions": True,
    "min_location_count": 15,
    "min_level_count": 10,
    "random_state": 42,
    "n_bootstrap": int(os.getenv("N_BOOTSTRAP", "300")),
    "train_through": None,
    "test_fraction": 0.20,
    "monotone": {
        "gla_sqft": 1, "bsmt_fin_sqft": 1, "bedrooms": 0, "baths_full": 1,
        "baths_half": 1, "garage_spaces": 1, "fireplaces": 1, "lot_sqft": 1,
        "age_at_sale": -1, "months_since_start": 1,
    },
    "cols": DEFAULT_COLS,
}


def _s3():
    import boto3
    return boto3.client("s3", region_name=AWS_REGION)


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-")[:60] or "job"


class RunLog:
    """Collects progress messages so the job's own log lands in S3 next to its
    results. CloudWatch has the same lines, but a log you find beside the
    output is the one you actually read."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str = ""):
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        print(line, flush=True)
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ==========================================================================
# Input handling
# ==========================================================================

def _read_table(body: bytes, key: str, sheet=0) -> pd.DataFrame:
    """CSV or Excel, decided by extension. CSVs get a couple of encoding
    fallbacks because MLS exports opened and re-saved in Excel on Windows are
    frequently cp1252, not UTF-8, and the failure is an unhelpful
    UnicodeDecodeError on row one."""
    suffix = Path(key).suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return pd.read_excel(io.BytesIO(body), sheet_name=sheet)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(body), encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode {key} as UTF-8, cp1252, or latin-1.")


def _load_config(s3, bucket: str, key: str, log: RunLog) -> dict:
    """
    Per-file config override.

    Upload `carmel-2026.csv` and optionally `carmel-2026.config.json` beside it.
    Anything in that JSON is merged over the defaults, so a market with renamed
    columns needs no redeploy:

        {"cols": {"bsmt_fin_sqft": "Below Grade Finished SF",
                  "fireplaces": null},
         "min_location_count": 10}
    """
    cfg = json.loads(json.dumps(BASE_CONFIG))  # deep copy
    sidecar = str(Path(key).with_suffix("")) + ".config.json"
    try:
        body = s3.get_object(Bucket=bucket, Key=sidecar)["Body"].read()
        override = json.loads(body)
        if "cols" in override:
            cfg["cols"] = {**cfg["cols"], **override.pop("cols")}
        cfg.update(override)
        log(f"Applied config override from {sidecar}")
    except Exception:
        log("No sidecar config found; using defaults.")
    return cfg


# ==========================================================================
# Job type 1 -- analyse a new market export
# ==========================================================================

def run_analysis_job(df_raw: pd.DataFrame, cfg: dict, job_name: str,
                     log: RunLog) -> dict[str, bytes]:
    """Returns {filename: bytes} for everything the job produced."""
    from .excel_export import build_workbook
    from .pipeline import run_dataset
    from .report import build_market_report

    cfg = {**cfg, "dataset_name": job_name, "model_version": job_name,
           "excel_path": job_name}

    log(f"Rows received: {len(df_raw):,}")
    missing = [c for c in [cfg["status_col"], cfg["target"]] if c not in df_raw.columns]
    if missing:
        raise ValueError(
            f"Required column(s) not found: {missing}. "
            f"Columns present: {list(df_raw.columns)[:25]}"
        )

    log("Training XGBoost and Random Forest...")
    result = run_dataset(cfg, df_raw=df_raw, write_artifacts=False, verbose=True)
    meta = result["metadata"]
    cmp_ = meta["model_comparison"]

    log(f"XGBoost  R2 {cmp_['xgboost']['r2']:.4f}  MAE ${cmp_['xgboost']['mae']:,.0f}")
    log(f"Forest   R2 {cmp_['random_forest']['r2']:.4f}  MAE ${cmp_['random_forest']['mae']:,.0f}")
    log(cmp_["verdict"])
    for f in meta["data_diagnostics"]["flags"]:
        log(f"RANGE RESTRICTION: {f}")
    if meta["missing_fields"]:
        log(f"Fields absent from this export: {', '.join(meta['missing_fields'])}")

    files: dict[str, bytes] = {}

    log("Building PDF...")
    files["market_report.pdf"] = build_market_report(
        meta, result["grid"], result["percent"], result["location"], result["importance"])

    log("Building Excel workbook...")
    files["adjustment_analysis.xlsx"] = build_workbook(
        meta, result["grid"], result["percent"], result["location"],
        result["importance"], result["X"], result["y"])

    log("Serialising model artifacts...")
    # Written so the analysis can be promoted to a deployed model later --
    # copy this folder to the model bucket and point MODEL_PREFIX at it.
    tmp = Path("/tmp") / f"artifacts-{_slug(job_name)}"
    tmp.mkdir(parents=True, exist_ok=True)
    result["xgb_model"].save_model(tmp / "model.json")
    files["artifacts/model.json"] = (tmp / "model.json").read_bytes()
    files["artifacts/metadata.json"] = json.dumps(meta, indent=2).encode()

    buf = io.BytesIO()
    result["X"].to_parquet(buf, index=False)
    files["artifacts/reference.parquet"] = buf.getvalue()

    files["artifacts/adjustment_grid.json"] = json.dumps({
        "dollar": result["grid"].to_dict(orient="records"),
        "percent": result["percent"].to_dict(orient="records"),
        "location": result["location"].to_dict(orient="records"),
    }, indent=2, default=str).encode()

    files["summary.json"] = json.dumps({
        "job": job_name,
        "sales_analyzed": meta["n_training_sales"],
        "mean_price": meta["mean_price"],
        "model_comparison": cmp_,
        "range_restricted": bool(meta["data_diagnostics"]["flags"]),
        "missing_fields": meta["missing_fields"],
        "supported_adjustments": int(
            result["grid"]["Use_In_Grid"].str.startswith("YES").sum()),
    }, indent=2, default=str).encode()

    return files


# ==========================================================================
# Job type 2 -- value a list of subject properties
# ==========================================================================

def run_valuation_job(df_raw: pd.DataFrame, job_name: str, log: RunLog) -> dict[str, bytes]:
    """Scores each row against the already-deployed model. Trains nothing."""
    from .features import build_serving_row, resolve_location
    from .model import BUNDLE, load_bundle
    from .report import build_report

    if not BUNDLE.loaded:
        load_bundle()
    if not BUNDLE.loaded:
        raise RuntimeError(
            f"No model available: {BUNDLE.load_error}. "
            "Valuation jobs need MODEL_BUCKET and MODEL_PREFIX pointing at a "
            "trained model. Run an analysis job first and promote its artifacts."
        )

    meta = BUNDLE.metadata
    log(f"Model {BUNDLE.version} loaded, {len(df_raw):,} subjects to value")

    mae = meta.get("metrics", {}).get("mae_test", 0.0)
    numeric = ["main_sqft", "upper_sqft", "gla_sqft", "bsmt_fin_sqft", "bedrooms",
               "baths_full", "baths_half", "garage_spaces", "fireplaces",
               "lot_sqft", "year_built", "age_at_sale"]

    rows, pdfs = [], {}
    for i, raw in enumerate(df_raw.to_dict(orient="records")):
        subject = {k: v for k, v in raw.items() if pd.notna(v)}
        for k in numeric:
            if k in subject:
                subject[k] = pd.to_numeric(subject[k], errors="coerce")
                if pd.isna(subject[k]):
                    subject.pop(k)
        if "effective_date" in subject:
            subject["effective_date"] = str(subject["effective_date"])[:10]

        out = {"row": i + 1,
               "address": subject.get("address"),
               "file_number": subject.get("file_number")}
        try:
            if not any(subject.get(k) is not None
                       for k in ("gla_sqft", "main_sqft", "upper_sqft")):
                raise ValueError("no size supplied (need gla_sqft, or main_sqft/upper_sqft)")

            x_row = build_serving_row(subject, meta)
            value = float(BUNDLE.model.predict(x_row)[0])
            loc_used, loc_matched = resolve_location(subject.get("location"), meta)

            warnings = []
            ref = BUNDLE.reference
            for col in ref.columns:
                if col.startswith("loc_"):
                    continue
                v = float(x_row[col].iloc[0])
                lo, hi = float(ref[col].min()), float(ref[col].max())
                if v < lo or v > hi:
                    warnings.append(f"{col} {v:,.0f} outside training range "
                                    f"({lo:,.0f}-{hi:,.0f})")
            if subject.get("location") and not loc_matched:
                warnings.append(f"location '{subject['location']}' unknown, "
                                f"valued as {loc_used}")

            out.update({
                "indicated_value": round(value),
                "range_low": round(value - mae),
                "range_high": round(value + mae),
                "location_used": loc_used,
                "model_version": BUNDLE.version,
                "warnings": "; ".join(warnings),
                "status": "ok",
            })

            # Per-subject PDFs only for small batches. A 500-row file would
            # otherwise write 500 PDFs and probably time out.
            if len(df_raw) <= 20:
                payload = {
                    "indicated_value": round(value),
                    "value_range_low": round(value - mae),
                    "value_range_high": round(value + mae),
                    "model_version": BUNDLE.version,
                    "location_used": loc_used, "location_matched": loc_matched,
                    "defaulted_fields": [], "warnings": warnings,
                    "features_used": {c: float(x_row[c].iloc[0]) for c in x_row.columns
                                      if not c.startswith("loc_")},
                    "adjustment_grid": BUNDLE.grid.get("dollar", []),
                    "percent_grid": BUNDLE.grid.get("percent", []),
                    "location_adjustments": BUNDLE.grid.get("location", []),
                }
                name = _slug(str(subject.get("file_number")
                                 or subject.get("address") or f"row-{i+1}"))
                pdfs[f"reports/{name}.pdf"] = build_report(payload, subject, meta)

        except Exception as exc:
            # One bad row must not lose the other 499.
            out.update({"status": "error", "warnings": f"{type(exc).__name__}: {exc}"})
            log(f"  row {i + 1}: {exc}")

        rows.append(out)

    results = pd.DataFrame(rows)
    ok = int((results["status"] == "ok").sum())
    log(f"Valued {ok} of {len(results)} rows")

    files = {"valuations.csv": results.to_csv(index=False).encode()}
    files.update(pdfs)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        results.to_excel(w, sheet_name="Valuations", index=False)
    files["valuations.xlsx"] = buf.getvalue()

    files["summary.json"] = json.dumps({
        "job": job_name, "model_version": BUNDLE.version,
        "rows": len(results), "valued": ok, "errors": len(results) - ok,
        "reports_generated": len(pdfs),
    }, indent=2).encode()
    return files


# ==========================================================================
# Orchestration
# ==========================================================================

def process_s3_object(bucket: str, key: str) -> dict:
    """Entry point for both Lambda and the CLI/ECS path."""
    log = RunLog()
    s3 = _s3()
    key = unquote_plus(key)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    job_name = _slug(Path(key).stem)

    # Route on the folder. Anything else is ignored rather than guessed at.
    lower = key.lower()
    if "/analysis/" in lower:
        job_type = "analysis"
    elif "/valuations/" in lower or "/valuation/" in lower:
        job_type = "valuations"
    else:
        log(f"Ignoring {key}: not under incoming/analysis/ or incoming/valuations/.")
        return {"status": "ignored", "key": key}

    if lower.endswith(".config.json"):
        return {"status": "ignored", "key": key, "reason": "sidecar config"}

    out_prefix = f"{OUTPUT_PREFIX}/{job_type}/{stamp}-{job_name}"
    log(f"Job {job_type} | source s3://{bucket}/{key}")
    log(f"Output s3://{OUTPUT_BUCKET}/{out_prefix}/")

    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        log(f"Downloaded {len(body):,} bytes")

        cfg = _load_config(s3, bucket, key, log)
        df_raw = _read_table(body, key, cfg.get("sheet_name", 0))
        log(f"Parsed {len(df_raw):,} rows x {len(df_raw.columns)} columns")

        if job_type == "analysis":
            files = run_analysis_job(df_raw, cfg, job_name, log)
        else:
            files = run_valuation_job(df_raw, job_name, log)

    except Exception as exc:
        detail = (f"Job failed for s3://{bucket}/{key}\n\n"
                  f"{type(exc).__name__}: {exc}\n\n"
                  f"{traceback.format_exc()}\n\n"
                  f"--- run log ---\n{log.text()}")
        fail_key = f"{FAILED_PREFIX}/{stamp}-{job_name}/error.txt"
        try:
            s3.put_object(Bucket=OUTPUT_BUCKET, Key=fail_key,
                          Body=detail.encode(), ContentType="text/plain")
        except Exception:
            print("Could not even write the error file", flush=True)
        print(detail, flush=True)
        # Returning rather than raising: see the module docstring.
        return {"status": "failed", "key": key,
                "error": f"{type(exc).__name__}: {exc}",
                "error_report": f"s3://{OUTPUT_BUCKET}/{fail_key}"}

    log("Uploading results...")
    written = []
    for name, data in files.items():
        ct = ("application/pdf" if name.endswith(".pdf")
              else "application/json" if name.endswith(".json")
              else "text/csv" if name.endswith(".csv")
              else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              if name.endswith(".xlsx") else "application/octet-stream")
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=f"{out_prefix}/{name}",
                      Body=data, ContentType=ct)
        written.append(name)
    log(f"Wrote {len(written)} files")

    s3.put_object(Bucket=OUTPUT_BUCKET, Key=f"{out_prefix}/run_log.txt",
                  Body=log.text().encode(), ContentType="text/plain")

    return {"status": "ok", "job_type": job_type,
            "output": f"s3://{OUTPUT_BUCKET}/{out_prefix}/", "files": written}


def lambda_handler(event, context):
    """
    S3 PutObject trigger. One event can carry several records.

    Also accepts a hand-written test event:
        {"bucket": "my-jobs", "key": "incoming/analysis/test.csv"}
    """
    if "bucket" in event and "key" in event:
        return process_s3_object(event["bucket"], event["key"])

    results = []
    for record in event.get("Records", []):
        results.append(process_s3_object(
            record["s3"]["bucket"]["name"],
            record["s3"]["object"]["key"],
        ))
    return {"results": results}


if __name__ == "__main__":
    # CLI / ECS path:  python -m app.batch <bucket> <key>
    import sys
    if len(sys.argv) == 3:
        print(json.dumps(process_s3_object(sys.argv[1], sys.argv[2]), indent=2))
    else:
        print(json.dumps(process_s3_object(
            os.environ["INPUT_BUCKET"], os.environ["INPUT_KEY"]), indent=2))
