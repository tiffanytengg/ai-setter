# AI Setter — MCP rebuild

Rebuild of the Committed Coaches AI setter automation: n8n becomes a thin
orchestration layer; reasoning moves to an agent service that acts through
three trust-scoped MCP servers.

- `agent-service/` — FastAPI + Claude Agent SDK: deterministic pre-pass, cheap
  relevance filter, classifier, skills.
- `mcp-servers/close-gateway` — Close tools. The ONLY thing that can send SMS.
  The allowlist is enforced inside `send_sms`, not offered as a tool.
- `mcp-servers/ghl-gateway` — GoHighLevel read tools (read-only by design).
- `mcp-servers/setter-memory` — Postgres logging/state. No external network.
- `eval/` — golden set, eval harness, weak labeling.
- `n8n/` — thin WF-01 definition (webhook → normalise → call agent → dispatch → log).
- `docs/` — rebuild plan and architecture notes.

Safety principle: doing nothing is the default. Acting requires explicit,
positive authorisation, enforced inside the gateway — never as a tool the
agent is asked to call.

Full plan: `docs/REBUILD_PLAN.md`
