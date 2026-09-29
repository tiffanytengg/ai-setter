"""Cheap relevance filter.

Second layer of the safety pipeline ("doing nothing is the default"):
deterministic pre-pass (opt-outs / duplicates / own-team) runs first, then
this filter removes the ~90% of inbound traffic that is pure coaching
content, so only scheduling-adjacent messages reach the structured
classifier. Cost control lives here.

Two execution paths:
- **LLM path** (production): a single binary question is asked of a cheap
  model (Haiku-class). The caller injects the model via ``llm_fn`` so this
  module never touches network or credentials.
- **Keyword path** (``llm_fn=None``): offline heuristic fallback used for
  local testing and the eval harness. NOT production logic. It errs on the
  side of ``relevant=True``: a false positive just costs a classifier call,
  while a false negative silently drops a real lead.

Exact LLM prompt template (production):

    You are a binary relevance filter for a coaching company's inbound
    message pipeline. Answer with exactly one word: "yes" or "no".

    Question: Is this message about scheduling, booking, or rescheduling
    a call or appointment?

    Examples:
    Message: "Hey, can we move my call on Friday to sometime next week?"
    Answer: yes

    Message: "Thanks for the new workout plan, feeling great this week!"
    Answer: no

    Message: "STOP"
    Answer: no

    Message: <raw message body verbatim>
    Answer:

The model may only return "yes" or "no"; parsing is lenient
(whitespace-stripped, lowercased; accepts yes/no/true/false) so minor
format drift doesn't break the pipeline.
"""

# Keyword fallback word lists. Positive signals make a message relevant.
_POSITIVE_SIGNALS = (
    "book",
    "booking",
    "schedule",
    "scheduling",
    "reschedule",
    "appointment",
    "call",
    "session",
    "calendar",
    "available",
    "availability",
    "time slot",
    "timeslot",
    "move it",
    "push it back",
    "push back",
    "set up a time",
    "set a time",
    "what time",
    "when can",
    "tomorrow",
    "next week",
)

# Negative overrides: content that is NOT scheduling-related. These only
# apply when NO positive signal is present, so mixed messages (e.g. a
# coaching question that also asks to move a call) stay relevant.
_NEGATIVE_SIGNALS = (
    "stop",
    "unsubscribe",
    "opt out",
    "opt-out",
    "workout",
    "meal plan",
    "nutrition",
    "macro",
    "feeling great",
    "thank you",
    "thanks",
    "how do i",
    "should i",
    "what should",
    "motivat",
)


def _parse_llm_answer(raw: str) -> bool:
    """Leniently parse an LLM "yes"/"no" answer into a bool."""
    token = (raw or "").strip().lower()
    if token in ("yes", "true", "y", "1"):
        return True
    if token in ("no", "false", "n", "0"):
        return False
    # Fallback: look for the first recognizable token anywhere in the text.
    for word in token.replace(",", " ").split():
        if word in ("yes", "true"):
            return True
        if word in ("no", "false"):
            return False
    raise ValueError(f"Could not parse LLM answer as yes/no: {raw!r}")


def _build_prompt(body: str) -> str:
    """Render the exact production prompt for a raw message body."""
    return (
        "You are a binary relevance filter for a coaching company's inbound\n"
        "message pipeline. Answer with exactly one word: \"yes\" or \"no\".\n"
        "\n"
        "Question: Is this message about scheduling, booking, or rescheduling\n"
        "a call or appointment?\n"
        "\n"
        "Examples:\n"
        "Message: \"Hey, can we move my call on Friday to sometime next week?\"\n"
        "Answer: yes\n"
        "\n"
        "Message: \"Thanks for the new workout plan, feeling great this week!\"\n"
        "Answer: no\n"
        "\n"
        "Message: \"STOP\"\n"
        "Answer: no\n"
        "\n"
        f"Message: {body}\n"
        "Answer:"
    )


def _keyword_relevance(body: str) -> dict:
    """Heuristic fallback: positive signals win; negatives only override
    when no positive signal is present; otherwise default relevant=True."""
    text = (body or "").lower()

    positives = [s for s in _POSITIVE_SIGNALS if s in text]
    negatives = [s for s in _NEGATIVE_SIGNALS if s in text]

    if positives:
        return {
            "relevant": True,
            "method": "keyword",
            "reason": f"positive signal(s): {', '.join(positives)}",
        }
    if negatives:
        return {
            "relevant": False,
            "method": "keyword",
            "reason": f"no scheduling signal; non-scheduling content: "
            f"{', '.join(negatives)}",
        }
    # No signal either way -> err toward relevant. False positives are
    # cheap (the classifier decides); false negatives drop real leads.
    return {
        "relevant": True,
        "method": "keyword",
        "reason": "no signal either way; defaulting relevant=True "
        "(cheap for the classifier, avoids dropped leads)",
    }


def is_scheduling(text: str) -> dict:
    """Return {"scheduling": bool, "reason": str}."""
    raise NotImplementedError("Day 2")


def is_scheduling_related(message: dict, llm_fn=None) -> dict:
    """Cheap relevance gate: is this message about scheduling at all?

    Args:
        message: canonical message dict with at least ``body``.
        llm_fn: optional callable ``llm_fn(prompt: str) -> str`` returning
            "yes"/"no" (production LLM path). If None, the offline keyword
            fallback is used.

    Returns:
        {"relevant": bool, "method": "llm"|"keyword", "reason": str}
    """
    body = (message or {}).get("body") or ""

    if llm_fn is not None:
        prompt = _build_prompt(body)
        raw = llm_fn(prompt)
        try:
            relevant = _parse_llm_answer(raw)
        except ValueError:
            # Unparseable model output: err toward relevant so the
            # classifier gets a say rather than silently dropping the lead.
            return {
                "relevant": True,
                "method": "llm",
                "reason": f"unparseable LLM answer {raw!r}; defaulting "
                "relevant=True for classifier review",
            }
        return {
            "relevant": relevant,
            "method": "llm",
            "reason": f"LLM answered {raw.strip()!r} to the binary "
            "scheduling question",
        }

    return _keyword_relevance(body)
