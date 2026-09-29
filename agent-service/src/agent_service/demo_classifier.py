"""Demo: run the keyword baseline over 10 sample messages (all 8 intents).

Usage:
    cd ~/workspace/ai-setter/agent-service/src
    python -m agent_service.demo_classifier
"""

from agent_service.classifier import classify

_SAMPLES = [
    # scheduling
    {"message_id": "m1", "channel": "sms", "source": "close",
     "sender": "+15550001111",
     "body": "Hi! Do you have any openings this week? I'd love to book a call.",
     "timestamp": "2026-09-27T10:00:00-04:00"},
    # rescheduling (move phrasing)
    {"message_id": "m2", "channel": "sms", "source": "close",
     "sender": "+15550002222",
     "body": "Hey, can we move Tuesday to Thursday? Something came up.",
     "timestamp": "2026-09-27T10:05:00-04:00"},
    # rescheduling (cancel-my-appointment -> wants to move it)
    {"message_id": "m3", "channel": "sms", "source": "ghl",
     "sender": "+15550003333",
     "body": "I need to cancel my appointment on Friday, can we find another time?",
     "timestamp": "2026-09-27T10:10:00-04:00"},
    # coaching_checkin
    {"message_id": "m4", "channel": "sms", "source": "close",
     "sender": "+15550004444",
     "body": "Hi! Just checking in, how are you doing?",
     "timestamp": "2026-09-27T10:15:00-04:00"},
    # refund
    {"message_id": "m5", "channel": "email", "source": "close",
     "sender": "client@example.com",
     "body": "I'd like a refund for last month's charge please, I was billed twice.",
     "timestamp": "2026-09-27T10:20:00-04:00"},
    # cancellation
    {"message_id": "m6", "channel": "email", "source": "ghl",
     "sender": "client2@example.com",
     "body": "Please cancel my membership effective immediately.",
     "timestamp": "2026-09-27T10:25:00-04:00"},
    # technical
    {"message_id": "m7", "channel": "sms", "source": "close",
     "sender": "+15550007777",
     "body": "I can't log into the app, it keeps saying wrong password.",
     "timestamp": "2026-09-27T10:30:00-04:00"},
    # opt_out (bare carrier keyword)
    {"message_id": "m8", "channel": "sms", "source": "close",
     "sender": "+15550008888",
     "body": "STOP",
     "timestamp": "2026-09-27T10:35:00-04:00"},
    # other
    {"message_id": "m9", "channel": "email", "source": "ghl",
     "sender": "prospect@example.com",
     "body": "Do you guys have a coaching location in Austin?",
     "timestamp": "2026-09-27T10:40:00-04:00"},
    # scheduling (second example: schedule phrasing)
    {"message_id": "m10", "channel": "sms", "source": "close",
     "sender": "+15550009999",
     "body": "Can I schedule my free consultation for tomorrow morning?",
     "timestamp": "2026-09-27T10:45:00-04:00"},
]


def main() -> None:
    print(f"{'msg':<5} {'intent':<17} {'conf':<5} reasoning")
    print("-" * 100)
    for msg in _SAMPLES:
        result = classify(msg)  # baseline: llm_fn=None
        body_preview = msg["body"][:60].replace("\n", " ")
        print(f"{msg['message_id']:<5} {result['intent']:<17} "
              f"{result['confidence']:<5.1f} {result['reasoning']}")
        print(f"      body: {body_preview!r}")
    print("-" * 100)
    print("baseline (llm_fn=None) — OFFLINE TESTING ONLY, not production")


if __name__ == "__main__":
    main()
