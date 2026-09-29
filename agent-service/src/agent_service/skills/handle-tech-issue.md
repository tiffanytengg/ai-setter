# Skill: handle-tech-issue

**Mode:** SHADOW — log + escalate to a human. **Never sends anything.** No send tools are available to this skill.

## Trigger

- Classification `intent == "technical"` (login, access, or platform problem) AND `confidence >= 0.7`.
- Below 0.7, or any trigger ambiguity → escalate; doing nothing is the default.
- Fires on any channel (`sms` or `email`); the skill itself never sends regardless.

## Inputs

- Canonical message: `message_id, channel, source, sender, body, timestamp`.
- Classification: `{"intent": "technical", "confidence": float, "reasoning": str}`.

## Steps

1. Record the action the system *would* have taken (e.g. draft a human acknowledgment — never sent).
2. `memory.log_decision` — log intent, confidence, reasoning, skill name, message_id,
   and the would-have-taken action.
3. Escalate to Slack for a human reviewer, attaching the log reference and the original message.

## Guardrails

- **Never sends.** This is a shadow skill; it exists so the system stays honest about
  what it chose *not* to do.
- May use `memory.log_decision` only — no `send_sms`, no `send_email`, no `render_template` side effects.
- Do not attempt troubleshooting steps on the lead's behalf; escalate the raw report.
- Doing nothing is the default: on any doubt, escalate without taking action.

## Output

- `{"skill": "handle-tech-issue", "mode": "shadow", "sent": false,
  "message_id": str, "reason": "escalated to human"}` — plus the `memory.log_decision`
  record and the Slack escalation reference.

## Phase 2 (email)

Unchanged behavior — shadow skills already fire on any channel. Phase 2 adds nothing
except that the original message may arrive with `channel == "email"`; the outcome is
still log + escalate, never send.
