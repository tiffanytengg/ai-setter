"""Agent service entrypoint (Track B).

POST /webhook/message  (called by thin n8n WF-01)
  1. deterministic pre-pass  -> opt-out / duplicate resolves early (mode "none")
  2. cheap relevance filter  -> not scheduling-related -> mode "none", logged
  3. classify(message, llm_fn) -> keyword baseline by default; llm_fn is the
     injection point for the production LLM path (see _default_llm_fn)
  4. skill_runner.plan -> {skill, mode, action_plan, would_send, send_tool}
  5. decision logged: Postgres (setter-memory schema) when DATABASE_URL is
     set, else JSON to stdout. Logging never breaks the pipeline.
  6. returns {message_id, intent, confidence, reasoning, skill, mode,
     action_plan, would_send, send_tool, execution, send_preview}

The agent service NEVER sends anything. Sends only happen inside the
ghl-gateway MCP server, behind its own two gates. close-gateway is
read-only by design (client requirement).

GET /health -> {"ok": true}

Auth: every /webhook/message call must carry header X-Agent-Token matching
AGENT_SHARED_SECRET (constant-time compare). If AGENT_SHARED_SECRET is
unset, the service refuses to start serving authenticated traffic unless
AGENT_ALLOW_NOAUTH=true, in which case it logs a LOUD warning on every
request. Dev-only.

A single bad message never raises: pipeline errors are returned as a
structured {"error": ..., "detail": ...} dict with mode "none".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request

from agent_service.prepass import check as prepass_check
from agent_service.relevance import is_scheduling_related
from agent_service.classifier import classify
from agent_service.skill_runner import plan as skill_plan
from agent_service.execute import execute_plan, render_send_preview

log = logging.getLogger("agent_service")

app = FastAPI(title="ai-setter agent-service")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_NOAUTH_WARNED = False


def _require_auth(request: Request) -> None:
    """Raise 401/503 unless the request is authorized. Dev bypass only with
    AGENT_ALLOW_NOAUTH=true and a loud warning."""
    global _NOAUTH_WARNED
    secret = os.environ.get("AGENT_SHARED_SECRET")
    if secret:
        token = request.headers.get("x-agent-token") or ""
        if not secrets.compare_digest(token, secret):
            raise HTTPException(status_code=401, detail="unauthorized: bad or missing X-Agent-Token")
        return
    if os.environ.get("AGENT_ALLOW_NOAUTH") != "true":
        raise HTTPException(
            status_code=503,
            detail="server misconfigured: AGENT_SHARED_SECRET is not set and "
                   "AGENT_ALLOW_NOAUTH != 'true'. Refusing to serve.",
        )
    if not _NOAUTH_WARNED:
        log.warning("!!! AUTH DISABLED: AGENT_SHARED_SECRET unset and "
                    "AGENT_ALLOW_NOAUTH=true. Dev only — never production.")
        _NOAUTH_WARNED = True


# ---------------------------------------------------------------------------
# LLM injection point
# ---------------------------------------------------------------------------

def _default_llm_fn():
    """Return an llm_fn(prompt)->str unless explicitly disabled.

    The LLM path is the service default. Set AGENT_USE_LLM=false for the
    offline keyword baseline. Missing/invalid keys and network failures are
    handled per message by classifier.classify(), which falls back to the
    keyword baseline instead of raising.
    """
    if os.environ.get("AGENT_USE_LLM", "true").strip().lower() == "false":
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("AGENT_LLM_MODEL", "claude-haiku-4-5")
    try:
        timeout = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10.0

    def _call(prompt: str) -> str:
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        import anthropic  # deferred: only needed on the LLM path

        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        resp = client.messages.create(
            model=model, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    return _call


# ---------------------------------------------------------------------------
# Decision logging (setter-memory schema: messages + decisions)
# ---------------------------------------------------------------------------

def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _action_taken(mode: str, would_send: bool) -> str:
    if mode == "active" and would_send:
        return "planned_send"  # dry-run plan; the gateway is the only sender
    if mode == "active":
        return "active_no_send"
    if mode == "shadow":
        return "shadow_logged"
    return "none"


def log_decision(message: dict, result: dict) -> None:
    """Persist one decision. Postgres when DATABASE_URL is set, else JSON to
    stdout. Never raises — logging must not break the pipeline."""
    record = {
        "message_id": message.get("message_id"),
        "channel": message.get("channel"),
        "source": message.get("source"),
        "sender": message.get("sender"),
        "intent": result.get("intent"),
        "confidence": result.get("confidence"),
        "reasoning": result.get("reasoning"),
        "skill": result.get("skill"),
        "mode": result.get("mode"),
        "would_send": result.get("would_send"),
        "send_tool": result.get("send_tool"),
        "escalation_note": result.get("escalation_note"),
        "action_taken": _action_taken(result.get("mode") or "none",
                                      bool(result.get("would_send"))),
        "action_plan": result.get("action_plan"),
        "execution": result.get("execution"),
        "send_preview": result.get("send_preview"),
    }
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print(json.dumps({"decision": record}), flush=True)
        return
    try:
        import psycopg  # deferred: optional dependency
        from psycopg.types.json import Json

        received_at = _parse_ts(message.get("timestamp"))
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS mode TEXT")
                cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS would_send BOOLEAN")
                cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS send_tool TEXT")
                cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS escalation_note TEXT")
                cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS action_plan JSONB")
                cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS execution JSONB")
                cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS send_preview JSONB")
                cur.execute(
                    """
                    INSERT INTO messages
                        (id, message_id, channel, source, sender, body, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (message_id) DO NOTHING
                    """,
                    (uuid.uuid4().hex, record["message_id"], record["channel"],
                     record["source"], record["sender"], message.get("body"),
                     received_at),
                )
                cur.execute(
                    """
                    INSERT INTO decisions
                        (id, message_id, intent, confidence, reasoning, skill,
                         mode, would_send, send_tool, escalation_note,
                         action_taken, action_plan, execution, send_preview)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (uuid.uuid4().hex, record["message_id"], record["intent"],
                     record["confidence"], record["reasoning"], record["skill"],
                     record["mode"], record["would_send"], record["send_tool"],
                     record["escalation_note"], record["action_taken"],
                     Json(record.get("action_plan")),
                     Json(record.get("execution")), Json(record.get("send_preview"))),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — DB failure degrades to stdout
        log.warning("decision DB log failed (%s); falling back to stdout",
                    type(exc).__name__)
        print(json.dumps({"decision": record, "db_error": str(exc)[:200]}),
              flush=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def handle_message_dict(payload: dict, llm_fn=None) -> dict:
    """Run the full pipeline on one canonical message dict. Never raises on
    a bad message — returns a structured error dict instead."""
    message = payload if isinstance(payload, dict) else {}
    message_id = message.get("message_id") or hashlib.sha256(
        json.dumps(message, sort_keys=True).encode()).hexdigest()[:16]

    def _base(**kw):
        out = {
            "message_id": message_id,
            "intent": None,
            "confidence": 0.0,
            "reasoning": "",
            "skill": None,
            "mode": "none",
            "action_plan": [],
            "would_send": False,
            "send_tool": None,
        }
        out.update(kw)
        return out

    try:
        # 1. Deterministic pre-pass: opt-outs and duplicates resolve early.
        pre = prepass_check(message)
        if pre["action"] == "drop":
            result = _base(
                intent="opt_out" if pre["reason"] == "opt-out" else "other",
                reasoning=f"pre-pass drop: {pre['reason']}"
                          + (f" (matched {pre['matched']!r})" if pre.get("matched") else ""),
                action_plan=[f"Pre-pass dropped the message ({pre['reason']}); no further action."],
            )
            log_decision(message, result)
            return result

        # 2. Cheap relevance filter.
        rel = is_scheduling_related(message, llm_fn=llm_fn)
        if not rel.get("relevant"):
            result = _base(
                intent="other",
                reasoning=f"not scheduling-related ({rel.get('method')}: {rel.get('reason')})",
                action_plan=["Relevance filter: not scheduling-related; no further action."],
            )
            log_decision(message, result)
            return result

        # 3. Structured classifier (keyword baseline default; llm_fn injected).
        classification = classify(message, llm_fn=llm_fn)

        # 4. Skill plan (dry-run; never sends).
        planned = skill_plan(classification, message)

        result = _base(
            intent=classification.get("intent"),
            confidence=classification.get("confidence"),
            reasoning=classification.get("reasoning"),
            skill=planned.get("skill"),
            mode=planned.get("mode"),
            action_plan=planned.get("action_plan"),
            would_send=planned.get("would_send"),
            send_tool=planned.get("send_tool"),
        )
        if planned.get("escalation_note"):
            result["escalation_note"] = planned["escalation_note"]
        if planned.get("shadowed_from_active"):
            result["shadowed_from_active"] = True
            result["would_send_before_shadow"] = planned.get("would_send_before_shadow")
            result["send_tool_before_shadow"] = planned.get("send_tool_before_shadow")
            result["mode_before_shadow"] = planned.get("mode_before_shadow")
            result["send_preview"] = render_send_preview(planned, message)

        # 5. Execute active plans through the MCP gateways (fail-closed:
        #    unconfigured gateways, empty renders, and gateway refusals all
        #    produce "not executed / not sent" — never a silent send).
        execution = execute_plan(planned, message)
        result["execution"] = execution
        if planned.get("would_send"):
            result["action_plan"] = list(result["action_plan"]) + [
                ("Execution: " if execution["executed"] else "Execution skipped: ")
                + execution["reason"]
            ]

        log_decision(message, result)
        return result

    except Exception as exc:  # noqa: BLE001 — one bad message never 500s the batch
        log.exception("pipeline error for message %s", message_id)
        return _base(
            reasoning=f"pipeline error: {type(exc).__name__}: {exc}",
            action_plan=["Pipeline error; message logged, no action taken."],
            error="pipeline_error",
            detail=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/webhook/message")
def webhook_message(payload: dict, request: Request) -> dict:
    _require_auth(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    return handle_message_dict(payload, llm_fn=_default_llm_fn())


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("AGENT_SERVICE_PORT", "8000")))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()
