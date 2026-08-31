"""
Appraisal Adjustment API.

Endpoints
---------
GET  /health          liveness for the ECS health check -- honest about the model
GET  /model-info      version, training date, metrics, XGBoost vs RF comparison
GET  /adjustment-grid the market adjustment grid on its own (no subject needed)
POST /predict         subject property -> indicated value + adjustments (JSON)
POST /report          the same, rendered as a downloadable PDF
GET  /openapi.json    machine-readable contract for whoever integrates

Design decisions worth knowing
------------------------------
The model loads ONCE in the lifespan handler, not per request. Loading inside
a route handler means every caller pays an S3 round trip and the container
falls over under any real concurrency.

/health returns 503 when the model failed to load. This matters more than it
looks: ECS uses this endpoint to decide whether your container is alive. A
health check that returns 200 while the model is missing will have ECS
happily routing traffic to a broken service, and the failure will surface as
500s to your consumers instead of as a failed deployment you can roll back.

Every prediction is logged to S3, batched. That is the Phase 4 hook, and the
reason it goes to S3 rather than local disk is that Fargate tasks are
ephemeral -- anything on the container filesystem disappears on the next
deployment. That is not a workaround; it is how it is actually done.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .adjustments import compare_scenarios, contribution_breakdown
from .auth import require_api_key
from .features import build_serving_row, resolve_location
from .logging_sink import PredictionLogger
from .model import BUNDLE, load_bundle
from .report import build_report
from .schemas import (HealthResponse, ModelInfoResponse, PredictRequest,
                      PredictResponse, ReportRequest)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("appraisal.api")

pred_logger = PredictionLogger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_bundle()                      # once, at startup
    pred_logger.start()
    yield
    pred_logger.flush()                # do not lose the last partial batch


app = FastAPI(
    title="Appraisal Adjustment API",
    description=(
        "Market-derived adjustments and indicated value for residential property, "
        "from a gradient-boosted model trained on closed MLS sales. "
        "Authenticate with the X-API-Key header on every request."
    ),
    version=os.getenv("API_VERSION", "1.0.0"),
    lifespan=lifespan,
)

# Same-origin browsers do not need this; a separate front end does. Set
# CORS_ORIGINS to a comma-separated allowlist, never to "*" in production.
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=_origins, allow_credentials=False,
        allow_methods=["GET", "POST"], allow_headers=["X-API-Key", "Content-Type"],
    )


# --------------------------------------------------------------------------
# Unauthenticated: health only. Everything else needs a key.
# --------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health():
    if not BUNDLE.loaded:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "model_loaded": False,
                     "detail": BUNDLE.load_error or "model not loaded"},
        )
    return HealthResponse(status="ok", model_loaded=True, model_version=BUNDLE.version)


# --------------------------------------------------------------------------
# Core prediction path
# --------------------------------------------------------------------------

def _require_model():
    if not BUNDLE.loaded:
        raise HTTPException(503, detail=f"Model unavailable: {BUNDLE.load_error}")


def _support_warnings(x_row: pd.DataFrame) -> list[str]:
    """Tree models return the edge value for anything outside the training
    range rather than extrapolating. Say so explicitly instead of returning a
    confident-looking number that is actually pinned to the boundary."""
    ref = BUNDLE.reference
    warn = []
    friendly = {"gla_sqft": "Above-grade GLA", "lot_sqft": "Lot size",
                "bsmt_fin_sqft": "Finished basement", "age_at_sale": "Age",
                "bedrooms": "Bedroom count", "baths_full": "Full bath count",
                "garage_spaces": "Garage bays", "fireplaces": "Fireplaces"}
    for col in ref.columns:
        if col.startswith("loc_"):
            continue
        v = float(x_row[col].iloc[0])
        lo, hi = float(ref[col].min()), float(ref[col].max())
        if v < lo or v > hi:
            warn.append(
                f"{friendly.get(col, col)} of {v:,.0f} is outside the training range "
                f"({lo:,.0f} to {hi:,.0f}). The model cannot extrapolate; this subject is "
                "valued at the edge of the observed market."
            )
    return warn


def _build_payload(req: PredictRequest, key_fp: str) -> tuple[dict, dict]:
    _require_model()
    meta = BUNDLE.metadata
    subject = req.subject.model_dump()
    subject = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in subject.items()}

    x_row = build_serving_row(subject, meta)
    indicated = float(BUNDLE.model.predict(x_row)[0])

    supplied = {k for k, v in subject.items() if v is not None}
    derived_from = {"gla_sqft": {"main_sqft", "upper_sqft", "gla_sqft"},
                    "age_at_sale": {"year_built", "age_at_sale"},
                    "months_since_start": {"effective_date", "months_since_start"}}
    defaulted = [
        f for f in meta["feature_names"]
        if not f.startswith("loc_") and not (derived_from.get(f, {f}) & supplied)
    ]

    loc_used, loc_matched = resolve_location(subject.get("location"), meta)
    mae = meta.get("metrics", {}).get("mae_test", 0.0)

    payload = {
        "indicated_value": round(indicated, 0),
        "value_range_low": round(indicated - mae, 0),
        "value_range_high": round(indicated + mae, 0),
        "model_version": BUNDLE.version,
        "location_used": loc_used,
        "location_matched": loc_matched,
        "defaulted_fields": defaulted,
        "features_used": {c: float(x_row[c].iloc[0]) for c in x_row.columns
                          if not c.startswith("loc_")},
        "warnings": _support_warnings(x_row),
    }
    if not loc_matched and subject.get("location"):
        payload["warnings"].append(
            f"Location '{subject['location']}' is not a level in the training data. "
            f"The subject was valued in the baseline location "
            f"({meta.get('baseline_location')}). Known levels: "
            f"{', '.join(meta.get('location_levels', []))}."
        )

    if req.include_grid:
        payload["adjustment_grid"] = BUNDLE.grid.get("dollar", [])
        payload["location_adjustments"] = BUNDLE.grid.get("location", [])
    if req.include_percent_grid:
        payload["percent_grid"] = BUNDLE.grid.get("percent", [])
    if req.include_contributions:
        cb = contribution_breakdown(BUNDLE.model, x_row, BUNDLE.reference)
        payload["contributions"] = [
            {"feature": r.Feature, "subject_value": r.Subject_Value,
             "sample_median": r.Sample_Median, "contribution": round(r.Contribution, 0)}
            for r in cb.itertuples()
        ]
    if req.scenarios:
        out = {}
        for sc in req.scenarios:
            if sc.feature not in meta["feature_names"]:
                raise HTTPException(422, detail=(
                    f"Unknown feature '{sc.feature}'. Valid features: "
                    f"{[f for f in meta['feature_names'] if not f.startswith('loc_')]}"
                ))
            df = compare_scenarios(BUNDLE.model, x_row, sc.feature, sc.values)
            out[sc.feature] = [
                {"value": float(r[sc.feature]),
                 "indicated_value": round(float(r["Indicated_Value"]), 0),
                 "difference": None if pd.isna(r["Difference"]) else round(float(r["Difference"]), 0)}
                for _, r in df.iterrows()
            ]
        payload["scenarios"] = out

    return payload, subject


@app.post("/predict", response_model=PredictResponse, tags=["valuation"])
async def predict(req: PredictRequest, request: Request, key_fp: str = Depends(require_api_key)):
    t0 = time.perf_counter()
    payload, subject = _build_payload(req, key_fp)
    latency_ms = (time.perf_counter() - t0) * 1000

    pred_logger.record({
        "request_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_version": BUNDLE.version,
        "api_key_fp": key_fp,
        "features": payload["features_used"],
        "location_used": payload["location_used"],
        "prediction": payload["indicated_value"],
        "latency_ms": round(latency_ms, 2),
        "endpoint": "/predict",
    })
    return payload


@app.post("/report", tags=["valuation"],
          responses={200: {"content": {"application/pdf": {}},
                           "description": "The analysis as a PDF"}})
async def report(
    req: ReportRequest,
    persist: bool = Query(False, description="Also write the PDF to S3 and return its key in the X-Report-S3-Key header"),
    key_fp: str = Depends(require_api_key),
):
    t0 = time.perf_counter()
    payload, subject = _build_payload(req, key_fp)
    pdf = build_report(payload, subject, BUNDLE.metadata, title=req.report_title)
    latency_ms = (time.perf_counter() - t0) * 1000

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = (subject.get("file_number") or subject.get("address") or "subject")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(slug))[:48].strip("-")
    filename = f"adjustment-analysis-{slug}-{stamp}.pdf"

    headers = {
        "Content-Disposition": f'{req.delivery}; filename="{filename}"',
        "X-Model-Version": BUNDLE.version,
        "X-Indicated-Value": str(payload["indicated_value"]),
        # Lets a browser fetch() read the headers above on a cross-origin call
        "Access-Control-Expose-Headers": "Content-Disposition, X-Model-Version, X-Indicated-Value, X-Report-S3-Key",
    }

    if persist:
        key = pred_logger.put_report(pdf, filename)
        if key:
            headers["X-Report-S3-Key"] = key

    pred_logger.record({
        "request_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_version": BUNDLE.version,
        "api_key_fp": key_fp,
        "features": payload["features_used"],
        "location_used": payload["location_used"],
        "prediction": payload["indicated_value"],
        "latency_ms": round(latency_ms, 2),
        "endpoint": "/report",
    })

    import io
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf", headers=headers)


@app.get("/adjustment-grid", tags=["valuation"])
async def adjustment_grid(key_fp: str = Depends(require_api_key)):
    """The market adjustment grid on its own. No subject property required --
    this is the market evidence, independent of any one appraisal."""
    _require_model()
    return {
        "model_version": BUNDLE.version,
        "baseline_location": BUNDLE.metadata.get("baseline_location"),
        "n_training_sales": BUNDLE.metadata.get("n_training_sales"),
        "dollar": BUNDLE.grid.get("dollar", []),
        "percent": BUNDLE.grid.get("percent", []),
        "location": BUNDLE.grid.get("location", []),
    }


@app.get("/model-info", response_model=ModelInfoResponse, tags=["ops"])
async def model_info(key_fp: str = Depends(require_api_key)):
    _require_model()
    m = BUNDLE.metadata
    return ModelInfoResponse(
        model_version=BUNDLE.version,
        trained_at=m.get("trained_at"),
        n_training_sales=m.get("n_training_sales"),
        feature_names=m.get("feature_names", []),
        location_levels=m.get("location_levels", []),
        baseline_location=m.get("baseline_location"),
        metrics=m.get("metrics", {}),
        model_comparison=m.get("model_comparison", {}),
        data_diagnostics=m.get("data_diagnostics", {}),
    )


# Deliberately NO catch-all ValueError -> 422 handler.
#
# An earlier version had one, and it turned a genuine server-side bug in the
# PDF builder into a 422 "your request was invalid" -- blaming the caller for
# a fault in this service, and hiding the bug from the logs. Pydantic already
# returns 422 for real validation failures. Anything else should surface as a
# 500 and show up in CloudWatch where it belongs.
