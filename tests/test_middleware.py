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
"""Credential middleware: 401 on missing headers, deferred region errors, per-request isolation."""

import asyncio
import base64

import pytest
from starlette.testclient import TestClient

from datto_saas_protection_mcp.__main__ import build_http_app
from datto_saas_protection_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
KEYS = {"X-Datto-SaaS-Public-Key": "public", "X-Datto-SaaS-Secret-Key": "secret"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Datto-SaaS-Public-Key": "public"},
        {"X-Datto-SaaS-Secret-Key": "secret"},
        {"X-Datto-SaaS-Public-Key": "public", "X-Datto-SaaS-Secret-Key": "${DATTO_SAAS_SECRET_KEY}"},
    ],
)
def test_missing_keys_return_401(headers):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required_headers"] == list(KEYS)


@pytest.mark.parametrize("region", ["us", "EU", "apac"])
def test_region_header(region):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | KEYS | {"X-Datto-SaaS-Region": region})
    assert resp.status_code == 200


def test_placeholder_headers_still_list_tools():
    # The admin console refreshes the tool catalog with placeholder header values.
    names = list(KEYS) + ["X-Datto-SaaS-Region"]
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | {h: "placeholder" for h in names})
    assert resp.status_code == 200
    assert "datto_saas_list_domains" in resp.text


def test_unknown_region_is_reported_by_tools():
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "datto_saas_list_domains", "arguments": {}}}
    with _client() as client:
        resp = client.post("/mcp", json=call, headers=MCP_HEADERS | KEYS | {"X-Datto-SaaS-Region": "apac"})
    assert resp.status_code == 200
    assert '\\"invalid_argument\\"' in resp.text and "must be one of: us, eu" in resp.text


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | KEYS)
    assert resp.status_code == 200
    for name in ("datto_saas_list_domains", "datto_saas_get_backup_stats", "datto_saas_bulk_seat_change"):
        assert name in resp.text


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def test_keys_become_basic_auth_and_are_reset():
    seen = _run_middleware([(b"x-datto-saas-public-key", b" pub "), (b"x-datto-saas-secret-key", b"sec")])
    headers = seen["client"]._headers("application/json")
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pub:sec").decode()
    assert seen["client"].base_url == "https://api.datto.com/v1"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().public_key
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().public_key))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(key: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": [(b"x-datto-saas-public-key", key), (b"x-datto-saas-secret-key", b"s")]}

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), receive, send), middleware(scope(b"tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_keys():
    seen = _run_middleware([(b"x-datto-saas-public-key", b"pub-value"), (b"x-datto-saas-secret-key", b"very-secret")])
    text = repr(seen["credentials"])
    assert "very-secret" not in text and "pub-value" not in text
