"""MCP client for the gateway servers.

The agent operates entirely through MCP tools — no direct provider API calls
in agent code. Gateway endpoints come from the environment:

    CLOSE_GATEWAY_URL  e.g. http://localhost:8123/sse
    GHL_GATEWAY_URL    e.g. http://localhost:8124/sse

Connections are lazy (opened on first tool call, closed after). Every failure
is fail-closed: an unconfigured or unreachable gateway raises GatewayError,
which the executor turns into "not executed" — never a silent skip, and the
gateway's own send gates (SEND_ENABLED + allowlist) still apply on top.

Stdlib only at import time; the `mcp` package is imported lazily so this
module (and the offline smoke tests) load in a bare environment.
"""

from __future__ import annotations

import asyncio
import json
import os


class GatewayError(RuntimeError):
    """A gateway is unconfigured, unreachable, or refused the call."""


def _direct_http_client(**kwargs):
    """httpx client that ignores proxy env vars.

    Gateway calls are service-to-service (Railway private network or
    localhost in dev) and must never go through an egress proxy.
    """
    import httpx

    kwargs.setdefault("trust_env", False)
    return httpx.AsyncClient(**kwargs)


def _unwrap(result):
    """Extract the tool's return value from a CallToolResult.

    FastMCP serializes tool returns to JSON text blocks. Raises GatewayError
    when the tool itself reported an error.
    """
    if getattr(result, "isError", False):
        texts = " ".join(getattr(b, "text", "") or "" for b in result.content or [])
        raise GatewayError(f"tool reported error: {texts[:300]}")
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:  # empty string is a legitimate tool result
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


class GatewayClient:
    """Thin MCP client for one gateway server (SSE transport)."""

    def __init__(self, name: str, url_env: str):
        self.name = name
        self.url_env = url_env

    @property
    def url(self) -> str:
        return os.environ.get(self.url_env, "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def call_tool(self, tool: str, arguments: dict):
        """Call a tool on the gateway. Raises GatewayError on any failure."""
        if not self.configured:
            raise GatewayError(f"{self.name} not configured ({self.url_env} unset)")
        try:
            from mcp.client.session import ClientSession
            from mcp.client.sse import sse_client

            async with sse_client(self.url,
                                 httpx_client_factory=_direct_http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalize to GatewayError
            raise GatewayError(
                f"{self.name}: call {tool} failed "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        return _unwrap(result)


def call_tool_sync(client: GatewayClient, tool: str, arguments: dict):
    """Synchronous wrapper for the sync pipeline. Raises GatewayError."""
    return asyncio.run(client.call_tool(tool, arguments))


# Shared clients, configured via environment.
close_gateway = GatewayClient("close-gateway", "CLOSE_GATEWAY_URL")
ghl_gateway = GatewayClient("ghl-gateway", "GHL_GATEWAY_URL")
