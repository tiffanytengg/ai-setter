"""Eval harness for the AI Setter classifier.

Runs ``agent_service.classifier.classify`` over every message in a golden-set
JSON file and reports overall accuracy, per-intent precision/recall/F1, and a
confusion summary. This is a measurement tool, not a gate: it always exits 0
and prints a PASS/FAIL line against an accuracy target (default 80%).

The harness imports the classifier via ``sys.path`` pointed at
``agent-service/src``::

    from agent_service.classifier import classify

and calls ``classify(message)`` with ``llm_fn`` left at its default (None),
which uses the classifier's offline keyword baseline. The harness itself makes
no network calls and needs no API keys.

Only the six canonical message fields are passed to the classifier -- the
``true_intent`` label and ``notes`` are stripped first so the label can never
leak into the prediction.

Golden-set JSON schema
----------------------
::

    {
      "version": "1",
      "description": "human-readable notes, e.g. source and labeling method",
      "messages": [
        {
          "message_id": "syn-001",
          "channel": "sms",                 # "sms" | "email"
          "source": "close",                # "close" | "ghl"
          "sender": "+15550101111",
          "body": "message text",
          "timestamp": "2026-09-27T10:00:00Z",  # ISO 8601
          "true_intent": "scheduling",      # one of the 8 taxonomy intents
          "notes": "optional annotator note"
        }
      ]
    }

Intent taxonomy: scheduling, rescheduling, coaching_checkin, refund,
cancellation, technical, opt_out, other.

Usage::

    cd ~/workspace/ai-setter && python eval/harness.py --golden eval/fixtures/golden_synthetic.json
"""

import argparse
import json
import os
import sys

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

CANONICAL_FIELDS = ("message_id", "channel", "source", "sender", "body", "timestamp")

DEFAULT_TARGET = 0.80


class GoldenSetError(Exception):
    """The golden-set file is missing, malformed, or fails validation."""


def load_golden_set(path):
    """Load and validate a golden-set JSON file.

    Returns ``(meta, records)`` where ``meta`` is a dict with
    ``version``/``description``/``path`` and ``records`` is the list of
    message dicts. Raises :class:`GoldenSetError` on any problem.
    """
    if not os.path.isfile(path):
        raise GoldenSetError("golden-set file not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise GoldenSetError("golden-set is not valid JSON: %s" % exc)
    if not isinstance(data, dict):
        raise GoldenSetError("golden-set top level must be a JSON object")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GoldenSetError("golden-set must contain a non-empty 'messages' list")
    for i, record in enumerate(messages):
        if not isinstance(record, dict):
            raise GoldenSetError("messages[%d] must be an object" % i)
        for field in CANONICAL_FIELDS:
            if field not in record:
                raise GoldenSetError(
                    "messages[%d] (%s) is missing required field %r"
                    % (i, record.get("message_id", "?"), field)
                )
        true_intent = record.get("true_intent")
        if true_intent not in INTENTS:
            raise GoldenSetError(
                "messages[%d] (%s) has invalid true_intent %r; must be one of: %s"
                % (i, record.get("message_id", "?"), true_intent, ", ".join(INTENTS))
            )
    meta = {
        "version": data.get("version", "?"),
        "description": data.get("description", ""),
        "path": path,
    }
    return meta, messages


def to_canonical_message(record):
    """Strip labels/notes: the classifier only ever sees the canonical message."""
    return {field: record[field] for field in CANONICAL_FIELDS}


def _coerce_confidence(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run_eval(records, classify_fn):
    """Run ``classify_fn`` over every record; return per-message result dicts."""
    results = []
    for record in records:
        message = to_canonical_message(record)
        decision = classify_fn(message) or {}
        predicted = decision.get("intent") or "<none>"
        results.append(
            {
                "message_id": record["message_id"],
                "true_intent": record["true_intent"],
                "predicted_intent": predicted,
                "confidence": _coerce_confidence(decision.get("confidence")),
                "reasoning": decision.get("reasoning") or "",
                "body": record["body"],
                "correct": predicted == record["true_intent"],
            }
        )
    return results


def compute_metrics(results):
    """Compute accuracy, per-intent P/R/F1, and the confusion counts."""
    n = len(results)
    correct = sum(1 for r in results if r["correct"])
    per_intent = {}
    for intent in INTENTS:
        tp = sum(
            1
            for r in results
            if r["true_intent"] == intent and r["predicted_intent"] == intent
        )
        fp = sum(
            1
            for r in results
            if r["true_intent"] != intent and r["predicted_intent"] == intent
        )
        fn = sum(
            1
            for r in results
            if r["true_intent"] == intent and r["predicted_intent"] != intent
        )
        support = tp + fn
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )
        per_intent[intent] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }
    confusion = {}
    for r in results:
        bucket = confusion.setdefault(r["true_intent"], {})
        bucket[r["predicted_intent"]] = bucket.get(r["predicted_intent"], 0) + 1
    return {
        "n": n,
        "correct": correct,
        "accuracy": (correct / n) if n else 0.0,
        "per_intent": per_intent,
        "confusion": confusion,
    }


def _truncate(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def format_report(meta, metrics, results, target):
    """Render the full text report."""
    lines = []
    lines.append("AI Setter classifier eval")
    lines.append("=" * 60)
    lines.append("Golden set : %s" % meta["path"])
    lines.append("Version    : %s" % meta["version"])
    if meta["description"]:
        lines.append("Notes      : %s" % _truncate(meta["description"], 80))
    lines.append("Messages   : %d" % metrics["n"])
    lines.append("")
    lines.append(
        "Overall accuracy: %.1f%% (%d/%d)"
        % (metrics["accuracy"] * 100, metrics["correct"], metrics["n"])
    )
    lines.append("")
    lines.append("Per-intent metrics:")
    lines.append(
        "  %-16s %6s %6s %6s %7s" % ("intent", "prec", "recall", "f1", "support")
    )
    for intent in INTENTS:
        m = metrics["per_intent"][intent]
        lines.append(
            "  %-16s %6.2f %6.2f %6.2f %7d"
            % (intent, m["precision"], m["recall"], m["f1"], m["support"])
        )
    lines.append("")
    lines.append("Confusion (true -> predicted; non-zero cells only):")
    for true_intent in INTENTS:
        bucket = metrics["confusion"].get(true_intent, {})
        if not bucket:
            continue
        cells = ", ".join(
            "%s=%d" % (pred, count)
            for pred, count in sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append("  %-16s %s" % (true_intent, cells))
    # Predicted labels outside the taxonomy get their own mention.
    stray = sorted(
        {
            pred
            for bucket in metrics["confusion"].values()
            for pred in bucket
            if pred not in INTENTS
        }
    )
    if stray:
        lines.append(
            "  NOTE: classifier predicted label(s) outside the taxonomy: %s"
            % ", ".join(stray)
        )
    misses = [r for r in results if not r["correct"]]
    lines.append("")
    lines.append("Misses (%d):" % len(misses))
    for r in misses:
        lines.append(
            "  [%s] true=%s predicted=%s conf=%.2f"
            % (
                r["message_id"],
                r["true_intent"],
                r["predicted_intent"],
                r["confidence"],
            )
        )
        lines.append("    body: %s" % _truncate(r["body"], 100))
        if r["reasoning"]:
            lines.append("    reasoning: %s" % _truncate(r["reasoning"], 160))
    lines.append("")
    lines.append("Target: accuracy >= %.1f%%" % (target * 100))
    if metrics["accuracy"] >= target:
        lines.append(
            "RESULT: PASS -- accuracy %.1f%% (%d/%d) meets the %.1f%% target"
            % (
                metrics["accuracy"] * 100,
                metrics["correct"],
                metrics["n"],
                target * 100,
            )
        )
    else:
        lines.append(
            "RESULT: FAIL -- accuracy %.1f%% (%d/%d) is below the %.1f%% target"
            % (
                metrics["accuracy"] * 100,
                metrics["correct"],
                metrics["n"],
                target * 100,
            )
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the AI Setter classifier over a golden set and report accuracy."
    )
    parser.add_argument(
        "--golden", required=True, help="Path to the golden-set JSON file."
    )
    parser.add_argument(
        "--target",
        type=float,
        default=DEFAULT_TARGET,
        help="Accuracy target for the PASS/FAIL line (default: 0.80).",
    )
    args = parser.parse_args(argv)

    # Import the classifier exactly the way the shared contract specifies:
    # sys.path pointed at agent-service/src.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agent_src = os.path.join(repo_root, "agent-service", "src")
    sys.path.insert(0, agent_src)
    try:
        from agent_service.classifier import classify
    except ImportError as exc:
        print("ERROR: could not import agent_service.classifier: %s" % exc)
        print("The classifier component is not implemented yet; nothing to measure.")
        print("RESULT: ERROR -- no measurement (classifier unavailable)")
        return 0

    try:
        meta, records = load_golden_set(args.golden)
    except GoldenSetError as exc:
        print("ERROR: %s" % exc)
        print("RESULT: ERROR -- no measurement (bad golden set)")
        return 0

    try:
        results = run_eval(records, classify)
    except NotImplementedError:
        print(
            "ERROR: agent_service.classifier.classify is not implemented yet "
            "(raised NotImplementedError); nothing to measure."
        )
        print("RESULT: ERROR -- no measurement (classifier unavailable)")
        return 0

    metrics = compute_metrics(results)
    print(format_report(meta, metrics, results, args.target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
