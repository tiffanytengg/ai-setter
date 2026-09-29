"""Deterministic pre-pass for the AI Setter agent-service.

Layer 1 of the four-layer "doing nothing is the default" safety stack:
deterministic pre-pass -> relevance filter -> classifier confidence ->
send allowlist inside the gateway.

This module is PURE: stdlib only (`hashlib`, `re`). No model calls, no
network, no secrets, nothing sends. For each inbound message it returns
whether the message must be DROPPED before anything downstream sees it,
or CONTINUEs into the pipeline.

Canonical message (dict):
    message_id (str), channel ("sms"|"email"), source ("close"|"ghl"),
    sender (str), body (str), timestamp (ISO 8601 str)

Rules (in order):

1. DUPLICATES -> drop, reason "duplicate", matched None.
   `idempotency_key(message)` = sha256(message_id) when the source supplies
   one; otherwise sha256("sender|body|timestamp"). Every key is recorded in
   a module-level in-memory seen-set (`reset()` clears it for tests).
   A message that arrives twice (webhook retry, redelivery) is dropped on
   the second arrival so the pipeline never processes the same inbound twice.

2. OPT-OUTS -> drop, reason "opt-out", matched=<canonical keyword>.
   Carrier-level opt-out keywords, case-insensitive, word-boundary aware.

KEY DECISIONS (documented because they are compliance-critical):

A) Bare keyword = opt-out. "STOP", "Stop.", "CANCEL!", "  Unsubscribe  "
   -> drop before any model sees the message. These are the unambiguous,
   carrier-defined opt-out signals; the message contains nothing else, so
   there is no alternative reading.

B) Embedded keyword = NOT an opt-out. "I need to cancel my appointment",
   "please cancel my call Tuesday", "when does the deal end" -> continue to
   the classifier, which has its own `cancellation` intent (shadow: log +
   escalate, never send). Rationale: the word "cancel" also expresses
   cancellation-of-appointment intent, and "end"/"quit"/"remove" appear in
   ordinary sentences. A deterministic layer that dropped on embedded
   matches would silently discard real customers with a false opt-out and
   starve the classifier of cancellations it must handle. A false drop is
   worse than letting a verbose opt-out ("stop emailing me, please") reach
   the classifier, because the downstream relevance filter and classifier
   are built to catch intent the pre-pass cannot prove. In short: the
   pre-pass only acts when it is certain.

C) Duplicates are checked BEFORE opt-outs. A retried STOP webhook must not
   double-process: the first delivery applies the opt-out (and any logging /
   suppression side effect downstream); the second delivery is the SAME event
   and must be dropped as a duplicate rather than re-handled as a new
   opt-out. Checking duplicates first gives exactly-once semantics for
   downstream side effects.

Opt-out keywords and their regexes (each compiled with re.IGNORECASE):

    keyword       bare-keyword pattern (body is normalized first)
    -------       ------------------------------------------------
    stop          r"^\\W*stop\\W*$"
    cancel        r"^\\W*cancel\\W*$"
    unsubscribe   r"^\\W*unsubscribe\\W*$"
    quit          r"^\\W*quit\\W*$"
    end           r"^\\W*end\\W*$"
    remove        r"^\\W*remove\\W*$"
    optout        r"^\\W*optout\\W*$"
    opt out       r"^\\W*opt\\ out\\W*$"

Normalization before matching: leading/trailing whitespace stripped, then
every run of whitespace/hyphens/underscores collapses to a single space, so
"OPT-OUT", "opt_out" and "opt out" all hit the same rule. Case-insensitivity
comes from re.IGNORECASE.

The ^...$ anchors plus \\W* are what make this a *bare* keyword match: any
other letter, digit, or word character anywhere in the message fails the
match, so "nonstop", "stopped", "I want to cancel", and "the deal ends" all
pass through to the classifier. Word-boundary awareness falls out of the
anchors: the keyword must stand alone. Short words ("end", "quit") are safe
here precisely because the anchors require the whole message to BE the
keyword -- no substring matching is ever performed.
"""

import hashlib
import re

__all__ = ["check", "idempotency_key", "reset", "OPT_OUT_KEYWORDS"]

#: Canonical opt-out keywords, in the form returned as `matched`.
OPT_OUT_KEYWORDS = (
    "stop",
    "cancel",
    "unsubscribe",
    "quit",
    "end",
    "remove",
    "optout",
    "opt out",
)

#: Normalization: runs of whitespace, hyphens, and underscores -> one space.
_NORMALIZE_RUNS = re.compile(r"[\s_-]+")

#: One compiled bare-keyword pattern per opt-out keyword.
_BARE_PATTERNS = tuple(
    (keyword, re.compile(r"^\W*" + re.escape(keyword) + r"\W*$", re.IGNORECASE))
    for keyword in OPT_OUT_KEYWORDS
)

#: In-memory seen-set of idempotency keys for duplicate detection.
_SEEN = set()


def reset() -> None:
    """Clear the in-memory seen-set (used between tests)."""
    _SEEN.clear()


def idempotency_key(message: dict) -> str:
    """Stable idempotency key for a canonical message.

    sha256 of `message_id` when the source supplies one, otherwise sha256 of
    the composite "sender|body|timestamp". Returned as a hex digest.
    """
    message_id = message.get("message_id")
    if message_id:
        basis = str(message_id)
    else:
        basis = "|".join(str(message.get(k) or "") for k in ("sender", "body", "timestamp"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _match_opt_out(body) -> "str | None":
    """Return the canonical opt-out keyword if body is a bare keyword, else None."""
    normalized = _NORMALIZE_RUNS.sub(" ", (body or "").strip())
    for keyword, pattern in _BARE_PATTERNS:
        if pattern.match(normalized):
            return keyword
    return None


def check(message: dict) -> dict:
    """Run the deterministic pre-pass on a canonical message.

    Returns {"action": "continue"|"drop", "reason": str, "matched": str|None}.
    "drop" means the message must not reach the relevance filter, classifier,
    or any model. The key is recorded in the seen-set even for dropped
    messages, so webhook retries are deduplicated.
    """
    key = idempotency_key(message)
    if key in _SEEN:
        return {"action": "drop", "reason": "duplicate", "matched": None}
    _SEEN.add(key)

    matched = _match_opt_out(message.get("body"))
    if matched is not None:
        return {"action": "drop", "reason": "opt-out", "matched": matched}

    return {"action": "continue", "reason": "no prepass rule matched", "matched": None}
