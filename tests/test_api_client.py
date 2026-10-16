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
"""Request shapes, retries and tool behaviour against a mocked Datto API."""

import base64
import json

import httpx
import pytest

from datto_saas_protection_mcp import api_client
from datto_saas_protection_mcp.api_client import DattoClient, DattoCredentials, DattoError
from datto_saas_protection_mcp.server import _credentials_var, create_mcp_server

CREDS = DattoCredentials("pub", "sec")
DOMAINS = [
    {
        "saasCustomerId": 101,
        "saasCustomerName": "Example Dental",
        "domain": "example.com",
        "externalSubscriptionId": "Classic:Office365:555",
        "backupStats": {"activeServicesCount": 10, "activeServicesWithRecentBackupCount": 9, "backupPercentage": 90},
    },
    {"saasCustomerId": 102, "saasCustomerName": "Sample Law", "domain": "sample.org"},
]
SEATS = [
    {"remoteId": "r1", "mainId": "jane@example.com", "seatType": "User", "seatState": "Active", "billable": "1"},
    {"remoteId": "r2", "mainId": "shared@example.com", "seatType": "SharedMailbox", "seatState": "Paused", "billable": "0"},
]
APPLICATIONS = [
    {
        "pagination": {"page": 1, "perPage": 100, "totalPages": 1, "count": 1},
        "items": [
            {
                "customerId": 101,
                "customerName": "Example Dental",
                "usedBytes": 2048,
                "suites": [
                    {
                        "suiteType": "Office365",
                        "appTypes": [
                            {
                                "appType": "Exchange",
                                "backupHistory": [
                                    {"status": "Success", "timeWindow": "day", "startTime": 1780185600, "endTime": 1780272000},
                                    {"status": "Failed", "timeWindow": "day", "startTime": 1780099200, "endTime": 1780185600},
                                ],
                            },
                            {"appType": "OneDrive", "backupHistory": [{"status": "Partial", "endTime": 1780272000}]},
                        ],
                    }
                ],
            }
        ],
    }
]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


class Recorder:
    """Mock transport: replies by method + path below /v1/ (path is the raw, encoded one)."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?")[0].removeprefix("/v1/")
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.raw_path.decode().split("?")[0] == f"/v1/{path}" and (method is None or r.method == method)
        ]


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> DattoClient:
    return DattoClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- client ----------------------------------------------------------------


async def test_basic_auth_header():
    rec = Recorder({"saas/domains": DOMAINS})
    await _client(rec).get("saas/domains")
    assert rec.requests[0].headers["Authorization"] == "Basic " + base64.b64encode(b"pub:sec").decode()
    assert str(rec.requests[0].url) == "https://api.datto.com/v1/saas/domains"


async def test_get_retries_5xx_but_put_does_not():
    replies = iter([httpx.Response(502), httpx.Response(200, json=DOMAINS)])
    rec = Recorder({"saas/domains": lambda r: next(replies), "PUT saas/1/x/bulkSeatChange": lambda r: httpx.Response(500, json={})})
    client = _client(rec)
    await client.get("saas/domains")
    with pytest.raises(DattoError):
        await client.put("saas/1/x/bulkSeatChange", {})
    assert len(rec.calls("saas/domains")) == 2
    assert len(rec.calls("saas/1/x/bulkSeatChange")) == 1


async def test_bad_keys_are_unauthorized(monkeypatch):
    rec = Recorder({"saas/domains": lambda r: httpx.Response(401, json={"code": "401", "message": "Unauthorized"})})
    result = await _call(monkeypatch, rec, "datto_saas_list_domains", {})
    assert result["error"]["code"] == "unauthorized"
    assert "sec" not in result["error"]["message"].replace("seconds", "")


# ---- tools -----------------------------------------------------------------


async def test_list_domains_search(monkeypatch):
    rec = Recorder({"saas/domains": DOMAINS})
    result = await _call(monkeypatch, rec, "datto_saas_list_domains", {"search": "LAW"})
    assert [d["saasCustomerId"] for d in result["domains"]] == [102]


async def test_customer_is_required_when_several_exist(monkeypatch):
    rec = Recorder({"saas/domains": DOMAINS})
    result = await _call(monkeypatch, rec, "datto_saas_list_seats", {})
    assert result["error"]["code"] == "invalid_argument"
    assert "Example Dental" in result["error"]["message"] and "= 101" in result["error"]["message"]
    assert not rec.calls("saas/101/seats")


async def test_single_customer_is_used_automatically(monkeypatch):
    rec = Recorder({"saas/domains": DOMAINS[:1], "saas/101/seats": SEATS})
    result = await _call(monkeypatch, rec, "datto_saas_list_seats", {})
    assert result["saasCustomerId"] == "101" and result["count"] == 2


async def test_list_seats_filters(monkeypatch):
    rec = Recorder({"saas/101/seats": SEATS})
    result = await _call(monkeypatch, rec, "datto_saas_list_seats", {"saasCustomerId": 101, "seatType": "User", "seatState": "paused"})
    assert rec.requests[0].url.params["seatType"] == "User"
    assert [s["remoteId"] for s in result["seats"]] == ["r2"]
    assert result["byState"] == {"Paused": 1}


async def test_non_numeric_customer_id_is_rejected(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "datto_saas_list_seats", {"saasCustomerId": "../domains"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_get_seat_by_main_id(monkeypatch):
    rec = Recorder({"saas/101/seats": SEATS})
    result = await _call(monkeypatch, rec, "datto_saas_get_seat", {"saasCustomerId": "101", "seatId": "JANE@example.com"})
    assert result["remoteId"] == "r1"


async def test_get_missing_seat(monkeypatch):
    rec = Recorder({"saas/101/seats": SEATS})
    result = await _call(monkeypatch, rec, "datto_saas_get_seat", {"saasCustomerId": 101, "seatId": "nobody"})
    assert result["error"]["code"] == "not_found"


async def test_list_applications_flattens_report(monkeypatch):
    rec = Recorder({"saas/101/applications": APPLICATIONS})
    result = await _call(monkeypatch, rec, "datto_saas_list_applications", {"saasCustomerId": 101, "daysUntil": 7})
    assert rec.requests[0].url.params["daysUntil"] == "7"
    assert result["customers"][0]["customerName"] == "Example Dental"


async def test_backup_stats_summarises_latest_window(monkeypatch):
    rec = Recorder({"saas/domains": DOMAINS, "saas/101/applications": APPLICATIONS})
    result = await _call(monkeypatch, rec, "datto_saas_get_backup_stats", {"saasCustomerId": 101})
    apps = {a["appType"]: a for a in result["applications"]}
    assert apps["Exchange"]["latestStatus"] == "Success"
    assert apps["Exchange"]["windowsNotSuccessful"] == 1
    assert apps["Exchange"]["latestEndAt"] == "2026-06-01T00:00:00Z"
    assert result["applicationsNotSuccessful"] == ["OneDrive"]
    assert result["backupStats"][0]["backupPercentage"] == 90
    assert result["usedBytes"] == 2048


async def test_bulk_change_needs_confirm(monkeypatch):
    rec = Recorder()
    args = {"saasCustomerId": 101, "externalSubscriptionId": "Classic:Office365:555", "seatType": "User", "actionType": "Pause", "ids": ["r1"]}
    result = await _call(monkeypatch, rec, "datto_saas_bulk_seat_change", args)
    assert result["sent"] is False
    assert "Pause 1 User seat(s)" in result["summary"]
    assert rec.requests == []


async def test_bulk_change_sends_documented_body(monkeypatch):
    path = "saas/101/Classic%3AOffice365%3A555/bulkSeatChange"
    rec = Recorder({f"PUT {path}": {"action": "Pause", "status": "Queued", "customerId": 101, "id": 9}})
    args = {
        "saasCustomerId": "101",
        "externalSubscriptionId": "Classic:Office365:555",
        "seatType": "User",
        "actionType": "Pause",
        "ids": ["r1", "r1", " r3 "],
        "confirm": True,
    }
    result = await _call(monkeypatch, rec, "datto_saas_bulk_seat_change", args)
    request = rec.calls(path, "PUT")[0]
    assert json.loads(request.content) == {"seat_type": "User", "action_type": "Pause", "ids": ["r1", "r3"]}
    assert result["sent"] is True and result["result"]["status"] == "Queued"
