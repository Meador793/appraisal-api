"""
Artifact loading.

Everything the service needs is produced by the Phase 1 notebook and lives in
S3 under one versioned prefix. Nothing is trained here. The container is a
read-only consumer of artifacts.

    s3://$MODEL_BUCKET/$MODEL_PREFIX/
        model.json            XGBoost champion, price scale
        model_log.json        XGBoost on log(price)  -> percentage adjustments
        metadata.json         feature order, medians, metrics, model comparison
        adjustment_grid.json  precomputed grid + bootstrap intervals
        reference.parquet     training feature snapshot (also the Phase 4 drift baseline)

Why the grid is precomputed: the bootstrap in adjustments.py is a few hundred
full-sample predictions per feature. On a 0.25 vCPU Fargate task that is
seconds of CPU per request. It does not change between deployments, so it is
computed once at training time and shipped as an artifact. Per-subject work
(valuation, contributions, scenarios) is a handful of predictions and stays
live.

Loading happens ONCE, in the lifespan handler -- never inside a route.
"""

from __future__ import annotations

import io
import json
import logging
import os
from dataclasses import dataclass, field

import pandas as pd
import xgboost as xgb

log = logging.getLogger("appraisal.model")


@dataclass
class ModelBundle:
    model: xgb.XGBRegressor = None
    log_model: xgb.XGBRegressor | None = None
    metadata: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)
    reference: pd.DataFrame | None = None
    loaded: bool = False
    load_error: str | None = None

    @property
    def version(self) -> str:
        return self.metadata.get("model_version", "unknown")

    @property
    def feature_names(self) -> list:
        return self.metadata.get("feature_names", [])


BUNDLE = ModelBundle()


def _local_or_s3(path_or_key: str, bucket: str | None, prefix: str) -> bytes:
    """Read an artifact from S3, or from the local disk when MODEL_BUCKET is
    unset. The local path exists so you can run and debug the container on
    your laptop without AWS credentials -- Phase 2 before Phase 3."""
    if not bucket:
        local = os.path.join(os.getenv("LOCAL_MODEL_DIR", "/app/artifacts"), path_or_key)
        with open(local, "rb") as fh:
            return fh.read()

    import boto3  # imported lazily so local runs need no AWS SDK config

    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-2"))
    key = f"{prefix.rstrip('/')}/{path_or_key}"
    buf = io.BytesIO()
    s3.download_fileobj(bucket, key, buf)
    return buf.getvalue()


def load_bundle() -> ModelBundle:
    """Populate the module-level BUNDLE. Called once from the lifespan handler."""
    bucket = os.getenv("MODEL_BUCKET") or None
    prefix = os.getenv("MODEL_PREFIX", "models/v1")

    try:
        # --- XGBoost champion. save_model/load_model, NEVER pickle. Pickle
        # breaks across XGBoost and Python versions and you will hit it.
        booster_bytes = _local_or_s3("model.json", bucket, prefix)
        model = xgb.XGBRegressor()
        model.load_model(bytearray(booster_bytes))

        log_model = None
        try:
            lb = _local_or_s3("model_log.json", bucket, prefix)
            log_model = xgb.XGBRegressor()
            log_model.load_model(bytearray(lb))
        except Exception as exc:  # percentage adjustments are optional
            log.warning("log-price model not loaded, percentage grid disabled: %s", exc)

        metadata = json.loads(_local_or_s3("metadata.json", bucket, prefix))
        grid = json.loads(_local_or_s3("adjustment_grid.json", bucket, prefix))
        reference = pd.read_parquet(io.BytesIO(_local_or_s3("reference.parquet", bucket, prefix)))

        # Fail loudly on skew rather than serving quiet garbage.
        if list(reference.columns) != metadata["feature_names"]:
            raise ValueError(
                "reference.parquet column order does not match metadata feature_names. "
                "The artifacts were not produced by the same notebook run."
            )

        BUNDLE.model = model
        BUNDLE.log_model = log_model
        BUNDLE.metadata = metadata
        BUNDLE.grid = grid
        BUNDLE.reference = reference
        BUNDLE.loaded = True
        BUNDLE.load_error = None
        log.info("Loaded model %s with %d features from %s",
                 metadata.get("model_version"), len(metadata["feature_names"]),
                 f"s3://{bucket}/{prefix}" if bucket else "local disk")

    except Exception as exc:
        BUNDLE.loaded = False
        BUNDLE.load_error = f"{type(exc).__name__}: {exc}"
        log.exception("Model load FAILED")

    return BUNDLE
