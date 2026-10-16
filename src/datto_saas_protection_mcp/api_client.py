# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Async client for the Datto partner REST API (SaaS Protection part)."""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ._json import error_envelope
from .config import API_BASE_URL

logger = logging.getLogger("datto_saas_protection_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own keys, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class DattoError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Datto API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class DattoCredentials:
    public_key: str
    private_key: str

    def __repr__(self) -> str:  # never leak the keys into logs or tracebacks
        return "DattoCredentials(keys=<redacted>)"


def segment(value: str) -> str:
    """One path segment, encoded (subscription ids contain colons)."""
    return quote(value, safe="")


class DattoClient:
    """Thin wrapper over the Datto partner API for one caller's key pair."""

    def __init__(self, credentials: DattoCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.base_url = API_BASE_URL

    def _headers(self, accept: str) -> dict[str, str]:
        pair = f"{self._credentials.public_key}:{self._credentials.private_key}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode(), "Accept": accept}

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._send(path, params, "application/json")
        if not resp.content or not resp.content.strip():
            return None
        try:
            return resp.json()
        except ValueError:
            return {"raw_response": resp.text[:2000]}

    async def put(self, path: str, body: Any) -> Any:
        resp = await self._send(path, None, "application/json", method="PUT", body=body)
        if not resp.content or not resp.content.strip():
            return None
        try:
            return resp.json()
        except ValueError:
            return {"raw_response": resp.text[:2000]}

    async def _send(
        self, path: str, params: dict[str, Any] | None, accept: str, method: str = "GET", body: Any = None
    ) -> httpx.Response:
        http = self._http or _get_shared_http_client()
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        headers = self._headers(accept)
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: DattoError | None = None
            try:
                resp = await http.request(method, url, params=query, headers=headers, json=body)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, DattoError(0, f"Timed out calling Datto ({method} {path})")
            except httpx.RequestError as e:
                status, error = 0, DattoError(0, f"Network error calling Datto ({method} {path}): {type(e).__name__}")

            logger.info(
                "datto_api method=%s path=%s status=%s ms=%d attempt=%d remaining=%s",
                method, path, status, (time.monotonic() - t0) * 1000, attempt,
                resp.headers.get("X-API-Limit-Remaining") if resp is not None else "-",
            )

            # 429 is refused before processing, so any method may retry it; other
            # failures are retried only for reads, so a write never runs twice.
            retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            if resp.status_code >= 400:
                raise DattoError(resp.status_code, _error_message(resp, path))
            return resp


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _error_message(resp: httpx.Response, path: str) -> str:
    fallback = f"Datto returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("code")
        if message:
            return f"{fallback}: {str(message)[:500]}"
    return fallback
