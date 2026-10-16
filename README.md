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

A request to `/mcp` without both keys gets HTTP 401 naming the missing one; an unknown region
gets HTTP 400.

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
