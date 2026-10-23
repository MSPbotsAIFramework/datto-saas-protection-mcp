# Datto SaaS Protection MCP Server

An [MCP](https://modelcontextprotocol.io) server for the SaaS Protection part of the
[Datto](https://www.datto.com) partner REST API, which backs up Microsoft 365 and Google
Workspace tenants. It lets an AI agent read protected customers and their backup health,
seats (users, shared mailboxes, sites, teams, shared drives) and per-application backup
history and, on explicit request, license, pause or unlicense seats.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: the key pair arrives on every request as HTTP headers and is
  never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Datto-SaaS-Public-Key` | Yes | string | none | - | Datto partner portal REST API public key | `your-public-key` |
| `X-Datto-SaaS-Secret-Key` | Yes | string | none | - | The secret key paired with it | `your-secret-key` |
| `X-Datto-SaaS-Region` | No | string | `us` | `us`, `eu` | Accepted for compatibility; Datto serves the SaaS Protection API from one host for every region | `us` |

Where to get them: in the Datto partner portal open **Admin -> Integrations**, choose the REST
API option and create an API key; the portal shows the public key and the secret key. The same
key pair serves Datto BCDR and SaaS Protection.

How the keys are sent to Datto: every call goes to `https://api.datto.com/v1/saas/...` with
HTTP Basic authentication (public key as user name, secret key as password).

A request to `/mcp` without both keys gets HTTP 401 naming the missing one. An unknown region
does not block `tools/list`; tool calls then return an `invalid_argument` error naming the
allowed regions.

## Tools

All tools return a JSON string. When an account has a single SaaS Protection customer,
`saasCustomerId` may be omitted on the read tools; with several customers the tool returns the
list of names and ids to choose from.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `datto_saas_list_domains` | Customers (M365 tenants / Google domains): saasCustomerId, externalSubscriptionId, seats used, backup health | search |
| `datto_saas_list_seats` | A customer's seats with type, state (Active / Paused / Archived / Unprotected), billable flag, counts by state | saasCustomerId, seatType (`User`, `SharedMailbox`, `Site`, `TeamSite`, `SharedDrive`, `Team`), seatState |
| `datto_saas_get_seat` | One seat by remoteId or mainId (email / site URL) | **seatId**, saasCustomerId |
| `datto_saas_list_applications` | Backup history per suite and application (Exchange, OneDrive, SharePoint, Teams, Gmail, Drive) | saasCustomerId, daysUntil |
| `datto_saas_get_backup_stats` | Overall backup percentage and each application's latest window status, plus apps not fully successful | saasCustomerId |
| `datto_saas_bulk_seat_change` | License, pause or unlicense up to 100 seats of one type (**write**) | **saasCustomerId**, **externalSubscriptionId**, **seatType**, **actionType** (`License`, `Pause`, `Unlicense`), **ids**, confirm |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Datto returned HTTP 401 for saas/domains", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Reads are retried with backoff on 429, 5xx and network
errors; the seat change is retried only on 429. Datto's documented limit is 10,000 requests
per hour per organization.

## Run locally

```bash
uv sync
uv run python -m datto_saas_protection_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t datto-saas-protection-mcp:dev .
docker run --rm -p 8080:8080 datto-saas-protection-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). The keys are never read from
the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Datto-SaaS-Public-Key: your-public-key" \
  -H "X-Datto-SaaS-Secret-Key: your-secret-key" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# protected customers and their backup health
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Datto-SaaS-Public-Key: your-public-key" \
  -H "X-Datto-SaaS-Secret-Key: your-secret-key" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"datto_saas_list_domains","arguments":{}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (6).** Kept as is because the tool names are an existing contract.
- **Seat changes need `confirm: true`.** `datto_saas_bulk_seat_change` changes protection and
  billing. Without `confirm: true` it only returns a summary of the change for the user to
  approve; it never asks the user mid-call, which a stateless gateway cannot relay.
- **Built on the documented endpoints.** Older versions read `.../detailedBackupStats`, which is
  not in Datto's API contract. `datto_saas_get_backup_stats` now combines the customer's
  `backupStats` (from `saas/domains`) with the latest window of each application (from
  `saas/{id}/applications`). The undocumented `includeRemoteID` option was dropped.
- **No interactive prompts.** Older versions asked the user to pick a customer when
  `saasCustomerId` was omitted; now the only customer is used, or the choices are returned.
- **Region.** The EU host the old version could select (`api.eu.datto.com`) does not serve the
  API, so every region uses `api.datto.com`.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
