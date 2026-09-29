# n8n — WF-01 (thin orchestration)

n8n is the thin orchestration layer: **webhook intake → normalize → call agent
service → dispatch → log**. There is deliberately **no reasoning in n8n** — every
classification decision happens in the agent service.

`wf-01-thin.json` is the importable workflow definition.

## Data flow

```
Webhook (POST /webhook/tt-inbound-crm)
  │  GHL workflow webhooks ("Customer Replied") + Close webhooks both POST here
  ▼
Normalize (Code, runOnceForAllItems)
  │  detects GHL-workflow vs Close shape → canonical message
  │  output 0 = canonical messages   output 1 (error) = unknown shapes
  ├─(0)─▶ Pre-pass note (NoOp, pass-through)
  │         │  documents: opt-out + duplicate handling lives in the agent service
  │         ▼
  │       Call agent service (HTTP POST {$env.AGENT_SERVICE_URL}/webhook/message)
  │         │  returns {intent, confidence, reasoning, skill, mode}
  │         ▼
  │       Dispatch (Switch on mode)
  │         ├─ active ─▶ Call ghl-gateway (STUB)   (no-op; agent service already logged the decision)
  │         ├─ shadow ─▶ (end; agent service already logged the decision)
  │         ├─ none   ─▶ End (no action)
  │         └─ other  ─▶ (end; agent service already logged the decision)
  │
  └─(1)─▶ Unknown payload shape (NoOp error sink) ─▶ Log
```

## Nodes

| # | Node | Type | What it does |
|---|------|------|--------------|
| 1 | Webhook | webhook (POST, path `tt-inbound-crm`) | The retained intake node. One entry point for both sources. |
| 2 | Normalize | Code | Detects GHL-workflow shape vs Close shape, maps to the canonical message. Two outputs: `0` = canonical messages, `1` = unknown shapes (never throws). Every mapped field is marked `[VERIFY]` in the code — shapes are not yet confirmed. |
| 3 | Unknown payload shape | NoOp | Error sink for Normalize output 1. Feeds Log so the raw payload is preserved for later mapping. Wire alerting here later. |
| 4 | Pre-pass note | NoOp | Pass-through. Documents that opt-out (STOP/HELP, compliance) and duplicate/idempotency handling live in the agent service's deterministic pre-pass — **not here**. |
| 5 | Call agent service | HTTP Request | POSTs the canonical message to `{{ $env.AGENT_SERVICE_URL }}/webhook/message` with the `X-Agent-Token` header. Expects `{"intent","confidence","reasoning","skill","mode"}` back. 60s timeout. |
| 6 | Dispatch | Switch | Routes on the returned `mode`: `active` → send stub, `shadow` → log only, `none` → end. Unexpected modes hit the `extra` fallback → Log. |
| 7 | Call ghl-gateway (STUB) | NoOp | **STUB — NOT IMPLEMENTED.** When send permission is granted, this becomes the call to `ghl-gateway.send_sms` (allowlist enforced inside the gateway). Intentionally does nothing today. |
| 8 | End (no action) | NoOp | `mode=none`: the agent decided no action. |
| 9 | Log | Postgres (insert) | Anomaly sink: writes unknown-shape payloads to the `messages` table with a synthetic `message_id`. Happy-path decisions are logged by the agent service, not here. Credential is named ("Setter Postgres (wire on import)"), not embedded. |

Plus one Sticky Note summarising the flow and what's stubbed.

## The canonical message

Everything downstream consumes exactly this shape (produced by Normalize,
consumed by the agent service's `/webhook/message`):

```json
{
  "message_id": "msg_9f8e7d6c5b4a",
  "channel": "sms",
  "source": "ghl",
  "sender": "+15551234567",
  "body": "Hi! Can I move my session to Thursday?",
  "timestamp": "2026-09-28T14:32:00.000Z"
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `message_id` | string | Stable id of the inbound message (dedupe key). |
| `channel` | `"sms"` \| `"email"` | Phase 1 = sms; email is the Phase 2 path through the identical pipeline. |
| `source` | `"close"` \| `"ghl"` | Which system delivered the webhook event. |
| `sender` | string | Phone number (sms) or email address of the contact. |
| `body` | string | Message text. |
| `timestamp` | string | ISO-8601 of when the message arrived. |

The agent service answers with:

```json
{
  "intent": "reschedule_request",
  "confidence": 0.92,
  "reasoning": "Contact asks to move an existing session.",
  "skill": "send-reschedule-link",
  "mode": "active"
}
```

`mode` is the only field n8n branches on: `active` (send path — currently a
stub), `shadow` (log + escalate, never send), `none` (no action).

## Accepted payload shapes

**1. GHL workflow webhook** — the "Customer Replied" workflow in the sales
sub-account POSTs every inbound message to `/webhook/tt-inbound-crm`.
Illustrative example: `fixtures/ghl_customer_replied.json`. Expected to carry a
`contact` object plus `message`/`trigger` fields; detected by the presence of
`contact`, `contact_id`, or `message`.

**2. Close webhook** — Close event webhooks. Illustrative example:
`fixtures/close_sms.json`. Expected to wrap the payload as
`{event, object_type, data, ...}`; detected by `event`+`data` or `object_type`.

> ⚠️ **Both shapes are UNVERIFIED.** The fixture files carry a top-level
> `"_note"` saying exactly that. Build the real mapping from captured
> executions: import the workflow, capture one real GHL execution and one real
> Close execution, fix the `[VERIFY]` fields in Normalize, and replace the
> fixtures with the real payloads.

## Env vars

| Var | Used by | Purpose |
|-----|---------|---------|
| `AGENT_SERVICE_URL` | Call agent service | Base URL of the agent service; the node POSTs to `{$env.AGENT_SERVICE_URL}/webhook/message`. No hardcoded host. Set in the n8n environment (n8n reads `$env` in expressions). |

No secrets live in this workflow. The Postgres connection is an n8n credential
(named "Setter Postgres (wire on import)"); the `messages` table and
credential must be wired on import.

## What's stubbed / pending

1. **Real payload verification** — every `[VERIFY]` field in Normalize, plus
   replacing `fixtures/*.json` with real captured executions.
2. **Send nodes** — `Call ghl-gateway (STUB)` does nothing until the
   ghl-gateway send permission + test contact are in place. **Nothing sends.**
3. **Credential wiring** — Postgres credential and `messages` table on import.
4. **Alerting** on the Unknown-shape error sink — wire later so unrecognised
   payloads get human attention.
5. **Outbound/own-team filtering** — when Close webhooks start delivering
   outbound events, add a drop rule in Normalize (or the agent-service
   pre-pass) so the system never replies to itself.
