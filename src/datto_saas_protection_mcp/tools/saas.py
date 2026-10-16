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
"""SaaS Protection customers (domains), seats, backup status and seat licensing."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import DattoClient, DattoError, segment
from ..config import MAX_BULK_SEAT_IDS
from ._common import READ_ONLY, WRITES_DESTRUCTIVE, ClientFactory, customer_path, iso, run_tool

SeatType = Literal["User", "SharedMailbox", "Site", "TeamSite", "SharedDrive", "Team"]
ActionType = Literal["License", "Pause", "Unlicense"]
OptionalCustomerId = Annotated[
    int | str | None,
    Field(description="saasCustomerId from datto_saas_list_domains; may be omitted when the account has one customer."),
]
_ACTION_EFFECT = {
    "License": "licensed seats are backed up and billed",
    "Pause": "paused seats keep existing backups but take no new backups",
    "Unlicense": "unlicensed seats are no longer protected (no new backups)",
}


async def list_domains(client: DattoClient) -> list[dict]:
    payload = await client.get("saas/domains")
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    return [d for d in payload or [] if isinstance(d, dict)] if isinstance(payload, list) else []


async def resolve_customer(client: DattoClient, provided: int | str | None) -> str:
    """The saasCustomerId to use; without one, the account's only customer or a list to choose from."""
    if provided is not None and str(provided).strip():
        return customer_path(provided)
    ids: dict[str, str] = {}
    for d in await list_domains(client):
        cid = str(d.get("saasCustomerId", "")).strip()
        if cid and cid not in ids:
            ids[cid] = f"{d.get('saasCustomerName') or d.get('organizationName') or cid} ({d.get('domain') or 'no domain'})"
    if len(ids) == 1:
        return next(iter(ids))
    if not ids:
        raise DattoError(404, "This API key sees no SaaS Protection customers.")
    options = "; ".join(f"{label} = {cid}" for cid, label in list(ids.items())[:25])
    raise ValueError(f"saasCustomerId is required ({len(ids)} customers). Ask the user which one: {options}")


async def list_seats(client: DattoClient, customer: str, seat_types: list[str] | None = None) -> list[dict]:
    params = {"seatType": ",".join(seat_types)} if seat_types else None
    payload = await client.get(f"saas/{customer}/seats", params)
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    return [s for s in payload or [] if isinstance(s, dict)] if isinstance(payload, list) else []


def report_items(payload: Any) -> list[dict]:
    """The applications report arrives as [{pagination, items: [...]}] or {pagination, items}."""
    blocks = payload if isinstance(payload, list) else [payload]
    items: list[dict] = []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("items"), list):
            items.extend(i for i in block["items"] if isinstance(i, dict))
        elif isinstance(block, dict) and "suites" in block:
            items.append(block)
    return items


def latest_by_app(items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        for suite in item.get("suites") or []:
            for app in (suite or {}).get("appTypes") or []:
                history = [h for h in (app or {}).get("backupHistory") or [] if isinstance(h, dict)]
                history.sort(key=lambda h: h.get("endTime") or 0, reverse=True)
                last = history[0] if history else {}
                rows.append(
                    {
                        "suiteType": suite.get("suiteType"),
                        "appType": app.get("appType"),
                        "latestStatus": last.get("status"),
                        "latestWindow": last.get("timeWindow"),
                        "latestEndAt": iso(last.get("endTime")),
                        "activeServiceCount": last.get("activeServiceCount"),
                        "activeServiceWithBackupCount": last.get("activeServiceWithBackupCount"),
                        "activeServiceWithPerfectBackupCount": last.get("activeServiceWithPerfectBackupCount"),
                        "windowsNotSuccessful": sum(1 for h in history if str(h.get("status", "")).lower() not in ("success", "")),
                        "windows": len(history),
                    }
                )
    return rows


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def datto_saas_list_domains(
        search: Annotated[str | None, Field(description="Text in the customer name, domain, organization or subscription id.")] = None,
    ) -> str:
        """List SaaS Protection customers (M365 tenants / Google domains): ids, seats and backup health.

        Start here: saasCustomerId and externalSubscriptionId feed every other tool.
        """

        async def action(client: DattoClient) -> dict:
            domains = await list_domains(client)
            needle = (search or "").strip().lower()
            if needle:
                fields = ("saasCustomerName", "domain", "organizationName", "externalSubscriptionId")
                domains = [d for d in domains if any(needle in str(d.get(f) or "").lower() for f in fields)]
            return {"domains": domains, "count": len(domains)}

        return await run_tool("datto_saas_list_domains", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_saas_list_seats(
        saasCustomerId: OptionalCustomerId = None,
        seatType: Annotated[SeatType | None, Field(description="Only this seat type.")] = None,
        seatState: Annotated[str | None, Field(description="Only this state: Active, Paused, Archived or Unprotected.")] = None,
    ) -> str:
        """List a customer's seats (users, shared mailboxes, sites, teams, drives) with state and billing."""

        async def action(client: DattoClient) -> dict:
            customer = await resolve_customer(client, saasCustomerId)
            seats = await list_seats(client, customer, [seatType] if seatType else None)
            if seatState and seatState.strip():
                wanted = seatState.strip().lower()
                seats = [s for s in seats if str(s.get("seatState", "")).lower() == wanted]
            states: dict[str, int] = {}
            for seat in seats:
                key = str(seat.get("seatState") or "Unknown")
                states[key] = states.get(key, 0) + 1
            return {"saasCustomerId": customer, "seats": seats, "count": len(seats), "byState": states}

        return await run_tool("datto_saas_list_seats", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_saas_get_seat(
        seatId: Annotated[str, Field(description="Seat remoteId or mainId (email / site URL) from datto_saas_list_seats.", min_length=1)],
        saasCustomerId: OptionalCustomerId = None,
    ) -> str:
        """Get one seat by remoteId or mainId (email / site URL): type, state, billable flag, date added."""

        async def action(client: DattoClient) -> dict:
            customer = await resolve_customer(client, saasCustomerId)
            needle = seatId.strip().lower()
            for seat in await list_seats(client, customer):
                if needle in (str(seat.get("remoteId", "")).lower(), str(seat.get("mainId", "")).lower()):
                    return {"saasCustomerId": customer, **seat}
            raise DattoError(404, f'No seat with remoteId / mainId "{seatId}" for customer {customer}')

        return await run_tool("datto_saas_get_seat", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_saas_list_applications(
        saasCustomerId: OptionalCustomerId = None,
        daysUntil: Annotated[int | None, Field(description="Days of backup history to report (Datto keeps about 30).", ge=0, le=90)] = None,
    ) -> str:
        """Backup history per suite and application (Exchange, OneDrive, SharePoint, Teams, Gmail, Drive)."""

        async def action(client: DattoClient) -> dict:
            customer = await resolve_customer(client, saasCustomerId)
            payload = await client.get(f"saas/{customer}/applications", {"daysUntil": daysUntil})
            return {"saasCustomerId": customer, "customers": report_items(payload)}

        return await run_tool("datto_saas_list_applications", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_saas_get_backup_stats(saasCustomerId: OptionalCustomerId = None) -> str:
        """Backup health for a customer: overall backup percentage and each application's latest window status."""

        async def action(client: DattoClient) -> dict:
            customer = await resolve_customer(client, saasCustomerId)
            domains = [d for d in await list_domains(client) if str(d.get("saasCustomerId")) == customer]
            items = report_items(await client.get(f"saas/{customer}/applications"))
            apps = latest_by_app(items)
            return {
                "saasCustomerId": customer,
                "customerName": domains[0].get("saasCustomerName") if domains else None,
                "backupStats": [d.get("backupStats") for d in domains] or None,
                "usedBytes": sum(i.get("usedBytes") or 0 for i in items) if items else None,
                "applications": apps,
                "applicationsNotSuccessful": [a["appType"] for a in apps if str(a["latestStatus"] or "").lower() not in ("success", "")],
            }

        return await run_tool("datto_saas_get_backup_stats", client_factory, action)

    @mcp.tool(annotations=WRITES_DESTRUCTIVE)
    async def datto_saas_bulk_seat_change(
        saasCustomerId: Annotated[int | str, Field(description="saasCustomerId from datto_saas_list_domains.")],
        externalSubscriptionId: Annotated[
            str, Field(description="externalSubscriptionId from datto_saas_list_domains, e.g. Classic:Office365:123456.", min_length=1)
        ],
        seatType: Annotated[SeatType, Field(description="Seat type of all the ids.")],
        actionType: Annotated[ActionType, Field(description="License starts protection and billing; Pause stops new backups; Unlicense removes protection.")],
        ids: Annotated[list[str], Field(description="remoteId of each seat (datto_saas_list_seats).", min_length=1, max_length=MAX_BULK_SEAT_IDS)],
        confirm: Annotated[bool | None, Field(description="Must be true, set only after the user approved this exact change.")] = None,
    ) -> str:
        """License, pause or unlicense up to 100 seats of one type. Changes protection and billing.

        Only when the user explicitly asks; describe the change and get approval, then call with confirm: true.
        """

        async def action(client: DattoClient) -> dict:
            customer = customer_path(saasCustomerId)
            seat_ids = list(dict.fromkeys(i.strip() for i in ids if i and i.strip()))
            if not seat_ids:
                raise ValueError("ids must contain at least one non-empty remoteId.")
            summary = (
                f"{actionType} {len(seat_ids)} {seatType} seat(s) for customer {customer} "
                f"(subscription {externalSubscriptionId.strip()}): {_ACTION_EFFECT[actionType]}."
            )
            if confirm is not True:
                return {
                    "sent": False,
                    "summary": summary,
                    "message": "Not sent. Show this change to the user and call again with confirm: true only after they approve it.",
                }
            result = await client.put(
                f"saas/{customer}/{segment(externalSubscriptionId.strip())}/bulkSeatChange",
                {"seat_type": seatType, "action_type": actionType, "ids": seat_ids},
            )
            return {"sent": True, "summary": summary, "result": result}

        return await run_tool("datto_saas_bulk_seat_change", client_factory, action)


