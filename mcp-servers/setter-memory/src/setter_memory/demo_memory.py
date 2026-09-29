"""Demo: exercises log_message -> log_decision -> get_state against MemoryStore.

Stdlib + this package only. Run from the src/ directory:
    cd mcp-servers/setter-memory/src && python -m setter_memory.demo_memory
"""

from setter_memory.server import MemoryStore


def main() -> None:
    store = MemoryStore()

    # 1. Log an inbound SMS.
    msg = {
        "message_id": "close-msg-001",
        "channel": "sms",
        "source": "close",
        "sender": "+14155550123",
        "body": "Hi! I'd like to book a call with a coach this week.",
        "timestamp": "2026-09-27T10:15:00Z",
    }
    res = store.log_message(msg)
    print("log_message ->", res)
    assert res == {"ok": True, "message_id": "close-msg-001"}

    # 2. Re-logging the same message is idempotent.
    res = store.log_message(msg)
    print("log_message (duplicate) ->", res)
    assert res["ok"] and len(store.messages) == 1

    # 3. Log the agent's decision (shadow mode: log only, never send).
    res = store.log_decision(
        message_id="close-msg-001",
        intent="booking_request",
        confidence=0.94,
        reasoning="Sender explicitly asks to book a call; matches booking intent pattern.",
        skill="send-booking-link",
        action_taken="shadow_logged",
    )
    print("log_decision ->", res)
    assert res == {"ok": True}

    # 4. Latest decision wins when several exist.
    store.log_decision(
        message_id="close-msg-001",
        intent="booking_request",
        confidence=0.97,
        reasoning="Re-classified after thread context loaded; still booking.",
        skill="send-booking-link",
        action_taken="shadow_logged",
    )
    state = store.get_state("close-msg-001")
    print("get_state ->")
    for section, row in state.items():
        print(f"  {section}:")
        for key, value in (row or {}).items():
            print(f"    {key}: {value}")
    assert state["message"]["sender"] == "+14155550123"
    assert state["decision"]["confidence"] == 0.97

    # 5. Unknown message returns nulls.
    state = store.get_state("no-such-message")
    print("get_state (unknown) ->", state)
    assert state == {"message": None, "decision": None}

    # 6. No env var + no explicit opt-in raises (no silent fallback).
    try:
        from setter_memory.server import make_store
        make_store()
    except RuntimeError as exc:
        print("make_store() without env var -> RuntimeError:")
        print(" ", str(exc)[:120], "...")
    else:  # pragma: no cover
        raise AssertionError("make_store() should have raised")

    print("\nAll demo assertions passed.")


if __name__ == "__main__":
    main()
