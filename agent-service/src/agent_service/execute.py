"""Execute an approved skill plan through the MCP gateways.

Only mode=active plans with would_send=true reach the gateways, in order:

  1. close-gateway.render_template (read-only) -> rendered SMS text.
     An empty render means the booking link could not be resolved ->
     escalate, never send, never invent a link.
  2. ghl-gateway.send_sms(contact_id, body) -> the gated send.

Fail-closed at every step: unconfigured or unreachable gateways, empty
renders, and gateway refusals all produce "not executed / not sent" outcomes
with a reason. Nothing here can bypass the gateway's own gates
(SEND_ENABLED + allowlist, enforced inside ghl-gateway.send_sms).

This module never raises — it returns an outcome dict. Stdlib only at import
time; gateway I/O goes through gateway_client (lazy `mcp` import).
"""

from __future__ import annotations

from .gateway_client import GatewayError, call_tool_sync, close_gateway, ghl_gateway


def _template_for(skill: str | None) -> str:
    return "booking" if skill == "send-booking-link" else "reschedule"


def _recipient_for(message: dict) -> str:
    message = message or {}
    return message.get("contact_id") or message.get("sender", "")


def render_send_preview(planned: dict, message: dict) -> dict:
    """Render the exact SMS preview for a forced-shadow active plan.

    This is read-only: it calls close-gateway.render_template and never calls
    send_sms. The returned dict is safe to include in webhook responses and
    decision logs.
    """
    def _out(rendered, reason, body=None):
        return {
            "rendered": rendered,
            "reason": reason,
            "recipient": _recipient_for(message),
            "template": _template_for((planned or {}).get("skill")),
            "render_tool": "close.render_template",
            "send_tool": (planned or {}).get("send_tool_before_shadow")
            or (planned or {}).get("send_tool"),
            "body": body,
        }

    planned = planned or {}
    message = message or {}
    if not planned.get("shadowed_from_active"):
        return _out(False, "not a forced-shadow active plan")
    if not close_gateway.configured:
        return _out(False, "close-gateway not configured (CLOSE_GATEWAY_URL unset)")

    template = _template_for(planned.get("skill"))
    sender = message.get("sender", "")
    try:
        rendered = call_tool_sync(
            close_gateway, "render_template",
            {"template": template, "lead_id": sender, "booking_link": ""},
        )
    except GatewayError as exc:
        return _out(False, f"render failed: {exc}")
    if not isinstance(rendered, str) or not rendered.strip():
        return _out(False, "booking link unresolvable — escalated, never invent a link")
    return _out(True, "rendered via close-gateway.render_template", rendered.strip())


def execute_plan(planned: dict, message: dict) -> dict:
    """Attempt to execute an active send plan via the MCP gateways.

    Returns {"executed": bool, "sent": bool, "reason": str,
             "send_result": dict | None}. Never raises.
    """
    def _out(executed, sent, reason, send_result=None):
        return {"executed": executed, "sent": sent, "reason": reason,
                "send_result": send_result}

    planned = planned or {}
    message = message or {}
    mode = planned.get("mode")

    if mode != "active" or not planned.get("would_send"):
        return _out(False, False, f"not an active send plan (mode={mode})")

    if not close_gateway.configured or not ghl_gateway.configured:
        return _out(False, False,
                    "gateways not configured (CLOSE_GATEWAY_URL/GHL_GATEWAY_URL "
                    "unset) — dry run, nothing attempted")

    skill = planned.get("skill")
    template = _template_for(skill)
    sender = message.get("sender", "")
    contact_id = _recipient_for(message)

    # 1. Render (read-only). Empty string = link unresolvable -> escalate.
    try:
        rendered = call_tool_sync(
            close_gateway, "render_template",
            {"template": template, "lead_id": sender, "booking_link": ""},
        )
    except GatewayError as exc:
        return _out(False, False, f"render failed: {exc}")
    if not isinstance(rendered, str) or not rendered.strip():
        return _out(False, False,
                    "booking link unresolvable — escalated, never invent a link")

    # 2. Send (gated inside the gateway).
    # Only the GHL pathway can execute; the Close pathway is shadow-only
    # by design (Close is read-only).
    if planned.get("send_tool") != "ghl.send_sms":
        return _out(True, False,
                    f"send refused: {planned.get('send_tool')} is not an "
                    "executable send pathway")

    try:
        send_result = call_tool_sync(
            ghl_gateway, "send_sms",
            {"contact_id": contact_id, "body": rendered.strip()},
        )
    except GatewayError as exc:
        return _out(True, False, f"send attempted but gateway unreachable: {exc}")
    if not isinstance(send_result, dict):
        return _out(True, False, f"unexpected send result: {send_result!r}"[:200])
    if send_result.get("sent") is True:
        return _out(True, True, "sent via ghl-gateway.send_sms", send_result)
    return _out(True, False,
                f"gateway refused the send: {send_result.get('reason', send_result)}"[:300],
                send_result)
