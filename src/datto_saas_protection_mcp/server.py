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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ._arguments import EnvelopeFastMCP
from .api_client import DattoClient, DattoCredentials
from .config import REGIONS, Settings

HEADER_PUBLIC_KEY = "X-Datto-SaaS-Public-Key"
HEADER_PRIVATE_KEY = "X-Datto-SaaS-Secret-Key"
HEADER_REGION = "X-Datto-SaaS-Region"
REQUIRED_HEADERS = [HEADER_PUBLIC_KEY, HEADER_PRIVATE_KEY]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[DattoCredentials | None] = contextvars.ContextVar(
    "datto_saas_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("datto_saas_request_id", default="-")

INSTRUCTIONS = (
    "Datto SaaS Protection backs up MSP clients' Microsoft 365 and Google "
    "Workspace tenants (mailboxes, OneDrive / Drive, SharePoint sites, Teams). "
    "Each protected tenant is a customer with a saasCustomerId; each protected "
    "user, shared mailbox, site, team or shared drive is a seat.\n"
    "Usage:\n"
    "- Start with datto_saas_list_domains: it gives saasCustomerId, the "
    "externalSubscriptionId and a backup-health summary per customer.\n"
    "- datto_saas_get_backup_stats(saasCustomerId) and "
    "datto_saas_list_applications give backup status per application and "
    "time window; datto_saas_list_seats / datto_saas_get_seat show seats and "
    "their state (Active, Paused, Archived, Unprotected).\n"
    "- datto_saas_bulk_seat_change licenses, pauses or unlicenses seats: "
    "License starts protection and billing, Pause stops new backups, "
    "Unlicense removes protection. Call it only when the user explicitly asks, "
    "for the exact seats named, and only with confirm: true after the user "
    "approved that exact change."
)


def get_client_from_context() -> DattoClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return DattoClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


class GatewayCredentialsMiddleware:
    """Reads the Datto key pair (and optional region) on /mcp requests; 401 when a key is missing.

    An unknown region does not block the request (tools/list must work with any
    values); tools report it instead.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        public_key = _clean(request.headers.get(HEADER_PUBLIC_KEY))
        private_key = _clean(request.headers.get(HEADER_PRIVATE_KEY))
        if not (public_key and private_key):
            missing = [h for h, v in ((HEADER_PUBLIC_KEY, public_key), (HEADER_PRIVATE_KEY, private_key)) if not v]
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                },
                status_code=401,
            )(scope, receive, send)
            return

        region = (_clean(request.headers.get(HEADER_REGION)) or "us").lower()
        header_error = None
        if region not in REGIONS:
            header_error = f"{HEADER_REGION} must be one of: {', '.join(REGIONS)}"

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(DattoCredentials(public_key, private_key, header_error))
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = EnvelopeFastMCP(
        name="datto-saas-protection-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], DattoClient | None] = get_client_from_context

    from .tools import saas

    saas.register(mcp, client_factory)

    return mcp
