# Skill: send-booking-link

**Mode:** ACTIVE — may send (via `ghl.send_sms`; the allowlist is enforced inside the gateway, not here).

## Trigger

- Classification `intent == "scheduling"` (lead wants to book a first call) AND `confidence >= 0.7`.
- Below 0.7, or any trigger ambiguity → escalate; doing nothing is the default.
- Phase 1: only fires when `message.channel == "sms"`. Email messages are Phase 2 territory.

## Inputs

- Canonical message: `message_id, channel, source, sender, body, timestamp` (channel `"sms"`).
- Classification: `{"intent": "scheduling", "confidence": float, "reasoning": str}`.
- Booking-link resolution for the sending account: sender profile field
  `user.scheduling_links.main_setter`, falling back to the static account→link map.

## Steps

1. Resolve the booking link for the sender: read `user.scheduling_links.main_setter`
   from the sending user's profile; fall back to the static account→link map.
2. If the resolved link is empty or unknown → stop, log, and escalate (see Guardrails).
3. `render_template(template="booking", lead_id=..., booking_link=...)` — populate
   the booking template with the resolved link. Never substitute a placeholder link.
4. `send_sms(lead_id=..., body=...)` — the gateway runs the allowlist check inside
   the send; this skill cannot bypass it.
5. `memory.log_decision` — log intent, confidence, reasoning, skill name, message_id,
   rendered body, and the send outcome.

## Guardrails

- **Never invent a booking link.** If link resolution returns empty or unknown,
  do not send — escalate to a human instead.
- Only acts when `channel == "sms"` in Phase 1 (email is Phase 2).
- Never sends without confidence >= 0.7 and an allowlisted recipient.
- Only `close-gateway` can send; this skill can only *propose* a send.
- No credential, token, or secret material may appear in logs or templates.

## Output

- `{"skill": "send-booking-link", "mode": "active", "sent": bool,
  "message_id": str, "reason": str}` — plus the `memory.log_decision` record.
- On escalate: `"sent": false` with a `reason` naming the guardrail that blocked the send.

## Phase 2 (email)

Same trigger, same template logic, same guardrails — channel is a parameter, not a
new skill. The final send goes via `ghl.send_email` instead of `ghl.send_sms`;
the `send_tool` in the plan becomes `"ghl.send_email"` and the channel gate
becomes `message.channel == "email"`.
