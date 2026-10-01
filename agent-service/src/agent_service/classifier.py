"""Classifier: canonical message in, structured decision out.

Contract (other components are built against this — do not change):
    from agent_service.classifier import classify
    classify(message, llm_fn=None) -> {"intent": str, "confidence": float, "reasoning": str}

`message` is the canonical dict: message_id, channel ("sms"|"email"),
source ("close"|"ghl"), sender, body, timestamp (ISO 8601).
`intent` is always one of the exact strings in INTENTS below.

Two paths:
1. LLM path (llm_fn provided): builds CLASSIFY_PROMPT_TEMPLATE, calls
   llm_fn(prompt) -> JSON string, parses leniently. Never raises on model
   output: invalid JSON or an out-of-taxonomy intent returns the
   "other"/0.0 parse-fallback dict.
2. Keyword baseline (llm_fn=None): transparent regex-free heuristic.
   BASELINE FOR OFFLINE TESTING ONLY — not production. It exists so the
   eval harness can run without a model; production must use the LLM path.

The full LLM prompt template (also in CLASSIFY_PROMPT_TEMPLATE):

---
You are the intent classifier for an AI setter at a coaching company. The
setter books and reschedules sales calls; other intents get logged and
escalated, never auto-sent.

Classify the inbound message into exactly ONE intent:

- scheduling: wants to book a NEW call/appointment. Example: "Do you have
  any openings this week?"
- rescheduling: wants to MOVE an existing call/appointment, including
  asking to cancel it so it can be rebooked. Example: "Can we move Tuesday
  to Thursday?"
- coaching_checkin: greeting or coaching check-in with no scheduling ask.
  Example: "Hi, just checking in — how are you?"
- refund: wants money back for a charge. Example: "I'd like a refund for
  last month please."
- cancellation: wants to end their membership/program/subscription.
  Example: "Please cancel my membership."
- technical: login, password, or app problems. Example: "I can't log into
  the app."
- opt_out: wants no more messages (carrier keyword or plain request).
  Example: "STOP"
- other: anything else. Example: "Do you have a location in Austin?"

Tricky distinctions — get these right:
- "cancel my appointment" / "cancel my call" means they want to MOVE it
  -> rescheduling (the setter sends the reschedule link, not a goodbye).
- "cancel my membership/program/subscription/account" -> cancellation.
- "CANCEL" or "STOP" alone -> opt_out.
- "Can we move Tuesday to Thursday?" -> rescheduling.
- "Do you have any openings?" -> scheduling.
- A greeting with no scheduling ask ("hello, how are you?") ->
  coaching_checkin.

Message (channel: {channel}, source: {source}):
---
{body}
---

Respond with ONLY JSON, no other text:
{"intent": "<one of the 8 intents>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}
---

Confidence guidance: 0.9 when the message unambiguously matches one
intent, 0.6 when it is ambiguous between two, 0.3 when it matches none
(use "other").
"""

import json
import logging
import re

log = logging.getLogger("agent_service.classifier")

INTENTS = (
    "scheduling",
    "rescheduling",
    "coaching_checkin",
    "refund",
    "cancellation",
    "technical",
    "opt_out",
    "other",
)

CLASSIFY_PROMPT_TEMPLATE = """You are the intent classifier for an AI setter at a coaching company. The
setter books and reschedules sales calls; other intents get logged and
escalated, never auto-sent.

Classify the inbound message into exactly ONE intent:

- scheduling: wants to book a NEW call/appointment. Example: "Do you have
  any openings this week?"
- rescheduling: wants to MOVE an existing call/appointment, including
  asking to cancel it so it can be rebooked. Example: "Can we move Tuesday
  to Thursday?"
- coaching_checkin: greeting or coaching check-in with no scheduling ask.
  Example: "Hi, just checking in — how are you?"
- refund: wants money back for a charge. Example: "I'd like a refund for
  last month please."
- cancellation: wants to end their membership/program/subscription.
  Example: "Please cancel my membership."
- technical: login, password, or app problems. Example: "I can't log into
  the app."
- opt_out: wants no more messages (carrier keyword or plain request).
  Example: "STOP"
- other: anything else. Example: "Do you have a location in Austin?"

Tricky distinctions — get these right:
- "cancel my appointment" / "cancel my call" means they want to MOVE it
  -> rescheduling (the setter sends the reschedule link, not a goodbye).
- "cancel my membership/program/subscription/account" -> cancellation.
- "CANCEL" or "STOP" alone -> opt_out.
- "Can we move Tuesday to Thursday?" -> rescheduling.
- "Do you have any openings?" -> scheduling.
- A greeting with no scheduling ask ("hello, how are you?") ->
  coaching_checkin.

Message (channel: {channel}, source: {source}):
---
{body}
---

Respond with ONLY JSON, no other text:
{{"intent": "<one of the 8 intents>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}}"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    m = _FENCE_RE.search(raw)
    if m:
        return m.group(1).strip()
    # Tolerate stray leading/trailing text: take the outermost {...} block.
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        return raw[start : end + 1]
    return raw


def _llm_classify(message: dict, llm_fn) -> dict:
    """LLM path. Falls back to keyword baseline on any model failure."""
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        channel=message.get("channel", "?"),
        source=message.get("source", "?"),
        body=message.get("body", ""),
    )
    try:
        raw = llm_fn(prompt)
        data = json.loads(_strip_fences(str(raw)))
        intent = str(data.get("intent", "")).strip().lower()
        confidence = float(data.get("confidence", 0.0))
        reasoning = str(data.get("reasoning", ""))
        if intent not in INTENTS:
            raise ValueError(f"intent {intent!r} not in taxonomy")
    except Exception as exc:  # noqa: BLE001 — fallback must catch every LLM failure
        fallback = _baseline_classify(message.get("body", ""))
        error = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("LLM classifier failed; falling back to keyword baseline: %s", error)
        return {
            **fallback,
            "reasoning": f"llm fallback ({error}); {fallback['reasoning']}",
        }
    confidence = max(0.0, min(1.0, confidence))
    return {"intent": intent, "confidence": confidence, "reasoning": reasoning}


# ---------------------------------------------------------------------------
# Keyword baseline — OFFLINE TESTING ONLY, not production.
# ---------------------------------------------------------------------------
# Transparent heuristic for the eval harness so accuracy can be measured
# without a model call. Rule order is load-bearing: the tricky cancel
# distinctions are disambiguated by checking the longest, most specific
# phrases before any generic "cancel" handling.
#
# Confidence: 0.9 single strong match, 0.6 weak or multiple matches,
# 0.3 default "other".
#
# Tricky distinctions handled here (mirroring the prompt):
# - "cancel my appointment/call" -> rescheduling (wants to move it; the
#   setter sends the reschedule link). Generic "cancel" is NOT a match on
#   its own for this rule — it must name the appointment/call.
# - "cancel my membership/program/subscription/account/plan" -> cancellation.
# - bare "cancel"/"stop"/"unsubscribe" as the whole message -> opt_out
#   (carrier-level keyword).
# - "can we move <day> to <day>" / "move my appointment" -> rescheduling.
# - "do you have any openings" -> scheduling.
# - greetings with no scheduling ask -> coaching_checkin (weak, 0.6 — a
#   bare "hi" could just be politeness).

# Whole-message opt-out keywords (carrier keywords). Checked against the
# stripped, lowercased body only — a bare "stop" is unambiguous.
_BARE_OPT_OUTS = {"stop", "cancel", "unsubscribe", "end", "quit"}

# (intent, phrases, strong). Order = priority when several intents match.
_BASELINE_RULES = [
    (
        "rescheduling",
        (
            "cancel my appointment",
            "cancel the appointment",
            "cancel our appointment",
            "cancel my call",
            "cancel the call",
            "cancel our call",
            "reschedul",
            "move my appointment",
            "move my call",
            "move our call",
            "can we move",
            "change my appointment",
            "change the time",
            "push my appointment",
            "push back my call",
        ),
        True,
    ),
    (
        "cancellation",
        (
            "cancel my membership",
            "cancel my program",
            "cancel my programme",
            "cancel my subscription",
            "cancel my account",
            "cancel my plan",
            "cancel my coaching",
            "cancel my service",
            "cancel everything",
        ),
        True,
    ),
    (
        "refund",
        ("refund", "money back", "chargeback", "charge back"),
        True,
    ),
    (
        "technical",
        (
            "log in",
            "login",
            "log-in",
            "password",
            "app not working",
            "app isn't working",
            "app is not working",
            "app keeps crashing",
            "can't log",
            "cannot log",
            "trouble logging",
            "reset my password",
        ),
        True,
    ),
    (
        "opt_out",
        (
            "unsubscribe",
            "stop texting",
            "stop messaging",
            "do not text",
            "don't text",
            "do not contact",
            "don't contact",
            "take me off",
            "remove me from",
            "opt out",
            "opt-out",
        ),
        True,
    ),
    (
        "scheduling",
        (
            "book a call",
            "book an appointment",
            "book my call",
            "schedule a call",
            "schedule an appointment",
            "schedule my",
            "any openings",
            "availability",
            "sign me up",
            "get started",
            "free consultation",
        ),
        True,
    ),
    (
        "scheduling",
        ("book", "schedul", "appointment"),
        False,
    ),
    (
        "coaching_checkin",
        ("checking in", "check in", "how are you", "hope you're well"),
        False,
    ),
    (
        "coaching_checkin",
        ("hello", "hi there", "hey there", "good morning", "good afternoon"),
        False,
    ),
]


def _baseline_classify(body: str) -> dict:
    text = (body or "").strip().lower()
    if not text:
        return {"intent": "other", "confidence": 0.3,
                "reasoning": "baseline: empty body, default other"}

    # Rule 0: bare carrier keyword -> opt_out, unambiguous.
    if text in _BARE_OPT_OUTS:
        return {"intent": "opt_out", "confidence": 0.9,
                "reasoning": f"baseline: bare opt-out keyword {text!r}"}

    hits = []  # (intent, phrase, strong)
    for intent, phrases, strong in _BASELINE_RULES:
        for phrase in phrases:
            if phrase in text:
                hits.append((intent, phrase, strong))
                break  # one hit per rule group is enough

    if not hits:
        return {"intent": "other", "confidence": 0.3,
                "reasoning": "baseline: no keyword matched, default other"}

    intents_hit = {h[0] for h in hits}
    if len(intents_hit) == 1:
        # One intent only — possibly via several phrases of different
        # strength. Any strong phrase wins 0.9; all-weak is 0.6.
        strong_hits = [h for h in hits if h[2]]
        if strong_hits:
            intent, phrase, _ = strong_hits[0]
            return {"intent": intent, "confidence": 0.9,
                    "reasoning": f"baseline: strong match {phrase!r} -> {intent}"}
        intent, phrase, _ = hits[0]
        return {"intent": intent, "confidence": 0.6,
                "reasoning": f"baseline: weak match {phrase!r} -> {intent}"}

    # Several intents matched: 0.6, highest-priority intent wins, ambiguity logged.
    first = hits[0]
    others = ", ".join(f"{i}({p!r})" for i, p, _ in hits[1:])
    return {"intent": first[0], "confidence": 0.6,
            "reasoning": f"baseline: ambiguous match {first[1]!r} -> {first[0]}; "
                         f"also matched: {others}"}


def classify(message: dict, llm_fn=None) -> dict:
    """Classify one canonical message.

    Returns {"intent": str, "confidence": float 0-1, "reasoning": str}.
    With llm_fn: prompt -> model -> parsed JSON, never raises. Any model,
    network, credential, or parse failure falls back to the keyword baseline
    for that message. Without llm_fn: the keyword baseline (OFFLINE TESTING
    ONLY).
    """
    if llm_fn is not None:
        return _llm_classify(message, llm_fn)
    return _baseline_classify(message.get("body", ""))
