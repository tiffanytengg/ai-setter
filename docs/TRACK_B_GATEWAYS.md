# Track B — Gateways + Thin n8n Orchestration

**Build night: Monday, Sept 28, 2026. Demo: Sept 30.**
Track A (golden sets / evals) is **parked** — it needs real inbound SMS samples
before it can mean anything. Track B makes the system *live-ready*: real API
gateways, real servers, and the n8n thin layer wired to receive production data.

## 1. The architecture in one picture

```
  Close ──webhook──┐
                    ▼
  GHL ──webhook──► n8n WF-01 (thin: webhook → normalize → agent → dispatch → log)
                    │
                    │  POST /webhook/message {message_id, channel, source, sender, body, timestamp}
                    │  header: X-Agent-Token (shared secret)
                    ▼
              agent-service (FastAPI)
                1. deterministic pre-pass   (opt-outs, duplicates — no model)
                2. relevance filter          (scheduling-related?)
                3. classifier               (intent + confidence + reasoning)
                4. skill planner            (active / shadow / none)
                    │                        │
                    │  MCP tools             │  log_decision
                    ▼                        ▼
        ┌───────────────────┐     setter-memory → Postgres (messages + decisions)
        │ close-gateway      │    (read-only BY DESIGN —
        │  get_message       │     no send tool exists)
        │  get_lead          │     ┌───────────────────┐
        │  render_template   │     │ ghl-gateway        │
        └───────────────────┘     │  get_contact       │
                                  │  get_conversation  │
                                  │  send_sms ◄──gated │
                                  └───────────────────┘
```

**Thin n8n rule:** n8n routes, it never reasons. All decisions live in the
agent service; all sends live in the gateways. That is the trust boundary.

## 2. The three gateways

| Gateway | Trust domain | Tools | Send? |
|---|---|---|---|
| `close-gateway` | Close API | `get_message`, `get_lead`, `render_template` | **No.** Read-only by design; no send tool exists to misconfigure |
| `ghl-gateway` | GHL API v2 | `get_contact`, `get_conversation`, `send_sms` | **Gated** — see §3 |
| `setter-memory` | Postgres | `log_message`, `log_decision`, `get_state` | N/A — storage |

`ghl-gateway` is the *only* thing in the system that can send SMS.
`close-gateway` is deliberately send-less: even a misconfigured prompt cannot
send through it, because the capability does not exist there. (This matches
the client's requirements: the Close credential is read-only.)

## 3. The send safety model (why nothing can send tonight)

`ghl-gateway.send_sms` enforces **two gates inside the function itself** —
not in the agent, not in n8n, so no prompt or workflow can bypass them:

1. **Master switch:** `SEND_ENABLED` must be `"true"` (default `"false"`).
2. **Allowlist:** `SEND_ALLOWLIST` (comma-separated phones/contact IDs) must be
   non-empty AND the recipient must be on it. Empty allowlist = nothing sends.

This is the GHL guide's rule ("your workflow reads; it never writes — if you
ever need a write, ask Adam first; experiments only to designated test
contacts") enforced in code rather than by convention. Until Adam authorizes
a test contact, both gates stay at their defaults: **the send path physically
cannot fire.** When he authorizes your number, you add one phone number to the
allowlist — a one-line env change, no code change. Nothing can ever reach a
production lead through this path unless someone deliberately allowlists one.

## 4. What "ready to receive live production data" means tonight

- [x] Both gateway MCP servers implemented: `close-gateway` read-only against
      the real Close API, `ghl-gateway` read + **gated** send against the real
      GHL API v2 (credentials from env only; missing creds → honest
      "not configured")
- [x] `agent-service` FastAPI app: `POST /webhook/message` runs the full
      pipeline and returns `{intent, confidence, reasoning, skill, mode}`
- [x] n8n WF-01 fixed: posts to `/webhook/message` with `X-Agent-Token`
      (was `/classify` with no auth — that endpoint never existed); the send
      stub node is now "Call ghl-gateway (STUB)"
- [x] Normalize node maps Close + GHL webhook shapes → canonical message
      (field mapping marked `[VERIFY]` — confirm against first real execution)
- [x] Every decision lands in `messages` + `decisions` (Postgres) or stdout JSON
      if no `DATABASE_URL` is set
- [x] Smoke tests pass offline: no creds → clean errors; sends refuse

## 5. Environment variables

```bash
# close-gateway (READ-ONLY — no send vars exist here)
CLOSE_API_KEY=            # Close API key (basic-auth username)
BOOKING_LINK_MAP=         # optional JSON {account_id: booking_link} fallback

# ghl-gateway (the only sender — both gates default to "send nothing")
GHL_PRIVATE_TOKEN=        # per-location private integration token
GHL_LOCATION_ID=          # sales sub-account: eafTwojmWC6t3oMzK8va (from GHL URL)
SEND_ENABLED=false        # master send switch — keep false until Adam
SEND_ALLOWLIST=           # comma-separated phones/contact IDs allowed to receive

# agent-service
AGENT_SHARED_SECRET=      # shared with n8n as X-Agent-Token
AGENT_ALLOW_NOAUTH=true   # local dev only
DATABASE_URL=             # Postgres; unset = log decisions as JSON to stdout
ANTHROPIC_API_KEY=        # for the LLM classifier path (Track A follow-up)
```

Copy each service's `.env.example` to `.env` locally. **Never commit `.env`.**

## 6. Run it locally (tonight's smoke test)

```bash
cd ~/workspace/ai-setter

# 0. one-time environment setup — use a venv (it lives in ~/workspace, so it
#    survives VM replacements; system-wide pip installs do not). Pin mcp<2:
#    v2 renamed FastMCP and the gateways target the v1 API.
python3 -m venv .venv && .venv/bin/pip install httpx "mcp<2" fastapi "uvicorn[standard]"
# run everything below with .venv/bin/python instead of bare python3

# 1. agent service
cd agent-service && pip install -e . && \
  AGENT_ALLOW_NOAUTH=true uvicorn agent_service.app:app --port 8000 &

# 2. fake an inbound Close SMS through the whole chain
curl -X POST localhost:8000/webhook/message \
  -H 'Content-Type: application/json' \
  -d '{"message_id":"test-1","channel":"sms","source":"close",
       "sender":"+15551234567","body":"Can we move Tuesday to Thursday?",
       "timestamp":"2026-09-28T19:00:00Z"}'
# expect: intent=rescheduling, mode=active, would_send=true (dry-run: nothing sent)

# 3. gateways (stdio MCP — test with the smoke script, not curl)
python3 tests/test_trackb_smoke.py   # 26 tests: gates refuse, no creds = clean errors

# 4. ghl-gateway send (only after Adam authorizes your number)
cd mcp-servers/ghl-gateway && pip install -e . && \
  GHL_PRIVATE_TOKEN=... GHL_LOCATION_ID=eafTwojmWC6t3oMzK8va \
  SEND_ENABLED=true SEND_ALLOWLIST=+1YOURNUMBER python -m ghl_gateway.server
```

## 7. Still pending Adam (message sent 2026-09-28 ~19:40 EDT — awaiting reply)

1. ~~GHL location ID for the sales sub-account~~ — **found it yourself:**
   `eafTwojmWC6t3oMzK8va` (from the GHL URL)
2. Authorization to send a test SMS to your own phone
3. Is `scheduling_links.main_setter` API-readable, or does the demo use the
   static `BOOKING_LINK_MAP` fallback?
4. Which credential has message-write permission on the GHL side
   (the PIT needs `conversations/message.write` scope for the sales location)

## 8. After Adam answers (updated 2026-09-28 — wiring is done)

1. Set `SEND_ENABLED=true` + add **your number only** to `SEND_ALLOWLIST`
   on the ghl-gateway (one number, one-line env change — never a real lead
   until the demo is proven).
2. ~~Point the agent service at the gateways as MCP tools~~ **DONE
   2026-09-28:** `agent-service` now executes active plans through the MCP
   gateways (`gateway_client.py` + `execute.py`): render via
   close-gateway, send via ghl-gateway.send_sms. Fail-closed at every step
   (unconfigured gateway / empty render / gateway refusal → nothing sends).
   Verified end-to-end over SSE: the send was attempted and the gateway
   refused it (`SEND_ENABLED` unset). Set `CLOSE_GATEWAY_URL` /
   `GHL_GATEWAY_URL` to point at the deployed gateways.
3. Import the fixed `wf-01-thin.json` into n8n.committedcoaches.com; set
   `AGENT_SERVICE_URL` / `AGENT_SHARED_SECRET` env vars; wire the Postgres
   credential on the Log node.
4. Capture one real GHL webhook execution + one real Close webhook execution;
   replace the `[VERIFY]` field mappings and the `n8n/fixtures/*.json`.
5. Send one authorized test SMS to your phone via `ghl-gateway.send_sms`.
   Click the booking link.
6. Run the full loop repeatedly before the Sept 30 demo.

**Current Track A send labels:** `agent-service/src/agent_service/skill_runner.py`
source-matches send tools: Close plans use the shadow-only `close.send_sms`
label, while GHL plans use the gated `ghl.send_sms` path.

## 9. Deliberately NOT tonight

- No real sends (blocked on Adam — this is correct, not a delay).
- No Railway deploy yet (Dockerfiles + env checklists ready; deploy is a
  dashboard task — see `docs/RAILWAY_DEPLOY.md`).
- No golden-set work (parked until real inbound SMS samples exist).
- Email channel (Phase 2 — same pipeline, `send_email` already gated and ready).
