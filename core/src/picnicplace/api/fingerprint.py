"""A fingerprint of the API, so a Studio built for a different core can tell.

The Studio is generated from the OpenAPI document (``docs/api/openapi.json``) and built
with the fingerprint of that file. The core computes the same fingerprint from its own
OpenAPI document and reports it in ``/api/v1/health``. If they differ, the Studio and
the core come from different versions, and the Studio says so: for example after
updating the code without restarting a running core.
"""

import hashlib
import json
from typing import Any

from fastapi import FastAPI


def openapi_text(schema: dict[str, Any]) -> str:
    """The OpenAPI document exactly as it is written to docs/api/openapi.json."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def api_fingerprint(app: FastAPI) -> str:
    cached: str | None = getattr(app.state, "api_fingerprint", None)
    if cached is None:
        cached = fingerprint(openapi_text(app.openapi()))
        app.state.api_fingerprint = cached
    return cached
