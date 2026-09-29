# agent-service

FastAPI service + Claude Agent SDK. Called by the thin n8n WF-01 over HTTP.

Request lifecycle (`src/agent_service/`):
- `prepass.py` — deterministic, no model: opt-outs (compliance), duplicates
  (idempotency key). **Built (Track A).** `check(message)` → continue/drop.
  Only bare-keyword messages ("STOP", "CANCEL!") drop as opt-outs; embedded
  uses ("cancel my appointment") flow to the classifier. Test:
  `python3 -m agent_service.test_prepass` (12 tests, stdlib only).
- `relevance.py` — cheap binary gate: "is this about scheduling at all?"
  **Built (Track A).** `is_scheduling_related(message, llm_fn=None)`; LLM path
  uses an injectable `llm_fn(prompt)->str`, keyword fallback for offline
  testing (errs toward relevant — false positives are cheap, false negatives
  drop leads). Demo: `python3 -m agent_service.demo_relevance`.
- `classifier.py` — structured output: intent, confidence, reasoning.
  **Built (Track A).** `classify(message, llm_fn=None)`; intents: scheduling,
  rescheduling, coaching_checkin, refund, cancellation, technical, opt_out,
  other. Prompt template documented in the module docstring; LLM path never
  raises on bad model output (falls back to other/0.0). Keyword baseline is
  offline-testing only. Demo: `python3 -m agent_service.demo_classifier`.
- `skills/` — versioned behaviour files. **Refined (Track A).** Active:
  send-booking-link, send-reschedule-link. Shadow (log + escalate, never send):
  handle-refund, handle-cancellation, handle-tech-issue.
- `skill_runner.py` — **built (Track A):** `plan(classification, message)` maps
  intent → skill and produces a DRY-RUN action plan. Never sends. Below 0.7
  confidence → no action ("doing nothing is the default"). Active skills gated
  to channel=="sms" in Phase 1. Demo: `python3 -m agent_service.demo_skills`.
- `app.py` — `POST /webhook/message`, `GET /health`. **Built (Track B):**
  full pipeline (pre-pass → relevance → classify → skill plan → decision
  log), auth via `X-Agent-Token` / `AGENT_SHARED_SECRET`, Postgres logging
  per the setter-memory schema (stdout JSON fallback). Run:
  `cd agent-service/src && AGENT_ALLOW_NOAUTH=true python3 -m uvicorn agent_service.app:app --port 8000`
  (dev only — set `AGENT_SHARED_SECRET` for anything real; see `.env.example`).

The service never touches Close/GHL/Postgres directly — everything goes
through MCP tools on the three gateway servers. No secrets in code (env vars
only); nothing in this package sends — the only send path is
`close-gateway.send_sms`/`send_email`, stubbed behind the allowlist.
