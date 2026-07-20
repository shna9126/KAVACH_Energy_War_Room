"""Shared requests session for all connectors.

On corporate networks with SSL-intercepting proxies, HTTPS certificate
verification fails. Set SSL_VERIFY=false in .env to disable it globally.
Warning suppression is intentional — this is a dev-environment bypass.
"""
from __future__ import annotations

import os

import requests
import urllib3


def _ssl_verify_enabled() -> bool:
    return os.getenv("SSL_VERIFY", "true").strip().lower() not in ("false", "0", "no")


_WARN_SUPPRESSED = False


def _maybe_disable_warnings(verify_enabled: bool) -> None:
    global _WARN_SUPPRESSED
    if not verify_enabled and not _WARN_SUPPRESSED:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _WARN_SUPPRESSED = True


def get(url: str, **kwargs) -> requests.Response:
    """Drop-in replacement for requests.get with configurable SSL verify."""
    verify_enabled = _ssl_verify_enabled()
    _maybe_disable_warnings(verify_enabled)
    kwargs.setdefault("verify", verify_enabled)
    return requests.get(url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    """Drop-in replacement for requests.post with configurable SSL verify."""
    verify_enabled = _ssl_verify_enabled()
    _maybe_disable_warnings(verify_enabled)
    kwargs.setdefault("verify", verify_enabled)
    return requests.post(url, **kwargs)
