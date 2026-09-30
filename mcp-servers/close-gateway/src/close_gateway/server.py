"""close-gateway MCP server. Trust domain: Close API.

READ-ONLY BY DESIGN. Only get_message, get_lead, and render_template exist.
There is NO send tool in this server, so there is nothing to misconfigure
into sending. (Client requirement, confirmed 2026-09-28: Close is the
read-only system of record for inbound context; the live send path is
ghl-gateway.send_sms.)

Reads return honest "not configured" errors when CLOSE_API_KEY is missing —
never fake data.

Close REST API docs: https://developer.close.com/
  Base:   https://api.close.com/api/v1/
  Auth:   HTTP Basic, API key as username, empty password.
  Reads:  GET /activity/sms/{id}/, GET /lead/{id}/,
          GET /activity/sms/?lead_id={id} (history)
  Me:     GET /me/ -> the API key owner's user id (for link resolution)
  Links:  GET /scheduling_link/user/{user_id}/ [VERIFY: exact path; Close
          docs have a "User Scheduling Links" resource with a list endpoint]
"""

from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

CLOSE_BASE = "https://api.close.com/api/v1"

mcp = FastMCP("close-gateway")


# ---------------------------------------------------------------------------
# Env config (read lazily so tests can override env between calls)
# ---------------------------------------------------------------------------

def _api_key() -> str | None:
    return os.environ.get("CLOSE_API_KEY") or None


def _booking_link_map() -> dict:
    raw = os.environ.get("BOOKING_LINK_MAP", "")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _not_configured(what: str) -> dict:
    return {
        "error": "not_configured",
        "detail": f"{what} is not set. Set it in the environment; "
                  "see .env.example. No fake data is returned.",
    }


def _client() -> httpx.Client:
    """Authenticated Close client. Raises RuntimeError if key missing."""
    import httpx  # deferred: only needed for live API calls

    key = _api_key()
    if not key:
        raise RuntimeError("CLOSE_API_KEY is not set")
    return httpx.Client(
        base_url=CLOSE_BASE,
        auth=(key, ""),  # Close: API key as username, empty password
        timeout=30.0,
    )


def _api_error(exc: Exception) -> dict:
    return {"error": "api_error", "detail": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@mcp.tool()
def get_message(message_id: str) -> dict:
    """Fetch one inbound SMS activity from Close by its activity id."""
    if not _api_key():
        return _not_configured("CLOSE_API_KEY")
    try:
        with _client() as c:
            r = c.get(f"/activity/sms/{message_id}/")
            r.raise_for_status()
            d = r.json()
    except Exception as exc:  # noqa: BLE001
        return _api_error(exc)
    return {
        "message_id": d.get("id"),
        "lead_id": d.get("lead_id"),
        "contact_id": d.get("contact_id"),
        "direction": d.get("direction"),
        "body": d.get("text"),
        "sender": d.get("remote_phone") or d.get("sender"),
        "local_phone": d.get("local_phone"),
        "status": d.get("status"),
        "timestamp": d.get("date_created"),
        "raw": d,
    }


@mcp.tool()
def get_lead(lead_id: str) -> dict:
    """Lead details + recent SMS history for a lead."""
    if not _api_key():
        return _not_configured("CLOSE_API_KEY")
    try:
        with _client() as c:
            lead_r = c.get(f"/lead/{lead_id}/")
            lead_r.raise_for_status()
            lead = lead_r.json()
            sms_r = c.get(
                "/activity/sms/",
                params={"lead_id": lead_id, "_limit": 20, "_order_by": "-date_created"},
            )
            sms_r.raise_for_status()
            sms = sms_r.json()
    except Exception as exc:  # noqa: BLE001
        return _api_error(exc)
    contacts = lead.get("contacts") or []
    phones: list[str] = []
    for contact in contacts:
        for ph in contact.get("phones") or []:
            if ph.get("phone"):
                phones.append(ph["phone"])
    return {
        "lead_id": lead.get("id"),
        "name": lead.get("display_name") or lead.get("name"),
        "status": (lead.get("status_label") or ""),
        "phones": phones,
        "contacts": [
            {"id": ct.get("id"), "name": ct.get("name"), "emails": [e.get("email") for e in (ct.get("emails") or [])]}
            for ct in contacts
        ],
        "recent_sms": [
            {
                "id": a.get("id"),
                "direction": a.get("direction"),
                "text": a.get("text"),
                "date_created": a.get("date_created"),
                "sender": a.get("remote_phone") or a.get("sender"),
            }
            for a in (sms.get("data") or [])
        ],
        "raw": lead,
    }


# ---------------------------------------------------------------------------
# Template rendering (read-only: resolves the link, renders text, sends nothing)
# ---------------------------------------------------------------------------

TEMPLATES = {
    # [VERIFY: exact production wording against the templates the setters use;
    #  placeholders {name} and {link} are filled below.]
    "booking": (
        "Hi {name}! This is {sender_name} — you can grab a time with me here: {link}"
    ),
    "reschedule": (
        "Hi {name}! Need to move your call? Pick a new time here: {link}"
    ),
}


def _resolve_booking_link(lead_id: str) -> str:
    """Resolve the sending user's booking link.

    1. BOOKING_LINK_MAP env JSON (account/user -> link), incl. "default" key.
    2. The API-key owner's user profile via Close: GET /me/ then the
       "User Scheduling Links" list endpoint. [VERIFY both paths]
    Returns "" when unresolvable — the caller must escalate, never invent.
    """
    link_map = _booking_link_map()
    if link_map:
        if isinstance(link_map.get("default"), str) and link_map["default"].strip():
            return link_map["default"].strip()
        if len(link_map) == 1:
            only = next(iter(link_map.values()))
            if isinstance(only, str) and only.strip():
                return only.strip()
    if not _api_key():
        return ""
    try:
        with _client() as c:
            me_r = c.get("/me/")
            me_r.raise_for_status()
            me = me_r.json()
            user_id = me.get("id")
            org_id = (me.get("organization_id") or "")
            if link_map:
                for key in (user_id, org_id):
                    if key and isinstance(link_map.get(key), str) and link_map[key].strip():
                        return link_map[key].strip()
            if user_id:
                # [VERIFY] Close docs: "User Scheduling Links" resource, list endpoint.
                links_r = c.get(f"/scheduling_link/user/{user_id}/")
                if links_r.status_code == 200:
                    links = links_r.json().get("data") or []
                    for link in links:
                        url = link.get("url") or link.get("link")
                        if url:
                            return url
    except Exception:  # noqa: BLE001 — resolution failure -> "" (escalate)
        return ""
    return ""


@mcp.tool()
def render_template(template: str, lead_id: str, booking_link: str = "") -> str:
    """Render the booking/reschedule template for a lead.

    If booking_link is given it is used directly; otherwise it is resolved
    from BOOKING_LINK_MAP then the Close user profile. Returns "" when the
    link cannot be resolved — the skill must escalate, never invent one.
    This only renders text; the actual send happens in ghl-gateway.send_sms.
    """
    if template not in TEMPLATES:
        return ""
    link = (booking_link or "").strip() or _resolve_booking_link(lead_id)
    if not link:
        return ""
    name = "there"
    sender_name = "your setter"
    if _api_key():
        try:
            lead = get_lead(lead_id)
            if not lead.get("error"):
                lead_name = (lead.get("name") or "").strip()
                if lead_name:
                    name = lead_name.split()[0]
        except Exception:  # noqa: BLE001 — name lookup is best-effort only
            pass
    return TEMPLATES[template].format(name=name, link=link, sender_name=sender_name)


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
