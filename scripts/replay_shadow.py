"""Replay historical inbound messages against the live agent service in shadow.

Demo/rehearsal tool. Every message is POSTed to the agent service's
/webhook/message; the service classifies it, builds the full action plan,
logs the decision, and — with AGENT_FORCE_SHADOW=true set on the service —
takes no action. This script never talks to the gateways itself.

Safety:
- A preflight probe (synthetic scheduling message, fictional 555-01xx sender)
  must come back mode "shadow" with the AGENT_FORCE_SHADOW escalation note.
  If it does not, the batch is aborted before any historical message is sent.
- The batch also aborts immediately if any response reports sent=true.
- The token is read from agent-service/.env (AGENT_SHARED_SECRET) and is
  never printed.

Input: the first file found among --input / DEFAULT_INPUTS. Both shapes are
accepted: Close SMS activity objects (close-inbound.json: text, remote_phone,
date_created) and canonical/golden-set messages ({"messages": [...]}).
The input file and the results contain real phone numbers — keep them out of
git (scripts/output/ is gitignored).

Usage::

    python scripts/replay_shadow.py --id-prefix rehearsal-
"""

import argparse
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_URL = "https://agent-service-production-d493.up.railway.app/webhook/message"
DEFAULT_ENV = os.path.join(REPO, "agent-service", ".env")
DEFAULT_INPUTS = (
    os.path.join(REPO, "sample-messages.json"),
    os.path.expanduser("~/Desktop/sample-messages.json"),
    os.path.expanduser("~/Desktop/close-inbound.json"),  # 168 inbound Close SMS
    os.path.join(REPO, "eval", "fixtures", "golden_synthetic.json"),
)
FORCED_SHADOW_MARKER = "AGENT_FORCE_SHADOW"
SLEEP_S = 0.2


class AuthError(Exception):
    pass


def read_token(env_path):
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("AGENT_SHARED_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"AGENT_SHARED_SECRET not found in {env_path}")


def find_input(explicit):
    candidates = [explicit] if explicit else DEFAULT_INPUTS
    for path in candidates:
        if path and os.path.exists(path):
            return path
    raise SystemExit("no input file found; tried: " + ", ".join(candidates))


def load_messages(path):
    with open(path) as fh:
        data = json.load(fh)
    rows = data.get("messages", data.get("data", [])) if isinstance(data, dict) else data
    out = []
    for i, row in enumerate(rows):
        if "event" in row:  # Close webhook envelope
            row = row["event"].get("data", {})
        out.append({
            "message_id": row.get("message_id") or row.get("id") or f"row-{i}",
            # Force "sms": the pipeline is Phase 1 (SMS-only). The 168-row
            # sample was believed to be email-origin; close-inbound.json is in
            # fact Close inbound SMS, so this is a no-op there — but it keeps
            # email-origin rows (e.g. the phase-2 email set) from being gated
            # out to mode "none" by the SMS-only check.
            "channel": "sms",
            "source": row.get("source") if row.get("source") in ("close", "ghl") else "close",
            "sender": row.get("sender") or row.get("remote_phone") or "",
            "body": row.get("body") if row.get("body") is not None else (row.get("text") or ""),
            "timestamp": row.get("timestamp") or row.get("date_created") or "",
        })
    return out


def post(url, token, message, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(message).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Agent-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError("401 unauthorized — X-Agent-Token rejected. Check "
                            "AGENT_SHARED_SECRET in agent-service/.env matches Railway.")
        raise


def is_sent(result):
    return bool((result.get("execution") or {}).get("sent"))


def preflight(url, token, prefix):
    """Prove forced shadow is live before replaying real messages."""
    probe = {
        "message_id": f"{prefix}preflight-{int(time.time())}",
        "channel": "sms", "source": "close",
        "sender": "+15555550100",  # fictional (555-01xx), never allowlisted
        "body": "Hi, can I book a call to schedule my session for tomorrow?",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    result = post(url, token, probe)
    note = result.get("escalation_note") or ""
    if (result.get("mode") != "shadow" or result.get("would_send")
            or is_sent(result) or FORCED_SHADOW_MARKER not in note):
        raise SystemExit(
            "PREFLIGHT FAILED — forced shadow is NOT active on the service. "
            "Set AGENT_FORCE_SHADOW=true on Railway and redeploy. Aborting; no "
            "historical messages were sent.\nProbe result: "
            + json.dumps({k: result.get(k) for k in
                          ("intent", "confidence", "mode", "would_send", "escalation_note")}))
    return result


def example_key(r):
    note = r.get("escalation_note") or ""
    if r.get("mode") == "shadow" and FORCED_SHADOW_MARKER in note:
        return "forced_shadow (active skill, downgraded)"
    if r.get("mode") == "shadow":
        return "natural_shadow (refund/cancellation/technical)"
    if r.get("intent") == "opt_out":
        return "none (pre-pass opt-out)"
    return "none"


def summarize(results):
    ok = [r for r in results if "result" in r]
    res = [r["result"] for r in ok]
    confs = [r.get("confidence") for r in res if isinstance(r.get("confidence"), (int, float))]
    examples = {}
    for item in ok:
        examples.setdefault(example_key(item["result"]), item)
    return {
        "total": len(results),
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "intent_distribution": dict(collections.Counter(r.get("intent") for r in res).most_common()),
        "mode_distribution": dict(collections.Counter(r.get("mode") for r in res).most_common()),
        "average_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "would_send_true": sum(1 for r in res if r.get("would_send")),
        "sent_true": sum(1 for r in res if is_sent(r)),
        "examples": examples,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", help="messages JSON (default: first of DEFAULT_INPUTS found)")
    ap.add_argument("--id-prefix", default="rehearsal-")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--env-file", default=DEFAULT_ENV)
    ap.add_argument("--out", help="results JSON path (default: scripts/output/replay-<prefix>.json)")
    ap.add_argument("--limit", type=int, help="replay only the first N messages")
    args = ap.parse_args(argv)

    token = read_token(args.env_file)
    path = find_input(args.input)
    messages = load_messages(path)[: args.limit]
    out_path = args.out or os.path.join(
        REPO, "scripts", "output", f"replay-{args.id_prefix.rstrip('-') or 'run'}.json")
    print(f"input: {path} ({len(messages)} messages)  ->  {args.url}")

    try:
        probe = preflight(args.url, token, args.id_prefix)
    except AuthError as exc:
        raise SystemExit(f"ABORT: {exc}")
    print("preflight OK: forced shadow is active "
          f"(probe intent={probe.get('intent')} mode={probe.get('mode')})")

    results = []
    for i, msg in enumerate(messages):
        msg = dict(msg, message_id=f"{args.id_prefix}{msg['message_id']}")
        try:
            result = post(args.url, token, msg)
            results.append({"message": msg, "result": result})
            if is_sent(result):
                raise SystemExit(f"ABORT: message {msg['message_id']} reported sent=true")
        except AuthError as exc:
            raise SystemExit(f"ABORT at message {i}: {exc}")
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — log and continue the batch
            print(f"  [{i}] {msg['message_id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append({"message": msg, "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(SLEEP_S)

    summary = summarize(results)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"input": path, "url": args.url, "id_prefix": args.id_prefix,
                   "summary": summary, "results": results}, fh, indent=2)

    print(f"\n=== replay summary ({summary['ok']}/{summary['total']} ok, "
          f"{summary['errors']} errors) ===")
    print("intent distribution:", json.dumps(summary["intent_distribution"]))
    print("mode distribution:  ", json.dumps(summary["mode_distribution"]))
    print("average confidence: ", summary["average_confidence"])
    print("would_send=true:    ", summary["would_send_true"])
    print("sent=true:          ", summary["sent_true"], "(must be 0)")
    for label, item in summary["examples"].items():
        r = item["result"]
        print(f"\n--- example: {label} ---")
        print(json.dumps({"message_id": item["message"]["message_id"],
                          "body": item["message"]["body"],
                          **{k: r.get(k) for k in ("intent", "confidence", "reasoning", "skill",
                                                   "mode", "would_send", "send_tool",
                                                   "escalation_note", "action_plan",
                                                   "execution")}}, indent=2))
    print(f"\nresults written to {out_path}")
    return 0 if summary["sent_true"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
