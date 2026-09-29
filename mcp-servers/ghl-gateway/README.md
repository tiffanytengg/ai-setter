# ghl-gateway

MCP server (stdio, FastMCP). Trust domain: the GoHighLevel API v2. **The only thing in the system that can send SMS.**

## Tools

- `get_contact(contact_id)` — contact details (`GET /contacts/{contactId}`). Read-only.
- `get_conversation(contact_id)` — conversation history: finds the contact's conversation via `GET /conversations/search?locationId=&contactId=` then reads `GET /conversations/{conversationId}/messages`. Read-only.
- `send_sms(contact_id, body)` — sends via `POST /conversations/messages` with `{"type": "SMS", "contactId", "message"}`. The two send gates run **inside** this tool as part of the send, not as a step before it:
  1. `SEND_ENABLED` must be exactly `true` (any other value, including unset, refuses).
  2. `SEND_ALLOWLIST` must be non-empty AND contain the contact id (or the contact's phone). Empty allowlist = nothing can send.

GHL tokens are per-location scoped (platform constraint); the agency-level application is a production-estimate item, not this week's work.

Reads return `{"error": "not_configured", ...}` when credentials are missing — never fake data.

## Env vars (see `.env.example`)

| Var | Purpose |
|---|---|
| `GHL_PRIVATE_TOKEN` | Private integration token, scoped to the sales sub-account. |
| `GHL_LOCATION_ID` | Location (sub-account) id the token is scoped to. Sales sub-account: `eafTwojmWC6t3oMzK8va`. |
| `SEND_ENABLED` | Must be exactly `true` or nothing sends. Default: disabled. |
| `SEND_ALLOWLIST` | Comma-separated contact ids / phone numbers allowed to receive sends. Empty = nothing can send. |

## Run

```bash
cd mcp-servers/ghl-gateway
pip install -e .
GHL_PRIVATE_TOKEN=... GHL_LOCATION_ID=... python -m ghl_gateway.server
```

## Nothing sends until Adam says so

Sends stay disabled (`SEND_ENABLED=false`, empty `SEND_ALLOWLIST`) until Adam confirms: the exact location, a credential with write permission, and the sending identity. Test sends go only to a designated test contact on the allowlist.
