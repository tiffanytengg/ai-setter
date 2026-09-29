"""Tests for agent_service.prepass (stdlib unittest only).

Run: cd agent-service/src && python -m agent_service.test_prepass
"""

import hashlib
import sys
import unittest

from agent_service.prepass import OPT_OUT_KEYWORDS, check, idempotency_key, reset


def make_message(body, message_id="test-1", sender="+15551234567",
                 timestamp="2026-09-27T12:00:00Z", **overrides):
    """Build a canonical message dict."""
    message = {
        "message_id": message_id,
        "channel": "sms",
        "source": "close",
        "sender": sender,
        "body": body,
        "timestamp": timestamp,
    }
    message.update(overrides)
    return message


class TestOptOutKeywords(unittest.TestCase):
    def setUp(self):
        reset()

    def test_each_keyword_drops_in_various_cases(self):
        cases = [
            ("stop", "stop"),
            ("STOP", "stop"),
            ("STOP!", "stop"),
            ("  stop  ", "stop"),
            ("Stop.", "stop"),
            ("STOP!!!", "stop"),
            ("cancel", "cancel"),
            ("CANCEL", "cancel"),
            ("CANCEL!", "cancel"),
            ("Unsubscribe", "unsubscribe"),
            ("  Unsubscribe  ", "unsubscribe"),
            ("UNSUBSCRIBE.", "unsubscribe"),
            ("quit", "quit"),
            ("QUIT", "quit"),
            ("end", "end"),
            ("END", "end"),
            ("remove", "remove"),
            ("REMOVE!", "remove"),
            ("optout", "optout"),
            ("OPTOUT", "optout"),
            ("opt out", "opt out"),
            ("OPT OUT", "opt out"),
            ("Opt-Out", "opt out"),   # hyphen normalizes to a space
            ("opt_out", "opt out"),   # underscore normalizes to a space
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                message = make_message(body, message_id="m-" + body)
                result = check(message)
                self.assertEqual(result["action"], "drop", body)
                self.assertEqual(result["reason"], "opt-out", body)
                self.assertEqual(result["matched"], expected, body)

    def test_embedded_keywords_continue(self):
        bodies = [
            "I need to cancel my appointment",
            "please cancel my call Tuesday",
            "I want to cancel",
            "stop emailing me about this",
            "when does the deal end",
            "the deal ends tomorrow",
            "I quit my job yesterday",
            "please remove me from the schedule",
            "nonstop",
            "stopped",
            "I have been unsubscribed already",
            "",          # empty body
            "Hello, is this a good time to talk?",
        ]
        for i, body in enumerate(bodies):
            with self.subTest(body=body):
                result = check(make_message(body, message_id=f"emb-{i}"))
                self.assertEqual(result["action"], "continue", body)
                self.assertIsNone(result["matched"], body)

    def test_all_keywords_documented(self):
        self.assertEqual(
            set(OPT_OUT_KEYWORDS),
            {"stop", "cancel", "unsubscribe", "quit", "end", "remove",
             "optout", "opt out"},
        )


class TestDuplicates(unittest.TestCase):
    def setUp(self):
        reset()

    def test_second_delivery_is_duplicate(self):
        message = make_message("Hi, I want to book a call", message_id="dup-1")
        first = check(message)
        self.assertEqual(first["action"], "continue")
        second = check(message)
        self.assertEqual(second, {"action": "drop", "reason": "duplicate",
                                  "matched": None})

    def test_duplicate_checked_before_opt_out(self):
        # A retried STOP webhook must be dropped as a duplicate, not
        # re-handled as a new opt-out: duplicates win.
        message = make_message("STOP", message_id="stop-1")
        first = check(message)
        self.assertEqual(first["reason"], "opt-out")
        second = check(message)
        self.assertEqual(second["reason"], "duplicate")
        self.assertIsNone(second["matched"])

    def test_reset_clears_seen_set(self):
        message = make_message("hello", message_id="r-1")
        self.assertEqual(check(message)["action"], "continue")
        self.assertEqual(check(message)["reason"], "duplicate")
        reset()
        self.assertEqual(check(message)["action"], "continue")

    def test_distinct_messages_are_not_duplicates(self):
        self.assertEqual(check(make_message("hi", message_id="a"))["action"],
                         "continue")
        self.assertEqual(check(make_message("hi", message_id="b"))["action"],
                         "continue")


class TestIdempotencyKey(unittest.TestCase):
    def test_stability(self):
        message = make_message("hello", message_id="stable-1")
        self.assertEqual(idempotency_key(message), idempotency_key(message))

    def test_uses_message_id_when_present(self):
        key = idempotency_key(make_message("hello", message_id="abc-123"))
        self.assertEqual(key, hashlib.sha256(b"abc-123").hexdigest())

    def test_falls_back_to_sender_body_timestamp(self):
        message = make_message("hello", message_id=None,
                               sender="+15550001111", timestamp="2026-09-27T12:00:00Z")
        expected = hashlib.sha256(
            "+15550001111|hello|2026-09-27T12:00:00Z".encode("utf-8")).hexdigest()
        self.assertEqual(idempotency_key(message), expected)

    def test_fallback_differs_on_body_change(self):
        base = dict(message_id=None, sender="+15550001111",
                    timestamp="2026-09-27T12:00:00Z")
        k1 = idempotency_key(make_message("hi", **base))
        k2 = idempotency_key(make_message("hello", **base))
        self.assertNotEqual(k1, k2)

    def test_missing_message_id_key_matches_redelivery(self):
        base = dict(message_id=None, sender="+15550001111",
                    timestamp="2026-09-27T12:00:00Z")
        reset()
        self.assertEqual(check(make_message("STOP", **base))["reason"], "opt-out")
        # Same content redelivered without message_id -> duplicate, not opt-out.
        self.assertEqual(check(make_message("STOP", **base))["reason"], "duplicate")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"\nPASS SUMMARY: {result.testsRun} tests run, "
          f"{passed} passed, {len(result.failures)} failed, "
          f"{len(result.errors)} errors "
          f"({'ALL PASSED' if result.wasSuccessful() else 'FAILURES PRESENT'})")
    sys.exit(0 if result.wasSuccessful() else 1)
