# Track B — File Contracts

Every starter file the scaffold creates, with its exact contract. The agent
writing these files must honor every contract line; anything marked [VERIFY]
must be checked against the real API docs before going live.

---

## 1. `mcp-servers/close-gateway/src/close_gateway/server.py`

Stdio MCP server named `close-gateway`. **Read-only by design: no send tool
may exist in this module.**

- Env: `CLOSE_API_KEY` (Close API key, used as basic-auth username),
  `BOOKING_LINK_MAP` (optional JSON `{account_id: booking_link}` fallback).
- HTTP: `httpx`, base `https://api.close.com/api/v1/`, basic auth `(api_key, "")`.
- If `CLOSE_API_KEY` is unset, every tool returns
  `{"error": "close_not_configured", "hint": "set CLOSE_API_KEY"}` — never fake data.

Tools:
- `get_message(message_id: str) -> dict`
  Fetch one inbound message/activity from Close. Return the raw activity
  fields the agent needs: id, text/body, direction, sender, timestamp, lead id.
- `get_lead(lead_id: str) -> dict`
  `GET /lead/{lead_id}/`. Return lead details + contact phones/emails needed
  for identity resolution.
- `render_template(template: str, lead_id: str, booking_link: str = "") -> dict`
  `template` is `"booking"` or `"reschedule"`. Returns
  `{"template": ..., "body": <rendered text>, "booking_link": <link or "">}`.
  Link resolution order: (1) explicit `booking_link` arg; (2) `BOOKING_LINK_MAP`
  env; (3) the API-key owner's user-profile custom field
  `scheduling_links.main_setter` [VERIFY — Close custom-field path on user].
  If unresolvable, `booking_link` is `""` — the caller must escalate, never invent.

`README.md`: tool table, env vars, "Read-only by design" banner.
`.env.example`: `CLOSE_API_KEY=`, `BOOKING_LINK_MAP=` with comments. No values.

---

## 2. `mcp-servers/ghl-gateway/src/ghl_gateway/server.py`

Stdio MCP server named `ghl-gateway`. **The only thing in the system that can
send SMS.** Read tools plus one gated send tool.

- Env: `GHL_PRIVATE_TOKEN` (per-location private integration token),
  `GHL_LOCATION_ID` (sales sub-account; known value `eafTwojmWC6t3oMzK8va`
  goes in `.env.example` as a commented example only),
  `SEND_ENABLED` (default `"false"` — any other value, including unset, refuses),
  `SEND_ALLOWLIST` (comma-separated phones/contact IDs; empty = nothing sends).
- HTTP: `httpx`, base `https://services.leadconnectorhq.com`,
  headers `Authorization: Bearer <token>`, `Version: 2021-07-28`.
- If `GHL_PRIVATE_TOKEN` is unset, every tool returns
  `{"error": "ghl_not_configured", "hint": "set GHL_PRIVATE_TOKEN"}`.

Tools:
- `get_contact(contact_id: str) -> dict`
  `GET /contacts/{contact_id}`. Return contact fields incl. phone.
- `get_conversation(contact_id: str) -> dict`
  Search conversations for the contact, return the latest thread with messages.
- `send_sms(contact_id: str, body: str) -> dict`
  Gates enforced INSIDE, in this order, before any network call:
  1. `SEND_ENABLED == "true"` else `{"sent": False, "reason": "refused: SEND_ENABLED is not 'true'"}`.
  2. `SEND_ALLOWLIST` non-empty AND (`contact_id` or the contact's phone is on it)
     else `{"sent": False, "reason": "refused: recipient not on SEND_ALLOWLIST"}`.
  Then `POST /conversations/messages` with
  `{"type": "SMS", "contactId": contact_id, "message": body}`.
  [VERIFY — whether `locationId` must also be in the body when the token spans
  locations; docs are ambiguous.] Return `{"sent": True, "message_id": ...}`
  on success, `{"sent": False, "reason": ...}` on API error.

`README.md`: "The only thing in the system that can send SMS" banner, gate
documentation, env vars.
`.env.example`: all four vars with comments. No values (location ID as comment only).

---

## 3. `agent-service/src/agent_service/app.py`

FastAPI app. Existing modules are imported, not reimplemented:
`prepass.deterministic_prepass`, `relevance` filter, `classifier.classify`,
`skill_runner.plan`.

- `GET /health` → `{"ok": True}`.
- `POST /webhook/message` accepts the canonical message:
  `{message_id, channel: "sms"|"email", source: "close"|"ghl", sender, body, timestamp}`.
  Pipeline: pre-pass (may resolve early → mode `"none"`) → relevance filter
  (not scheduling-related → mode `"none"`, logged) → `classify(message)`
  (keyword baseline default; keep an `llm_fn` injection point for the LLM path)
  → `skill_runner.plan(classification, message)`.
  Returns `{message_id, intent, confidence, reasoning, skill, mode,
  action_plan, would_send, send_tool}`.
  Never raises on a single bad message — returns a structured error dict.
- Auth: require header `X-Agent-Token` equal to `AGENT_SHARED_SECRET`.
  If the env var is unset, allow requests only when `AGENT_ALLOW_NOAUTH=true`
  and log a loud warning on every unauthenticated call.
- Decision logging: if `DATABASE_URL` is set, insert the decision record into
  `setter_decisions` (columns per `mcp-servers/setter-memory/schema.sql`);
  otherwise print the record as JSON to stdout. Logging must never break the
  response path — wrap it in try/except.

---

## 4. `n8n/wf-01-thin.json` (surgical fix, not a rewrite)

Keep the existing nodes and wiring. Three changes:
1. "Call agent service" node: URL → `={{ $env.AGENT_SERVICE_URL }}/webhook/message`,
   add header `X-Agent-Token: ={{ $env.AGENT_SHARED_SECRET }}`.
2. Rename "Call close-gateway (STUB)" → "Call ghl-gateway (STUB)"; notes must
   say sends go through `ghl-gateway.send_sms` once `SEND_ENABLED` + allowlist
   are set, and that the node intentionally does nothing until then.
3. Sticky note: document required env vars (`AGENT_SERVICE_URL`,
   `AGENT_SHARED_SECRET`) and the "Close is read-only by design" fact.
   n8n stays thin: webhook → normalize → call agent → dispatch → log.

---

## 5. `tests/test_trackb_smoke.py`

Stdlib `unittest`, runnable as `python3 tests/test_trackb_smoke.py`, offline,
no credentials, no network, no sends. Must cover:
- close-gateway exposes exactly `{get_message, get_lead, render_template}` —
  assert no attribute named `send_sms`/`send_email` exists.
- ghl-gateway `send_sms` refusal matrix: `SEND_ENABLED` unset, `"false"`,
  empty allowlist, recipient not on allowlist → all `{"sent": False}`.
- Missing creds → `*_not_configured` errors, never exceptions, never fake data.
- Agent pipeline on a fixture canonical message returns all of
  `{message_id, intent, confidence, reasoning, skill, mode}`; a rescheduling
  message yields `mode == "active"` and `send_tool == "ghl.send_sms"`.
- `wf-01-thin.json` is valid JSON; the agent-call node URL ends with
  `/webhook/message` and carries the `X-Agent-Token` header; the old
  "Call close-gateway (STUB)" node name is gone.

---

## 6. `pyproject.toml` files

- `mcp-servers/close-gateway/pyproject.toml`: deps `mcp`, `httpx`. Entry point
  `python -m close_gateway.server` runs the stdio server.
- `mcp-servers/ghl-gateway/pyproject.toml`: deps `mcp`, `httpx`. Entry point
  `python -m ghl_gateway.server`.
- `agent-service` additions: `fastapi`, `uvicorn`, `httpx`
  (`psycopg[binary]` optional — stdout JSON logging is acceptable without it).
  Run: `AGENT_ALLOW_NOAUTH=true uvicorn agent_service.app:app --port 8000`.
