from __future__ import annotations

import os

from fastapi import Header, HTTPException


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Enforce API key auth when API_AUTH_KEY is configured.

    If API_AUTH_KEY is empty, auth is treated as disabled for local/dev flows.
    """
    expected = os.getenv("API_AUTH_KEY", "").strip()
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
