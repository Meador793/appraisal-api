"""
Prediction logging to S3.

This is the Phase 4 hook, built in Phase 2 because retrofitting it later means
you have no history to detect drift against.

Why S3 and not a local file: Fargate tasks are ephemeral. Anything written to
the container filesystem is gone on the next deployment, restart, or scale-in
event. Since you will scale this service to zero every time you stop working
on it, a local log would be erased constantly.

Why buffered: one S3 PUT per prediction is slow (adds tens of milliseconds to
every response) and expensive at any volume. Records accumulate in memory and
flush when the buffer fills or the interval elapses, whichever comes first.

The tradeoff to be able to explain: a hard task kill loses whatever is in the
buffer. For a drift-detection log that is acceptable, because drift is a
distributional question and a few dropped records do not change it. It would
not be acceptable for billing or audit records, which is exactly the sort of
distinction a senior engineer will ask you about.

Partitioned by date so the Phase 4 drift job can read one window without
scanning everything:

    s3://$PREDICTION_BUCKET/predictions/date=2026-08-30/<uuid>.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger("appraisal.logsink")


class PredictionLogger:
    def __init__(self):
        self.bucket = os.getenv("PREDICTION_BUCKET") or None
        self.prefix = os.getenv("PREDICTION_PREFIX", "predictions")
        self.report_prefix = os.getenv("REPORT_PREFIX", "reports")
        self.max_batch = int(os.getenv("LOG_BATCH_SIZE", "25"))
        self.max_seconds = int(os.getenv("LOG_FLUSH_SECONDS", "60"))
        self._buf: list[dict] = []
        self._lock = threading.Lock()
        self._last = time.time()
        self._s3 = None

    # ---------------------------------------------------------------- client
    @property
    def s3(self):
        if self._s3 is None and self.bucket:
            import boto3
            self._s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-2"))
        return self._s3

    def start(self):
        if not self.bucket:
            log.warning("PREDICTION_BUCKET not set -- prediction logging disabled. "
                        "Phase 4 drift detection will have nothing to read.")
        else:
            log.info("Prediction logging to s3://%s/%s", self.bucket, self.prefix)

    # ---------------------------------------------------------------- record
    def record(self, entry: dict):
        if not self.bucket:
            return
        with self._lock:
            self._buf.append(entry)
            due = (len(self._buf) >= self.max_batch
                   or (time.time() - self._last) > self.max_seconds)
        if due:
            self.flush()

    def flush(self):
        if not self.bucket:
            return
        with self._lock:
            batch, self._buf = self._buf, []
            self._last = time.time()
        if not batch:
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{self.prefix}/date={day}/{uuid.uuid4()}.jsonl"
        body = "\n".join(json.dumps(r, default=str) for r in batch).encode()
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                               ContentType="application/x-ndjson")
            log.info("Flushed %d prediction records to %s", len(batch), key)
        except Exception:
            # Never fail a prediction because logging failed. The caller has a
            # valid answer; losing a drift record is the lesser problem.
            log.exception("Prediction log flush failed; %d records dropped", len(batch))

    # ---------------------------------------------------------------- reports
    def put_report(self, pdf_bytes: bytes, filename: str) -> str | None:
        """Persist a generated PDF. Returns the S3 key, or None if no bucket."""
        if not self.bucket:
            return None
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{self.report_prefix}/date={day}/{filename}"
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=pdf_bytes,
                               ContentType="application/pdf")
            return key
        except Exception:
            log.exception("Report persist failed")
            return None
