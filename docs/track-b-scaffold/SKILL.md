---
name: "track-b-scaffold"
description: "Scaffold the Track B gateway and thin n8n orchestration layer inside the ai-setter repo: close-gateway (read-only MCP server), ghl-gateway (read + gated-send MCP server), the agent-service FastAPI entrypoint, the fixed thin n8n WF-01 workflow, and smoke tests. Use when setting up Track B folders/files in a fresh checkout of the ai-setter repo."
---

# Track B Scaffold

## Purpose
Create every Track B folder and starter file in the ai-setter repo so the gateway + thin-orchestration layer exists with the correct structure, contracts, and safety rules. Track A files (`eval/`, `classifier.py`, `prepass.py`, `relevance.py`, `skill_runner.py` logic, `skills/*.md`) are never touched, with one documented exception below.

## Workflow
1. Confirm the repo root: it must contain `agent-service/`, `eval/`, `n8n/`, and `mcp-servers/`. Create any missing Track B paths from the Output Contract tree. Never delete files. Never overwrite an existing file except `n8n/wf-01-thin.json`, and there only with the surgical fix in step 3.
2. Write each starter file according to its contract in `references/file-contracts.md`. Stubs must fail honestly: missing credentials produce "not configured" errors, never fake data, never a send.
3. Apply the n8n fix to `wf-01-thin.json`: the "Call agent service" node URL becomes `={{ $env.AGENT_SERVICE_URL }}/webhook/message` with header `X-Agent-Token: ={{ $env.AGENT_SHARED_SECRET }}`; rename the node "Call close-gateway (STUB)" to "Call ghl-gateway (STUB)" and update its notes to reference `ghl-gateway.send_sms`.
4. Write `.env.example` files documenting every variable. No real secrets, ever.
5. Run `python3 tests/test_trackb_smoke.py` and the existing Track A tests. Report pass/fail counts. If anything fails, fix the scaffold — do not leave a red tree.

## Output Contract
```
mcp-servers/
  close-gateway/                 # READ-ONLY by design — no send tool may exist
    pyproject.toml               # deps: mcp, httpx
    README.md
    .env.example                 # CLOSE_API_KEY, BOOKING_LINK_MAP
    src/close_gateway/__init__.py
    src/close_gateway/server.py  # tools: get_message, get_lead, render_template
  ghl-gateway/                   # the ONLY sender in the system (gated)
    pyproject.toml               # deps: mcp, httpx
    README.md
    .env.example                 # GHL_PRIVATE_TOKEN, GHL_LOCATION_ID, SEND_ENABLED, SEND_ALLOWLIST
    src/ghl_gateway/__init__.py
    src/ghl_gateway/server.py    # tools: get_contact, get_conversation, send_sms (gated)
agent-service/
  src/agent_service/app.py       # FastAPI: GET /health, POST /webhook/message
n8n/
  wf-01-thin.json                # fixed per step 3 (thin: webhook -> normalize -> agent -> dispatch -> log)
tests/
  test_trackb_smoke.py           # gate matrix, no-send-tools, pipeline, n8n JSON checks
```

## Operating Rules
1. **Close is READ-ONLY.** No send tool may exist in `close-gateway`, not even a disabled one.
2. **The only send in the system is `ghl-gateway.send_sms`.** Both gates live INSIDE the function: `SEND_ENABLED == "true"`, AND `SEND_ALLOWLIST` is non-empty AND contains the recipient. Defaults send nothing. This is the client's "never writes without Adam's OK, test contacts only" rule enforced in code.
3. All credentials come from environment variables. Never write a secret to any file.
4. Missing credentials → honest `"not configured"` errors. Never synthesize data.
5. Keep n8n thin: webhook → normalize → call agent → dispatch → log. No reasoning, no business rules in n8n.
6. Track A is frozen. Send tools are source-matched in `skill_runner.py`: Close plans use the shadow-only `close.send_sms` label, while GHL plans use the gated `ghl.send_sms` path.
