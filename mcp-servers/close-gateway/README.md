# close-gateway

MCP server (stdio, FastMCP). Trust domain: the Close API. **Read-only by design.**

## Tools

- `get_message(message_id)` — fetch one inbound SMS activity from Close (`GET /activity/sms/{id}/`).
- `get_lead(lead_id)` — lead details + recent SMS history (`GET /lead/{id}/`, `GET /activity/sms/?lead_id=`).
- `render_template(template, lead_id, booking_link="")` — renders the `booking` / `reschedule` template text. If `booking_link` is not passed, it is resolved from `BOOKING_LINK_MAP` first, then the API-key owner's user profile (`GET /me/` → user scheduling links). Returns `""` when the link cannot be resolved — the skill must escalate, never invent one. This only renders text; the actual send happens in `ghl-gateway.send_sms`.

There is deliberately **no send tool** in this server — Close is the read-only system of record (client requirement). The live send path is `ghl-gateway.send_sms`.

Reads return `{"error": "not_configured", ...}` when `CLOSE_API_KEY` is missing — never fake data.

## Env vars (see `.env.example`)

| Var | Purpose |
|---|---|
| `CLOSE_API_KEY` | Close API key (Basic auth username; password empty). |
| `BOOKING_LINK_MAP` | Optional JSON map for booking-link resolution (e.g. `{"default": "https://..."}`). |

## Run

```bash
cd mcp-servers/close-gateway
pip install -e .
CLOSE_API_KEY=... python -m close_gateway.server
```
