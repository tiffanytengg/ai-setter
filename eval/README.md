# eval

Makes the classification loop measurable in seconds instead of hours. Every
classifier or prompt change gets run through `harness.py` before it ships.

## What the harness does

`harness.py` loads a labeled golden-set JSON file, runs
`agent_service.classifier.classify` over every message, and prints:

- overall accuracy (correct / total),
- per-intent precision, recall, F1, and support counts,
- a confusion summary (true → predicted, non-zero cells only),
- the individual misses with the classifier's reasoning, so bad calls are
  debuggable, not just countable,
- a `RESULT: PASS/FAIL` line against an accuracy target (default 80%).

It is a measurement tool, not a gate: it always exits 0. The classifier is
imported via `sys.path` pointed at `agent-service/src`
(`from agent_service.classifier import classify`) and called as
`classify(message)` with `llm_fn` left at its default (`None`), i.e. the
offline keyword baseline — no network calls, no API keys. Only the six
canonical message fields (`message_id`, `channel`, `source`, `sender`,
`body`, `timestamp`) are passed to the classifier; `true_intent` and `notes`
are stripped first so the label can never leak into the prediction.

## Run

```bash
cd ~/workspace/ai-setter && python eval/harness.py --golden eval/fixtures/golden_synthetic.json
```

Options: `--golden <path>` (required), `--target <float>` (accuracy target
for the PASS/FAIL line, default `0.80`).

## Golden-set schema

```json
{"version": "1", "description": "...", "messages": [
  {"message_id": "...", "channel": "sms", "source": "close", "sender": "+15551234567",
   "body": "...", "timestamp": "2026-09-27T10:00:00Z", "true_intent": "scheduling", "notes": "..."}]}
```

- `channel`: `"sms"` | `"email"`; `source`: `"close"` | `"ghl"`.
- `timestamp`: ISO 8601. `true_intent`: one of `scheduling`, `rescheduling`,
  `coaching_checkin`, `refund`, `cancellation`, `technical`, `opt_out`, `other`.
- `notes` is optional (annotator rationale). Extra fields are ignored.

`eval/fixtures/golden_synthetic.json` is a 24-message synthetic set (3 per
intent) for smoke-testing the harness. **Do not trust accuracy numbers
computed on it** — it exists so the harness loop works before real labels do.

## Adding real labeled data

1. **Mine SMS threads from Close Conversations via template-phrase search.**
   Find inbound messages that a setter answered with the booking or
   reschedule template — each one is a free `scheduling`/`rescheduling`
   label (the "answer key"). Everything else gets hand-labeled.
2. **Hand-label the tricky slice first.** 40–60 of the hardest real
   messages beat 1,000 easy ones: near-miss phrasing ("your booking link is
   broken" is `technical`, not `scheduling`), terse replies ("STOP",
   "cancel my subscription please"), and cross-intent messages.
3. **The 168 emails are the phase-2 set.** Phase 1 is texts; keep the email
   labels in a separate golden file (`eval/fixtures/golden_email_v1.json`)
   and run the identical pipeline with `channel: "email"`.
4. Save new sets under `eval/fixtures/` as `golden_<source>_v<n>.json` and
   note the labeling method in `description`. `weak_label.py` (stub) is the
   planned answer-key-mining + LLM-judge expansion path; report accuracy on
   the hand-labeled slice only — that's the honest number.
