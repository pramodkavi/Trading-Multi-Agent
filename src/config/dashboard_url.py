"""Lazy, failure-tolerant read of the dashboard's base URL (Step 4.7 Part B, design §7).

``DashboardStack`` publishes its CloudFront URL as a plain SSM ``String`` parameter with a static
name; the scan Lambda is given that *name* in ``DASHBOARD_URL_PARAM_NAME`` (no cross-stack
reference in either direction). The value is fetched at most once per container and **never
raises**: a missing parameter, a denied read, a throttled call or an unusable value all mean
"no link", so the dashboard can never make a scan fail or wait.

The log line carries the exception *type* only -- never the parameter value.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DASHBOARD_URL_PARAM_NAME_ENV = "DASHBOARD_URL_PARAM_NAME"
_MAX_URL_LENGTH = 200

# `None` = not resolved yet; a 1-tuple holds the resolved value (itself possibly `None`),
# so a failure is cached as firmly as a success.
_cache: tuple[str | None] | None = None


def _ssm_client() -> Any:
    """Build a boto3 SSM client lazily (no AWS import until needed)."""
    import boto3

    return boto3.client("ssm")


def _valid_base_url(value: object) -> str | None:
    """The trimmed value if it is an ``https`` URL with a host and no whitespace, else ``None``."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > _MAX_URL_LENGTH or any(ch.isspace() for ch in url):
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        return None
    return url


def reset_dashboard_url_cache() -> None:
    """Forget the cached value (tests)."""
    global _cache
    _cache = None


def get_dashboard_base_url(
    *, env: Mapping[str, str] | None = None, client: Any = None
) -> str | None:
    """The dashboard's base URL, or ``None`` when there is none to link to. Never raises."""
    global _cache
    environ: Mapping[str, str] = os.environ if env is None else env
    name = environ.get(DASHBOARD_URL_PARAM_NAME_ENV)
    if not name:
        return None
    if _cache is not None:
        return _cache[0]

    value: str | None = None
    try:
        ssm = client or _ssm_client()
        raw = ssm.get_parameter(Name=name).get("Parameter", {}).get("Value")
        value = _valid_base_url(raw)
        if value is None:
            logger.warning("dashboard URL parameter has an unusable value; sending no links")
    except Exception as exc:  # the contract is "never fail a scan over a link"
        logger.warning(
            "could not read the dashboard URL parameter (%s); sending no links", type(exc).__name__
        )
    _cache = (value,)
    return value
