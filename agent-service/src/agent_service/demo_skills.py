"""Demo for the dry-run skill planner (Track A).

Feeds ~6 sample classifications through skill_runner.plan() and prints the
resulting plans. NOTHING SENDS — every plan is a dry run.
"""

from __future__ import annotations

import json

from agent_service.skill_runner import plan


def _msg(message_id, channel="sms", source="close"):
    return {
        "message_id": message_id,
        "channel": channel,
        "source": source,
        "sender": "+15551234567",
        "body": "(sample body)",
        "timestamp": "2026-09-27T12:00:00-04:00",
    }


CASES = [
    # 1. Active: scheduling on SMS, high confidence -> send-booking-link plan.
    (
        "active: scheduling (sms)",
        {"intent": "scheduling", "confidence": 0.93,
         "reasoning": "lead asked to book a first call"},
        _msg("m-001"),
    ),
    # 2. Active: rescheduling on SMS, high confidence -> send-reschedule-link plan.
    (
        "active: rescheduling (sms)",
        {"intent": "rescheduling", "confidence": 0.88,
         "reasoning": "lead wants to move existing call"},
        _msg("m-002"),
    ),
    # 3. Shadow: refund -> log + escalate, never sends.
    (
        "shadow: refund",
        {"intent": "refund", "confidence": 0.91,
         "reasoning": "billing dispute / refund request"},
        _msg("m-003"),
    ),
    # 4. Shadow: cancellation -> log + escalate, never sends.
    (
        "shadow: cancellation",
        {"intent": "cancellation", "confidence": 0.85,
         "reasoning": "lead wants to cancel the programme"},
        _msg("m-004"),
    ),
    # 5. Low confidence: scheduling at 0.42 -> mode none, escalate.
    (
        "low confidence: scheduling (0.42)",
        {"intent": "scheduling", "confidence": 0.42,
         "reasoning": "ambiguous ask, could be coaching question"},
        _msg("m-005"),
    ),
    # 6. None: opt_out has no skill -> mode none, no action.
    (
        "none: opt_out",
        {"intent": "opt_out", "confidence": 0.97,
         "reasoning": "lead replied STOP"},
        _msg("m-006"),
    ),
    # 7. Active intent on email channel -> gated to none (Phase 1 is SMS-only).
    (
        "gated: scheduling on email (Phase 2 territory)",
        {"intent": "scheduling", "confidence": 0.9,
         "reasoning": "lead asked to book a first call"},
        _msg("m-007", channel="email"),
    ),
]


def main():
    print("DRY RUN — the planner never sends. Each plan below is hypothetical.\n")
    for label, classification, message in CASES:
        result = plan(classification, message)
        print("=" * 70)
        print("case:", label)
        print("input:", json.dumps(classification), "| channel:", message["channel"])
        print("plan:", json.dumps(result, indent=2))
        print()


if __name__ == "__main__":
    main()
