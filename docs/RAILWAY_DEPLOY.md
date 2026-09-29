# Railway deploy — AI Setter (Track B)

Three services, three credential sets. Postgres already lives on Railway.

## 1. Create the services

In your Railway project, add three services from this repo, one Dockerfile each:

| Service | Dockerfile path |
|---|---|
| `close-gateway` | `mcp-servers/close-gateway/Dockerfile` |
| `ghl-gateway` | `mcp-servers/ghl-gateway/Dockerfile` |
| `agent-service` | `agent-service/Dockerfile` |

Each Dockerfile installs its own package and serves on Railway's injected
`PORT` (gateways default to SSE transport via `MCP_TRANSPORT=sse`).

## 2. Environment variables — per service (names only, never commit values)

### close-gateway (READ-ONLY by client requirement — no send tool exists)
- `CLOSE_API_KEY` — Close API key (read scope is enough)
- `BOOKING_LINK_MAP` — JSON map, e.g. `{"default": "https://<real-link>"}`
  (ask Adam whether `scheduling_links.main_setter` is API-readable; until
  then the static map is the fallback — it fails closed with `""`, never
  an invented link)

### ghl-gateway (the ONLY sender)
- `GHL_PRIVATE_TOKEN` — private integration token for the sales sub-account
- `GHL_LOCATION_ID` — `eafTwojmWC6t3oMzK8va` (sales sub-account)
- `SEND_ENABLED` — leave `false` until Adam explicitly authorizes the test
- `SEND_ALLOWLIST` — leave **empty** until Adam designates the test contact;
  then exactly one entry (your phone)

### agent-service
- `AGENT_SHARED_SECRET` — token n8n sends as `X-Agent-Token`
- `DATABASE_URL` — Railway Postgres connection string (reference the
  existing Postgres service's variable)
- `CLOSE_GATEWAY_URL` — e.g. `http://close-gateway.railway.internal:8000/sse`
- `GHL_GATEWAY_URL` — e.g. `http://ghl-gateway.railway.internal:8000/sse`

Copy the exact private domain from each gateway service's **Settings →
Networking** tab. The `/sse` suffix is required (that's the MCP endpoint).

## 3. Verify the deploy (before touching n8n)

1. `GET <agent-service-url>/health` → `{"ok": true}`
2. `POST <agent-service-url>/webhook/message` with `X-Agent-Token` and a
   reschedule fixture → expect `mode: "active"`, `execution.sent: false`,
   reason = the gateway's refusal (`SEND_ENABLED` is not `true`).
   **This refusal is the correct pre-authorization behavior — it proves the
   gates hold end to end.**
3. Only after Adam authorizes: set `SEND_ENABLED=true` + your number in
   `SEND_ALLOWLIST` on **ghl-gateway only**, then send one test SMS.

## 4. Point n8n at the agent service

In the imported `wf-01-thin.json`: the "Call agent service" HTTP node gets
`AGENT_SERVICE_URL` = the Railway agent-service public URL, and
`X-Agent-Token` = `AGENT_SHARED_SECRET`.

## 5. Normalize — capture REAL webhook payloads (no credentials needed)

The `Normalize` node's field mapping is still built on synthetic fixtures
(fields marked `[VERIFY]`). To ground it:

1. Open the existing workflow at
   `https://n8n.committedcoaches.com/workflow/zWnGZZWUq5LPXpFA` → **Executions**.
2. Open a recent successful execution → click the **Webhook** node → copy
   its output JSON.
3. Redact PII: replace real names/phones/emails with placeholders
   (`+15551234567`, `lead@example.com`), keep the **structure** intact.
4. Save as `n8n/fixtures/ghl-webhook-real.json` (same for any
   Close-triggered execution → `close-webhook-real.json`).
5. Hand the redacted files over — the Normalize node mapping gets updated
   against the real shape, and `contact_id` / sender-phone extraction gets
   verified (the executor uses `contact_id` for `send_sms`).

Do NOT trigger test webhooks into the production workflow — copy past
executions only.
