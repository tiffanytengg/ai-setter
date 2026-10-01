"""Dry-run skill planner for the AI Setter rebuild (Track A).

Maps an intent classification + canonical message to the skill that would
handle it, producing an *action plan only*. This module NEVER sends anything:
there are no network calls, no secrets, stdlib only.

Safety principle: "doing nothing is the default." Below the confidence
threshold, or for intents with no skill, the planner returns mode "none"
and an escalation note instead of a plan.

Contracts:
- Classification: {"intent": str, "confidence": float, "reasoning": str}
  intent in {scheduling, rescheduling, coaching_checkin, refund,
  cancellation, technical, opt_out, other}
- Canonical message: {message_id, channel ("sms"|"email"), source
  ("close"|"ghl"), sender, body, timestamp}

See skills/*.md for the authoritative per-skill behavior docs.
"""

from __future__ import annotations

import os

CONFIDENCE_THRESHOLD = 0.7

ACTIVE_SEND_TOOL = "ghl.send_sms"

# intent -> (skill file slug, mode). Active skills may send; shadow skills
# only log + escalate. Anything else maps to no skill.
_INTENT_ROUTING = {
    "scheduling": ("send-booking-link", "active"),
    "rescheduling": ("send-reschedule-link", "active"),
    "refund": ("handle-refund", "shadow"),
    "cancellation": ("handle-cancellation", "shadow"),
    "technical": ("handle-tech-issue", "shadow"),
    "opt_out": (None, "none"),
    "coaching_checkin": (None, "none"),
    "other": (None, "none"),
}

DRY_RUN_MARKER = "DRY RUN — no send executed"


def _template_for(skill):
    return "booking" if skill == "send-booking-link" else "reschedule"


def plan(classification, message):
    """Build a dry-run action plan. Never sends.

    Returns:
        {"skill": str|None, "mode": "active"|"shadow"|"none",
         "action_plan": [steps...], "would_send": bool,
         "send_tool": "ghl.send_sms"|None, "escalation_note": str|None}
    """
    intent = (classification or {}).get("intent")
    confidence = (classification or {}).get("confidence")
    reasoning = (classification or {}).get("reasoning", "")
    message_id = (message or {}).get("message_id")
    channel = (message or {}).get("channel")

    skill, mode = _INTENT_ROUTING.get(intent, (None, "none"))

    # Safety layer: confidence gate. Below threshold, do nothing but escalate
    # for review — "doing nothing is the default".
    if not isinstance(confidence, (int, float)) or confidence < CONFIDENCE_THRESHOLD:
        return {
            "skill": skill,
            "mode": "none",
            "action_plan": [
                "Classify intent ({} confidence {:.2f} below threshold {}). "
                "Take no action.".format(intent, confidence or 0.0, CONFIDENCE_THRESHOLD),
            ],
            "would_send": False,
            "send_tool": None,
            "escalation_note": "low confidence — escalate for review",
        }

    if mode == "none":
        note = "intent '{}' has no skill (reasoning: {}) — no action taken".format(
            intent, reasoning
        )
        return {
            "skill": None,
            "mode": "none",
            "action_plan": ["Classify intent ({}) and take no action.".format(intent)],
            "would_send": False,
            "send_tool": None,
            "escalation_note": note,
        }

    if mode == "shadow":
        return {
            "skill": skill,
            "mode": "shadow",
            "action_plan": [
                "Record the action the system would have taken for message {} "
                "(intent {}, confidence {:.2f}).".format(message_id, intent, confidence),
                "Log decision via memory.log_decision with intent, confidence, "
                "reasoning, and skill name.",
                "Escalate to Slack for a human reviewer ({}).".format(DRY_RUN_MARKER),
            ],
            "would_send": False,
            "send_tool": None,
            "escalation_note": "shadow mode — logged and escalated, never sent",
        }

    # mode == "active"
    # Phase 1 gate: active skills only act on SMS. Email is Phase 2.
    if channel != "sms":
        return {
            "skill": skill,
            "mode": "none",
            "action_plan": [
                "Classify intent ({} confidence {:.2f}) but channel is '{}' — "
                "Phase 1 is SMS-only. Take no action.".format(intent, confidence, channel),
            ],
            "would_send": False,
            "send_tool": None,
            "escalation_note": (
                "active skill requires channel 'sms' in Phase 1 "
                "(email is Phase 2) — escalate for review"
            ),
        }

    template = _template_for(skill)
    planned = {
        "skill": skill,
        "mode": "active",
        "action_plan": [
            "[{}] Resolve booking link for sender from "
            "user.scheduling_links.main_setter (fallback: static account->link map). "
            "If empty/unknown → escalate instead of sending.".format(DRY_RUN_MARKER),
            "[{}] Planned: render_template(template={!r}, lead_id=<lead>, "
            "booking_link=<resolved>).".format(DRY_RUN_MARKER, template),
            "[{}] Planned: send_sms(lead_id=<lead>, body=<rendered>) via "
            "{} — allowlist enforced inside the gateway.".format(DRY_RUN_MARKER, ACTIVE_SEND_TOOL),
            "[{}] Log outcome via memory.log_decision (intent, confidence, "
            "reasoning, message_id, rendered body).".format(DRY_RUN_MARKER),
        ],
        "would_send": True,
        "send_tool": ACTIVE_SEND_TOOL,
        "escalation_note": None,
    }

    # Forced shadow (demo/rehearsal): keep the full action plan as built, but
    # downgrade so execute_plan() never touches the gateways.
    if os.environ.get("AGENT_FORCE_SHADOW") == "true":
        planned["mode"] = "shadow"
        planned["would_send"] = False
        planned["send_tool"] = None
        planned["escalation_note"] = (
            "forced shadow via AGENT_FORCE_SHADOW — full plan built and logged, "
            "never executed"
        )
    return planned
