# CLAUDE.md — AI Setter Rebuild

## What this is
Rebuilding Committed Coaches' AI setter automation. An agent triages inbound
messages and sends booking/rescheduling texts. n8n stays as a thin orchestration
layer; the reasoning lives here.

**The one thing that matters:** the agent must send a real booking or reschedule
message to a real phone, live, in the demo (Sept 30). Everything serves that.

## Architecture (target)
- **n8n (thin):** webhook intake (`tt-inbound-crm`) → normalize → call agent
  service → dispatch → log. No reasoning in n8n.
- **agent-service:** deterministic pre-pass → cheap relevance filter (Haiku) →
  structured classifier → skill execution. See `agent-service/`.
- **MCP servers** (one per trust domain — credentials never share a process):
  - `mcp-servers/close-gateway/` — Close API. **The only thing that can send.**
    `send_sms`/`send_email` enforce the allowlist *inside* the send.
  - `mcp-servers/ghl-gateway/` — GoHighLevel API. Read-only.
  - `mcp-servers/setter-memory/` — Postgres access layer. No external network.
- **Skills** (`agent-service/src/agent_service/skills/`): versioned behavior files.
  Active: `send-booking-link`, `send-reschedule-link`. Shadow (log + escalate,
  never send): refunds, cancellations, tech issues. Deterministic (never reach
  the agent): opt-outs, duplicates.

## Build order (dependency order)
1. **Phase 1 = texts.** Phase 2 = email through the identical pipeline
   (channel is a parameter, not a new system). Don't build Phase 2 until
   Phase 1 sends.
2. Eval harness BEFORE the classifier — every prompt change must be measurable.
3. Golden set: mine real SMS threads from Close Conversations via
   template-phrase search (primary); the 168 emails are the phase-2 set.
4. The send path (`close-gateway.send_sms`) needs a test contact + API key with
   send permission — those are pending. Build everything else first; stub the
   send behind the allowlist interface.

## Conventions
- **No secrets in code. Ever.** API keys, tokens, connection strings → env vars
  only. `.env` is gitignored. If you need a credential, ask — don't invent one.
- Skeleton methods currently raise `NotImplementedError` — that's the build list.
- Safety principle: "doing nothing is the default." Four layers: deterministic
  pre-pass → relevance filter → classifier confidence → allowlist inside the
  gateway. The agent can suggest; only the gateway can send, and only to
  allowlisted recipients.
- Booking links resolve per sender from the sender's profile
  (`user.scheduling_links.main_setter`); static account→link map is the fallback.
- Every decision gets logged with its reasoning — accuracy is computed, not asserted.

## Key docs
- `docs/REBUILD_PLAN.md` — the full plan (source of truth).
- `docs/` in this repo mirrors the shared Google Doc the client team reads.
