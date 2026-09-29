"""Demo: run sample messages through both relevance-filter paths.

Run with:
    cd agent-service/src && python -m agent_service.demo_relevance

Both paths run offline. The LLM path uses a stub ``llm_fn`` (no network)
standing in for the cheap production model; the keyword path is the
offline-testing fallback. Nothing sends, nothing leaves the machine.
"""

from agent_service.relevance import is_scheduling_related


def _mk(mid, channel, source, sender, body):
    return {
        "message_id": mid,
        "channel": channel,
        "source": source,
        "sender": sender,
        "body": body,
        "timestamp": "2026-09-27T12:00:00-04:00",
    }


SAMPLES = [
    _mk("m1", "sms", "close", "+1 555-0101",
        "Hey, can we move my call on Friday to sometime next week?"),
    _mk("m2", "sms", "close", "+1 555-0102",
        "I'd like to book a discovery call, when are you available?"),
    _mk("m3", "sms", "close", "+1 555-0103",
        "Thanks for the new workout plan, feeling great this week!"),
    _mk("m4", "sms", "close", "+1 555-0104",
        "STOP"),
    _mk("m5", "email", "ghl", "lead@example.com",
        "Sorry I missed our session, can I reschedule for Tuesday morning?"),
    _mk("m6", "sms", "close", "+1 555-0106",
        "How do I adjust my macros on rest days?"),
    _mk("m7", "sms", "close", "+1 555-0107",
        "Do you have a time slot open tomorrow afternoon?"),
    _mk("m8", "sms", "close", "+1 555-0108",
        "This workout is kicking my butt, love it! Quick Q though - "
        "can we push back our check-in call by 30 min?"),
    _mk("m9", "email", "ghl", "prospect@example.com",
        "Just confirming our appointment on Oct 5th, see you then!"),
    _mk("m10", "sms", "close", "+1 555-0110",
        "ok"),
]

_SCHEDULING_WORDS = (
    "book", "schedule", "reschedule", "appointment", "call",
    "session", "calendar", "available", "slot", "move",
)


def stub_llm_fn(prompt: str) -> str:
    """Offline stand-in for the production cheap model.

    Answers the binary scheduling question from the message body embedded
    in the prompt. Returns varied-but-valid formats ("Yes", "no", "YES")
    to exercise the lenient parser. No network, no side effects.
    """
    # The body is everything after the last "Message: " line.
    body = prompt.rsplit("Message:", 1)[-1].rsplit("Answer:", 1)[0]
    text = body.lower()
    hit = any(w in text for w in _SCHEDULING_WORDS)
    # Opt-outs are not scheduling, even though "call" never appears there.
    if "stop" in text and len(text.strip()) < 12:
        return "no"
    return "Yes" if hit else "no"


def main() -> None:
    print(f"{'id':<4} {'body (truncated)':<52} {'LLM':<6} {'KW':<6} "
          f"{'keyword reason'}")
    print("-" * 120)
    for msg in SAMPLES:
        llm = is_scheduling_related(msg, llm_fn=stub_llm_fn)
        kw = is_scheduling_related(msg)
        preview = (msg["body"][:49] + "...") if len(msg["body"]) > 52 else msg["body"]
        print(f"{msg['message_id']:<4} {preview:<52} "
              f"{str(llm['relevant']):<6} {str(kw['relevant']):<6} "
              f"{kw['reason']}")
    print()
    print("LLM reason (m1):", is_scheduling_related(SAMPLES[0],
          llm_fn=stub_llm_fn)["reason"])
    print("LLM reason (m4):", is_scheduling_related(SAMPLES[3],
          llm_fn=stub_llm_fn)["reason"])


if __name__ == "__main__":
    main()
