"""ghl-gateway MCP server. Trust domain: GoHighLevel API v2.

The ONLY thing in the system that can send SMS.

Safety model: the two send gates live INSIDE send_sms, as part of the send
itself — never as a tool the agent is asked to call first:
  1. SEND_ENABLED == "true" (any other value, including unset, refuses)
  2. SEND_ALLOWLIST non-empty AND the recipient in it (empty allowlist =
     nothing can send, which is the default)

Nothing sends until Adam explicitly authorizes it. Read tools return honest
"not_configured" errors when GHL_PRIVATE_TOKEN / GHL_LOCATION_ID are
missing — never fake data.

GHL API v2 docs: https://marketplace.gohighlevel.com/docs/
  Base:    https://services.leadconnectorhq.com
  Headers: Authorization: Bearer <private integration token>
           Version: 2021-07-28
  Contact: GET /contacts/{contactId}
  Search:  GET /conversations/search?locationId={loc}&contactId={id}
  History: GET /conversations/{conversationId}/messages
  Send:    POST /conversations/messages with
           {"type": "SMS", "contactId": ..., "message": ...}
           (confirmed via GHL docs; include "locationId" when the token
           covers multiple locations [VERIFY])
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"

mcp = FastMCP("ghl-gateway")


# ---------------------------------------------------------------------------
# Env config (read lazily so tests can override env between calls)
# ---------------------------------------------------------------------------

def _token() -> str | None:
    return os.environ.get("GHL_PRIVATE_TOKEN") or None


def _location_id() -> str | None:
    return os.environ.get("GHL_LOCATION_ID") or None


def _send_enabled() -> bool:
    return os.environ.get("SEND_ENABLED") == "true"


def _allowlist() -> set[str]:
    raw = os.environ.get("SEND_ALLOWLIST", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _not_configured() -> dict:
    missing = [
        v for v, val in
        (("GHL_PRIVATE_TOKEN", _token()), ("GHL_LOCATION_ID", _location_id()))
        if not val
    ]
    return {
        "error": "not_configured",
        "detail": f"Missing: {', '.join(missing)}. Set them in the environment; "
                  "see .env.example. No fake data is returned.",
    }


def _client() -> httpx.Client:
    import httpx  # deferred: only needed for live API calls

    token = _token()
    if not token:
        raise RuntimeError("GHL_PRIVATE_TOKEN is not set")
    return httpx.Client(
        base_url=GHL_BASE,
        headers={
            "Authorization": f"Bearer {token}",
            "Version": GHL_VERSION,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@mcp.tool()
def get_contact(contact_id: str) -> dict:
    """Contact details from GHL (read-only)."""
    if not _token() or not _location_id():
        return _not_configured()
    try:
        with _client() as c:
            r = c.get(f"/contacts/{contact_id}")
            r.raise_for_status()
            d = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": "api_error", "detail": f"{type(exc).__name__}: {exc}"}
    contact = d.get("contact", d)
    return {
        "contact_id": contact.get("id"),
        "name": " ".join(
            p for p in (contact.get("firstName"), contact.get("lastName")) if p
        ).strip(),
        "email": contact.get("email"),
        "phone": contact.get("phone"),
        "tags": contact.get("tags") or [],
        "raw": contact,
    }


@mcp.tool()
def get_conversation(contact_id: str) -> dict:
    """Conversation history for a GHL contact (read-only).

    Finds the contact's conversation via /conversations/search, then reads
    its messages. Returns the newest-first message list.
    """
    if not _token() or not _location_id():
        return _not_configured()
    try:
        with _client() as c:
            s = c.get(
                "/conversations/search",
                params={"locationId": _location_id(), "contactId": contact_id},
            )
            s.raise_for_status()
            convos = (s.json().get("conversations") or [])
            if not convos:
                return {"contact_id": contact_id, "messages": [],
                        "note": "no conversations found for this contact"}
            convo_id = convos[0].get("id")
            m = c.get(f"/conversations/{convo_id}/messages", params={"limit": 50})
            m.raise_for_status()
            msgs = m.json().get("messages", {}).get("messages") or []
    except Exception as exc:  # noqa: BLE001
        return {"error": "api_error", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "contact_id": contact_id,
        "conversation_id": convo_id,
        "messages": [
            {
                "id": msg.get("id"),
                "type": msg.get("messageType"),
                "direction": msg.get("direction"),
                "body": msg.get("body"),
                "date_added": msg.get("dateAdded"),
            }
            for msg in msgs
        ],
    }


# ---------------------------------------------------------------------------
# Send — the two gates live INSIDE this function.
# ---------------------------------------------------------------------------

def _send_gate(contact_id: str) -> dict | None:
    """Return a refusal dict if the send must not proceed, else None."""
    if not _send_enabled():
        return {
            "sent": False,
            "reason": "refused: SEND_ENABLED is not 'true' — sending is disabled",
        }
    allowlist = _allowlist()
    if not allowlist:
        return {
            "sent": False,
            "reason": "refused: SEND_ALLOWLIST is empty — nothing may send",
        }
    if contact_id not in allowlist:
        # Also honor allowlisted phone numbers for the contact.
        phones: set[str] = set()
        if _token() and _location_id():
            contact = get_contact(contact_id)
            if not contact.get("error") and contact.get("phone"):
                phones.add(contact["phone"])
        if not (phones & allowlist):
            return {
                "sent": False,
                "reason": "refused: recipient not on SEND_ALLOWLIST",
            }
    return None


@mcp.tool()
def send_sms(contact_id: str, body: str) -> dict:
    """Send an SMS via GHL. Gates: SEND_ENABLED=='true' AND contact_id (or
    its phone number) on SEND_ALLOWLIST. Refuses otherwise. Nothing sends
    until Adam authorizes it — test sends go only to a designated contact
    on the allowlist."""
    refusal = _send_gate(contact_id)
    if refusal is not None:
        return refusal
    if not _token() or not _location_id():
        return {"sent": False, "reason": "not_configured: GHL_PRIVATE_TOKEN / GHL_LOCATION_ID are not set"}
    try:
        # POST /conversations/messages {"type": "SMS", "contactId", "message"}
        # [VERIFY] exact accepted fields against GHL's official
        # marketplace docs (send-a-message); "locationId" may be required
        # when the token covers multiple locations.
        payload: dict = {
            "type": "SMS",
            "contactId": contact_id,
            "message": body,
        }
        with _client() as c:
            r = c.post("/conversations/messages", json=payload)
            r.raise_for_status()
            created = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "reason": f"api_error: {type(exc).__name__}: {exc}"}
    return {
        "sent": True,
        "message_id": created.get("id") or created.get("messageId"),
        "contact_id": contact_id,
    }


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport in ("sse", "streamable-http"):
        # Remote deployments (Railway): serve MCP over HTTP. Railway injects
        # PORT; MCP_PORT overrides it when set explicitly.
        mcp.settings.host = os.environ.get("MCP_HOST", "::")
        mcp.settings.port = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8000")))
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
