"""Track B smoke tests (stdlib unittest only).

Run: cd ~/workspace/ai-setter && python3 tests/test_trackb_smoke.py

Architecture (flipped 2026-09-28, client requirement): Close is READ-ONLY;
the live send path is ghl-gateway.send_sms (gated). close-gateway has NO
send tool — nothing to misconfigure.

Covers:
  1. close-gateway imports, exposes only read tools (get_message, get_lead,
     render_template), and has NO send tools / no send config.
  2. close get_lead/get_message with no CLOSE_API_KEY -> honest
     not-configured error.
  3. render_template with no link source -> "" (escalate, never invent).
  4. ghl-gateway exposes get_contact, get_conversation, AND send_sms.
  5. ghl send_sms refuses when SEND_ENABLED unset/false; refuses when the
     allowlist is empty; refuses when the recipient is not allowlisted.
     Nothing sends by default.
  6. ghl get_contact with no creds -> honest not-configured error.
  7. Agent-service pipeline on a canonical fixture message: response carries
     intent/mode/etc.; a synthetic reschedule message runs the full path to
     mode "active" with send_tool == "ghl.send_sms" (dry-run; nothing sends).
  8. n8n/wf-01-thin.json is valid JSON; the agent-call node points at
     /webhook/message with the X-Agent-Token header; the send stub node is
     "Call ghl-gateway (STUB)".
  9. Auth: wrong token -> 401; missing secret without AGENT_ALLOW_NOAUTH -> 503.

No network, no secrets, no sends. Env is scrubbed/restored per test.
"""

import asyncio
import inspect
import json
import os
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent-service", "src"))
sys.path.insert(0, os.path.join(ROOT, "mcp-servers", "close-gateway", "src"))
sys.path.insert(0, os.path.join(ROOT, "mcp-servers", "ghl-gateway", "src"))

import close_gateway.server as close_server          # noqa: E402
import ghl_gateway.server as ghl_server              # noqa: E402
from agent_service import app as app_mod              # noqa: E402
from agent_service import prepass                     # noqa: E402
from agent_service import execute as execute_mod       # noqa: E402
from agent_service.gateway_client import (             # noqa: E402
    GatewayError, call_tool_sync, close_gateway, ghl_gateway)

SCRUB = ("CLOSE_API_KEY", "SEND_ENABLED", "SEND_ALLOWLIST",
         "BOOKING_LINK_MAP", "GHL_PRIVATE_TOKEN", "GHL_LOCATION_ID",
         "AGENT_SHARED_SECRET", "AGENT_ALLOW_NOAUTH", "DATABASE_URL",
         "CLOSE_GATEWAY_URL", "GHL_GATEWAY_URL")


@contextmanager
def scrubbed_env(**overrides):
    saved = {k: os.environ.get(k) for k in SCRUB}
    for k in SCRUB:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in overrides.items() if v is not None})
    try:
        yield
    finally:
        for k in SCRUB:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def list_tools(server_module):
    tools = server_module.mcp.list_tools()
    if inspect.isawaitable(tools):
        tools = asyncio.run(tools)
    return {t.name for t in tools}


def canonical_from_close_fixture():
    with open(os.path.join(ROOT, "n8n", "fixtures", "close_sms.json")) as f:
        raw = json.load(f)
    data = raw["data"]
    return {
        # Documented canonical shape: {message_id, channel, source,
        # sender, body, timestamp}
        "message_id": data["id"],
        "channel": "sms" if str(data.get("type", "")).upper() == "SMS" else "email",
        "source": "close",
        "sender": data.get("sender", ""),
        "body": data.get("text", ""),
        "timestamp": data.get("date_created", ""),
    }


class TestCloseGatewayReadOnly(unittest.TestCase):
    def test_only_read_tools_exposed(self):
        tools = list_tools(close_server)
        self.assertEqual(
            tools,
            {"get_message", "get_lead", "render_template"},
        )

    def test_no_send_tool_exists(self):
        tools = list_tools(close_server)
        for name in ("send_sms", "send_message", "send_email"):
            self.assertNotIn(name, tools, f"close-gateway must not expose {name}")
            self.assertFalse(
                hasattr(close_server, name),
                f"close-gateway module must not define {name}",
            )

    def test_no_send_config_helpers(self):
        # The send gates belong to ghl-gateway only.
        self.assertFalse(hasattr(close_server, "_send_enabled"))
        self.assertFalse(hasattr(close_server, "_allowlist"))
        self.assertFalse(hasattr(close_server, "_send_gate"))

    def test_get_lead_not_configured(self):
        with scrubbed_env():
            out = close_server.get_lead("lead_test123")
        self.assertEqual(out.get("error"), "not_configured")
        self.assertIn("CLOSE_API_KEY", out.get("detail", ""))

    def test_get_message_not_configured(self):
        with scrubbed_env():
            out = close_server.get_message("acti_test123")
        self.assertEqual(out.get("error"), "not_configured")

    def test_render_template_unresolvable_returns_empty(self):
        with scrubbed_env():
            out = close_server.render_template("booking", "lead_test123")
        self.assertEqual(out, "")

    def test_render_template_uses_static_map(self):
        with scrubbed_env(BOOKING_LINK_MAP='{"default": "https://example.com/book"}'):
            out = close_server.render_template("booking", "lead_test123")
        self.assertIn("https://example.com/book", out)

    def test_render_template_rejects_unknown_template(self):
        with scrubbed_env():
            self.assertEqual(close_server.render_template("nope", "lead_x"), "")


class TestGHLGateway(unittest.TestCase):
    def test_tools_exposed(self):
        tools = list_tools(ghl_server)
        self.assertEqual(
            tools,
            {"get_contact", "get_conversation", "send_sms"},
        )

    def test_send_sms_refuses_when_disabled(self):
        with scrubbed_env():
            out = ghl_server.send_sms("contact_test123", "hello")
        self.assertFalse(out["sent"])
        self.assertIn("disabled", out["reason"])

    def test_send_sms_refuses_when_explicitly_false(self):
        with scrubbed_env(SEND_ENABLED="false", SEND_ALLOWLIST="contact_test123"):
            out = ghl_server.send_sms("contact_test123", "hello")
        self.assertFalse(out["sent"])
        self.assertIn("disabled", out["reason"])

    def test_send_sms_refuses_when_not_allowlisted(self):
        with scrubbed_env(SEND_ENABLED="true", SEND_ALLOWLIST="contact_other"):
            out = ghl_server.send_sms("contact_test123", "hello")
        self.assertFalse(out["sent"])
        self.assertIn("allowlist", out["reason"].lower())

    def test_send_sms_refuses_when_allowlist_empty(self):
        with scrubbed_env(SEND_ENABLED="true", SEND_ALLOWLIST=""):
            out = ghl_server.send_sms("contact_test123", "hello")
        self.assertFalse(out["sent"])
        self.assertIn("allowlist", out["reason"].lower())

    def test_get_contact_not_configured(self):
        with scrubbed_env():
            out = ghl_server.get_contact("contact_test123")
        self.assertEqual(out.get("error"), "not_configured")

    def test_get_conversation_not_configured(self):
        with scrubbed_env():
            out = ghl_server.get_conversation("contact_test123")
        self.assertEqual(out.get("error"), "not_configured")


class TestAgentServicePipeline(unittest.TestCase):
    def setUp(self):
        prepass.reset()

    def test_fixture_message_returns_full_shape(self):
        msg = canonical_from_close_fixture()
        self.assertEqual(msg["channel"], "sms")
        with scrubbed_env():
            out = app_mod.handle_message_dict(msg)
        for field in ("message_id", "intent", "confidence", "reasoning",
                      "skill", "mode", "action_plan", "would_send", "send_tool"):
            self.assertIn(field, out, f"missing field {field}")
        self.assertEqual(out["message_id"], msg["message_id"])
        self.assertIn(out["mode"], ("active", "shadow", "none"))

    def test_reschedule_message_goes_active_dry_run(self):
        msg = {
            "message_id": "smoke-resched-1",
            "channel": "sms",
            "source": "ghl",
            "sender": "+15551234567",
            "body": "Can we move Tuesday to Thursday?",
            "timestamp": "2026-09-28T19:00:00Z",
        }
        with scrubbed_env():
            out = app_mod.handle_message_dict(msg)
        self.assertEqual(out["intent"], "rescheduling")
        self.assertEqual(out["mode"], "active")
        self.assertTrue(out["would_send"])
        self.assertEqual(out["send_tool"], "ghl.send_sms")
        self.assertEqual(out["skill"], "send-reschedule-link")

    def test_opt_out_resolves_early(self):
        msg = {
            "message_id": "smoke-optout-1",
            "channel": "sms",
            "source": "ghl",
            "sender": "+15551234567",
            "body": "STOP",
            "timestamp": "2026-09-28T19:00:00Z",
        }
        with scrubbed_env():
            out = app_mod.handle_message_dict(msg)
        self.assertEqual(out["mode"], "none")
        self.assertEqual(out["intent"], "opt_out")
        self.assertFalse(out["would_send"])

    def test_irrelevant_message_goes_none(self):
        msg = {
            "message_id": "smoke-irrelevant-1",
            "channel": "sms",
            "source": "ghl",
            "sender": "+15551234567",
            "body": "Thanks for the new workout plan, feeling great this week!",
            "timestamp": "2026-09-28T19:00:00Z",
        }
        with scrubbed_env():
            out = app_mod.handle_message_dict(msg)
        self.assertEqual(out["mode"], "none")

    def test_bad_message_never_raises(self):
        with scrubbed_env():
            out = app_mod.handle_message_dict(None)
        self.assertEqual(out["mode"], "none")

    def test_http_end_to_end(self):
        from fastapi.testclient import TestClient

        with scrubbed_env(AGENT_ALLOW_NOAUTH="true"):
            client = TestClient(app_mod.app)
            self.assertEqual(client.get("/health").json(), {"ok": True})
            r = client.post("/webhook/message", json={
                "message_id": "smoke-http-1",
                "channel": "sms",
                "source": "ghl",
                "sender": "+15551234567",
                "body": "Can we move Tuesday to Thursday?",
                "timestamp": "2026-09-28T19:00:00Z",
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["intent"], "rescheduling")
        self.assertEqual(body["mode"], "active")


class TestExecutorWiring(unittest.TestCase):
    """Agent -> gateway MCP wiring. All offline: gateways unconfigured."""

    def test_execute_active_plan_offline_is_dry_run(self):
        planned = {"mode": "active", "would_send": True,
                   "skill": "send-reschedule-link"}
        with scrubbed_env():
            out = execute_mod.execute_plan(
                planned, {"sender": "+15551234567", "message_id": "w1"})
        self.assertFalse(out["executed"])
        self.assertFalse(out["sent"])
        self.assertIn("not configured", out["reason"])
        self.assertIsNone(out["send_result"])

    def test_execute_non_active_plan_skipped(self):
        for mode in ("none", "shadow"):
            planned = {"mode": mode, "would_send": False, "skill": None}
            with scrubbed_env():
                out = execute_mod.execute_plan(planned, {"sender": "x"})
            self.assertFalse(out["executed"])
            self.assertFalse(out["sent"])

    def test_execute_never_raises(self):
        with scrubbed_env():
            out = execute_mod.execute_plan(None, None)
        self.assertFalse(out["executed"])

    def test_unconfigured_gateway_client_raises_gateway_error(self):
        with scrubbed_env():
            self.assertFalse(close_gateway.configured)
            self.assertFalse(ghl_gateway.configured)
            with self.assertRaises(GatewayError):
                call_tool_sync(ghl_gateway, "send_sms",
                               {"contact_id": "x", "body": "y"})

    def test_pipeline_carries_execution_outcome(self):
        msg = {
            "message_id": "smoke-exec-1",
            "channel": "sms",
            "source": "ghl",
            "sender": "+15551234567",
            "body": "Can we move Tuesday to Thursday?",
            "timestamp": "2026-09-28T19:00:00Z",
        }
        with scrubbed_env():
            out = app_mod.handle_message_dict(msg)
        self.assertIn("execution", out)
        self.assertFalse(out["execution"]["executed"])
        self.assertFalse(out["execution"]["sent"])
        # the plan itself is unchanged by the offline executor
        self.assertTrue(out["would_send"])
        self.assertEqual(out["mode"], "active")


class TestAuth(unittest.TestCase):
    def _req(self, token=None):
        headers = {}
        if token is not None:
            headers["x-agent-token"] = token
        return SimpleNamespace(headers=headers)

    def test_wrong_token_401(self):
        from fastapi import HTTPException

        with scrubbed_env(AGENT_SHARED_SECRET="s3cret"):
            with self.assertRaises(HTTPException) as cm:
                app_mod._require_auth(self._req("wrong"))
        self.assertEqual(cm.exception.status_code, 401)

    def test_missing_secret_without_bypass_503(self):
        from fastapi import HTTPException

        with scrubbed_env():
            with self.assertRaises(HTTPException) as cm:
                app_mod._require_auth(self._req("anything"))
        self.assertEqual(cm.exception.status_code, 503)

    def test_correct_token_passes(self):
        with scrubbed_env(AGENT_SHARED_SECRET="s3cret"):
            app_mod._require_auth(self._req("s3cret"))  # must not raise


class TestN8nWorkflow(unittest.TestCase):
    def test_workflow_json_and_agent_call_node(self):
        path = os.path.join(ROOT, "n8n", "wf-01-thin.json")
        with open(path) as f:
            wf = json.load(f)  # raises if invalid JSON
        nodes = {n["name"]: n for n in wf["nodes"]}
        call = nodes["Call agent service"]
        params = call["parameters"]
        self.assertTrue(
            params["url"].endswith("/webhook/message"),
            f"agent call node points at {params['url']!r}, expected /webhook/message",
        )
        headers = params.get("headerParameters", {}).get("parameters", [])
        token_headers = [h for h in headers if h.get("name") == "X-Agent-Token"]
        self.assertTrue(token_headers, "X-Agent-Token header missing on agent call node")
        self.assertIn("AGENT_SHARED_SECRET", token_headers[0]["value"])
        # Everything else intact: normalize, dispatch on mode, stub, log.
        for name in ("Webhook", "Normalize", "Pre-pass note", "Dispatch",
                     "Call ghl-gateway (STUB)", "End (no action)", "Log",
                     "Sticky Note"):
            self.assertIn(name, nodes, f"node {name!r} missing after edit")
        self.assertNotIn("Call close-gateway (STUB)", nodes)

    def test_stub_node_is_noop_and_ghl_flavored(self):
        path = os.path.join(ROOT, "n8n", "wf-01-thin.json")
        with open(path) as f:
            wf = json.load(f)
        stub = next(n for n in wf["nodes"] if n["name"] == "Call ghl-gateway (STUB)")
        self.assertEqual(stub["type"], "n8n-nodes-base.noOp")
        self.assertIn("ghl-gateway.send_sms", stub["notes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
