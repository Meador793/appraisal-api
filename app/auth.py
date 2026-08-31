"""
API key authentication.

Keys are supplied as the environment variable API_KEYS, comma-separated, and
compared with `secrets.compare_digest` so a timing attack cannot recover a key
character by character.

Where the keys actually live in each phase:

  local dev   ->  a .env file, or `-e API_KEYS=...` on `docker run`
  ECS         ->  AWS Secrets Manager, referenced from the task definition as
                  a `secrets` entry (NOT an `environment` entry -- environment
                  values are visible in plain text to anyone who can call
                  DescribeTaskDefinition)

Rotation without downtime: API_KEYS holds a list. Add the new key, redeploy,
move consumers over, then remove the old key and redeploy again.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets

from fastapi import Header, HTTPException, status

log = logging.getLogger("appraisal.auth")

_HEADER_NAME = "X-API-Key"


def _configured_keys() -> list[str]:
    raw = os.getenv("API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def key_fingerprint(key: str) -> str:
    """First 8 hex of the SHA-256. Safe to log and to show a consumer so they
    can confirm which key they are using without the key ever being written
    to CloudWatch."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


async def require_api_key(x_api_key: str | None = Header(None, alias=_HEADER_NAME)) -> str:
    keys = _configured_keys()

    if not keys:
        # Refuse to run open to the internet by accident. An unauthenticated
        # deploy is a much worse failure than a broken one.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No API keys configured on the server. Set the API_KEYS environment variable.",
        )

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {_HEADER_NAME} header.",
            headers={"WWW-Authenticate": _HEADER_NAME},
        )

    for valid in keys:
        if secrets.compare_digest(x_api_key, valid):
            return key_fingerprint(x_api_key)

    log.warning("Rejected request with unknown key fingerprint %s", key_fingerprint(x_api_key))
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key.")
