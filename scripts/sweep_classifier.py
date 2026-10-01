#!/usr/bin/env python3
"""Sweep the classifier over mined Close records.

Default mode is the offline keyword baseline. With --llm, the script injects
the same Anthropic-backed llm_fn shape the service uses, which makes network
calls and requires ANTHROPIC_API_KEY. The input/output files may contain PII;
do not commit generated JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IN = ROOT / "eval" / "fixtures" / "golden_mined.json"
DEFAULT_OUT = ROOT / "eval" / "results" / "sweep.json"
LABELS_PATH = ROOT / "eval" / "labels.json"
DEFAULT_MODEL = "claude-haiku-4-5"
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


def load_classifier():
    agent_src = ROOT / "agent-service" / "src"
    sys.path.insert(0, str(agent_src))
    from agent_service.classifier import classify

    return classify


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def make_anthropic_llm_fn(model: str, delay_s: float = 0.0):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    def _call(prompt: str) -> str:
        if delay_s:
            time.sleep(delay_s)
        resp = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        )

    return _call


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit(f"input must contain a messages array: {path}")
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        message_id = str(row.get("message_id") or f"row-{i}")
        if message_id in seen:
            continue
        seen.add(message_id)
        deduped.append(row)
    return deduped


def load_labels(path: Path = LABELS_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"labels file must be an object: {path}")
    labels: dict[str, dict[str, str]] = {}
    for message_id, label in data.items():
        if not isinstance(label, dict):
            continue
        true_intent = str(label.get("true_intent") or "").strip()
        if not true_intent:
            continue
        labels[str(message_id)] = {
            "true_intent": true_intent,
            "notes": str(label.get("notes") or ""),
        }
    return labels


def merge_labels(records: list[dict[str, Any]], labels: dict[str, dict[str, str]]) -> None:
    for row in records:
        label = labels.get(str(row.get("message_id") or ""))
        if not label:
            continue
        row["true_intent"] = label["true_intent"]
        row["notes"] = label.get("notes", "")


def canonical(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": row.get("message_id", ""),
        "channel": row.get("channel", "sms"),
        "source": row.get("source", "close"),
        "sender": row.get("sender", ""),
        "body": row.get("body", ""),
        "timestamp": row.get("timestamp", ""),
    }


def snippet(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def pct(count: int, total: int) -> str:
    return f"{(count / total * 100):.1f}%" if total else "0.0%"


def confidence_bucket(conf: float) -> str:
    if conf < 0.5:
        return "<0.5"
    if conf < 0.7:
        return "0.5-0.7"
    if conf < 0.9:
        return "0.7-0.9"
    return ">=0.9"


def run_sweep(records: list[dict[str, Any]], llm_fn=None) -> list[dict[str, Any]]:
    classify = load_classifier()
    results = []
    for row in records:
        message = canonical(row)
        decision = classify(message, llm_fn=llm_fn) or {}
        confidence = float(decision.get("confidence") or 0.0)
        results.append(
            {
                "message_id": message["message_id"],
                "channel": message["channel"],
                "source": message["source"],
                "sender": message["sender"],
                "body": message["body"],
                "timestamp": message["timestamp"],
                "predicted_intent": decision.get("intent") or "<none>",
                "confidence": confidence,
                "reasoning": decision.get("reasoning") or "",
                "true_intent": row.get("true_intent"),
                "true_intent_suggestion": row.get("true_intent_suggestion"),
                "setter_sent_link_after": bool(row.get("setter_sent_link_after")),
            }
        )
    return results


def format_distribution(results: list[dict[str, Any]]) -> list[str]:
    total = len(results)
    counts = Counter(r["predicted_intent"] for r in results)
    by_intent: dict[str, list[float]] = defaultdict(list)
    for r in results:
        by_intent[r["predicted_intent"]].append(r["confidence"])

    lines = ["Predicted intent distribution:"]
    all_conf = [r["confidence"] for r in results]
    lines.append(f"  overall avg confidence: {mean(all_conf):.3f}" if all_conf else "  overall avg confidence: n/a")
    for intent, count in counts.most_common():
        avg = mean(by_intent[intent]) if by_intent[intent] else 0.0
        lines.append(f"  {intent:17s} {count:4d}  {pct(count, total):>6s}  avg_conf={avg:.3f}")
    return lines


def format_histogram(results: list[dict[str, Any]]) -> list[str]:
    order = ("<0.5", "0.5-0.7", "0.7-0.9", ">=0.9")
    counts = Counter(confidence_bucket(r["confidence"]) for r in results)
    return ["Confidence histogram:"] + [
        f"  {bucket:7s} {counts[bucket]:4d}  {pct(counts[bucket], len(results)):>6s}"
        for bucket in order
    ]


def format_manual_accuracy(results: list[dict[str, Any]]) -> list[str]:
    labeled = [r for r in results if r.get("true_intent")]
    lines = ["Manual true_intent accuracy:"]
    if not labeled:
        lines.append("  no records with true_intent yet")
        return lines
    correct = sum(1 for r in labeled if r["predicted_intent"] == r["true_intent"])
    lines.append(f"  {correct}/{len(labeled)} correct ({pct(correct, len(labeled))})")
    for r in labeled:
        lines.append(
            "  - "
            f"body={snippet(r['body'])!r} "
            f"pred={r['predicted_intent']} true={r['true_intent']} "
            f"conf={r['confidence']:.2f}"
        )
    return lines


def format_disagreements(results: list[dict[str, Any]]) -> list[str]:
    rows = [
        r for r in results
        if r.get("true_intent_suggestion")
        and r["predicted_intent"] != r["true_intent_suggestion"]
    ]
    rows.sort(key=lambda r: r["confidence"], reverse=True)
    lines = ["Top disagreements vs true_intent_suggestion:"]
    if not rows:
        lines.append("  none")
        return lines
    for r in rows[:15]:
        lines.append(
            "  - "
            f"body={snippet(r['body'])!r} "
            f"pred={r['predicted_intent']} "
            f"suggestion={r['true_intent_suggestion']} "
            f"conf={r['confidence']:.2f}"
        )
    return lines


def format_setter_link_crosstab(results: list[dict[str, Any]]) -> list[str]:
    linked = [r for r in results if r.get("setter_sent_link_after")]
    counts = Counter(r["predicted_intent"] for r in linked)
    lines = ["Classifier predictions where setter_sent_link_after=true:"]
    if not linked:
        lines.append("  none")
        return lines
    for intent, count in counts.most_common():
        lines.append(f"  {intent:17s} {count:4d}  {pct(count, len(linked)):>6s}")
    return lines


def print_summary(results: list[dict[str, Any]]) -> None:
    print(f"Records swept: {len(results)}")
    sections = [
        format_distribution(results),
        format_histogram(results),
        format_manual_accuracy(results),
        format_disagreements(results),
        format_setter_link_crosstab(results),
    ]
    for section in sections:
        print()
        print("\n".join(section))


def save_results(path: Path, input_path: Path, results: list[dict[str, Any]], classifier_mode: str, model: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "input": str(input_path),
                "classifier_mode": classifier_mode,
                "model": model,
                "deduped_count": len(results),
                "results": results,
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", default=str(DEFAULT_IN))
    parser.add_argument("--out", dest="out_path", default=str(DEFAULT_OUT))
    parser.add_argument("--llm", action="store_true", help="Use Anthropic LLM classifier path instead of keyword baseline.")
    parser.add_argument("--model", default=None, help="Anthropic model for --llm (default: AGENT_LLM_MODEL or claude-haiku-4-5).")
    parser.add_argument("--env-file", default=str(ROOT / "agent-service" / ".env"))
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep between LLM calls.")
    args = parser.parse_args(argv)

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    records = load_records(in_path)
    merge_labels(records, load_labels())
    llm_fn = None
    classifier_mode = "keyword"
    model = None
    if args.llm:
        load_env_file(Path(args.env_file))
        model = args.model or os.environ.get("AGENT_LLM_MODEL") or DEFAULT_MODEL
        llm_fn = make_anthropic_llm_fn(model, delay_s=args.delay)
        classifier_mode = "llm"
    results = run_sweep(records, llm_fn=llm_fn)
    save_results(out_path, in_path, results, classifier_mode, model)
    print(f"Classifier mode: {classifier_mode}" + (f" ({model})" if model else ""))
    print_summary(results)
    print()
    print(f"Saved full local results to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
