"""setter-memory MCP server. Trust domain: the database only.

Stdio MCP server fronting the audit-trail tables (see ../schema.sql).
It can reach Postgres and nothing else: no external network calls, and
no send capability by design -- it logs what happened, it cannot act.

Storage selection:
  * SETTER_MEMORY_DATABASE_URL set  -> PostgresStore (psycopg, lazy import).
  * --memory flag or SETTER_MEMORY_MEMORY_MODE=1 -> MemoryStore (explicit
    opt-in only; loudly warns on stderr that nothing is persisted).
  * neither -> RuntimeError naming the env var. No silent fallback.

The connection string lives in the environment only -- never in code,
never logged.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

ENV_VAR = "SETTER_MEMORY_DATABASE_URL"
MEMORY_MODE_ENV = "SETTER_MEMORY_MEMORY_MODE"


def _warn_non_persistent() -> None:
    print(
        "WARNING: setter-memory is running with MemoryStore: "
        "nothing is persisted; all state is lost on exit. "
        f"Set {ENV_VAR} for the real Postgres audit trail.",
        file=sys.stderr,
        flush=True,
    )


class Store(Protocol):
    """Storage interface behind the MCP tools."""

    def log_message(self, message: dict) -> dict: ...
    def log_decision(
        self,
        message_id: str,
        intent: str,
        confidence: float,
        reasoning: str,
        skill: str | None,
        action_taken: str,
    ) -> dict: ...
    def get_state(self, message_id: str) -> dict: ...


class MemoryStore:
    """In-memory Store. For tests and demos only -- non-persistent by design."""

    def __init__(self) -> None:
        _warn_non_persistent()
        self.messages: dict[str, dict] = {}   # message_id -> row
        self.decisions: dict[str, list[dict]] = {}  # message_id -> rows, insertion order

    def log_message(self, message: dict) -> dict:
        message_id = message.get("message_id")
        if not message_id:
            raise ValueError("message dict requires a 'message_id' key")
        row = self.messages.get(message_id)
        if row is None:
            row = {
                "id": uuid.uuid4().hex,
                "message_id": message_id,
                "channel": message.get("channel"),
                "source": message.get("source"),
                "sender": message.get("sender"),
                "body": message.get("body"),
                "received_at": message.get("timestamp"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.messages[message_id] = row
        return {"ok": True, "message_id": message_id}

    def log_decision(
        self,
        message_id: str,
        intent: str,
        confidence: float,
        reasoning: str,
        skill: str | None,
        action_taken: str,
    ) -> dict:
        if message_id not in self.messages:
            raise ValueError(f"unknown message_id: {message_id!r}")
        row = {
            "id": uuid.uuid4().hex,
            "message_id": message_id,
            "intent": intent,
            "confidence": confidence,
            "reasoning": reasoning,
            "skill": skill,
            "action_taken": action_taken,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.decisions.setdefault(message_id, []).append(row)
        return {"ok": True}

    def get_state(self, message_id: str) -> dict:
        latest = (self.decisions.get(message_id) or [None])[-1]
        return {"message": self.messages.get(message_id), "decision": latest}


class PostgresStore:
    """psycopg-backed Store. Driver import is lazy so plain `import` of this
    module never requires a database driver."""

    def __init__(self, conninfo: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "psycopg is required for Postgres mode; install it with "
                "'pip install \"psycopg[binary]>=3.1\"'."
            ) from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._conninfo = conninfo  # never logged

    def _connect(self):
        return self._psycopg.connect(self._conninfo, row_factory=self._dict_row)

    @staticmethod
    def _jsonable(row: dict | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("received_at", "created_at"):
            if isinstance(out.get(key), datetime):
                out[key] = out[key].isoformat()
        return out

    def log_message(self, message: dict) -> dict:
        message_id = message.get("message_id")
        if not message_id:
            raise ValueError("message dict requires a 'message_id' key")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO messages
                        (id, message_id, channel, source, sender, body, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (message_id) DO NOTHING
                    """,
                    (
                        uuid.uuid4().hex,
                        message_id,
                        message.get("channel"),
                        message.get("source"),
                        message.get("sender"),
                        message.get("body"),
                        message.get("timestamp"),
                    ),
                )
        return {"ok": True, "message_id": message_id}

    def log_decision(
        self,
        message_id: str,
        intent: str,
        confidence: float,
        reasoning: str,
        skill: str | None,
        action_taken: str,
    ) -> dict:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO decisions
                        (id, message_id, intent, confidence, reasoning, skill, action_taken)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid.uuid4().hex,
                        message_id,
                        intent,
                        confidence,
                        reasoning,
                        skill,
                        action_taken,
                    ),
                )
        return {"ok": True}

    def get_state(self, message_id: str) -> dict:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM messages WHERE message_id = %s", (message_id,)
                )
                message = self._jsonable(cur.fetchone())
                cur.execute(
                    """
                    SELECT * FROM decisions
                    WHERE message_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (message_id,),
                )
                decision = self._jsonable(cur.fetchone())
        return {"message": message, "decision": decision}


def make_store(explicit_memory: bool = False) -> Store:
    """Build the Store.

    Raises RuntimeError naming ENV_VAR when no connection string is set and
    memory mode was not explicitly requested -- there is no silent fallback.
    """
    if explicit_memory:
        return MemoryStore()
    conninfo = os.environ.get(ENV_VAR)
    if not conninfo:
        raise RuntimeError(
            f"{ENV_VAR} is not set and memory mode was not explicitly requested. "
            f"Set {ENV_VAR} to the Postgres connection string, or pass --memory / "
            f"{MEMORY_MODE_ENV}=1 to run non-persistently (tests and demos only)."
        )
    return PostgresStore(conninfo)


_STORE: Store | None = None


def _store() -> Store:
    """Lazily build the process-wide store on first tool call."""
    global _STORE
    if _STORE is None:
        explicit = "--memory" in sys.argv[1:] or os.environ.get(MEMORY_MODE_ENV) == "1"
        _STORE = make_store(explicit_memory=explicit)
    return _STORE


def create_mcp_server():
    """Build the FastMCP app. The `mcp` import is lazy so that importing
    this module (e.g. in tests or the demo) does not require the MCP SDK."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "the 'mcp' package is required to run the MCP server "
            "(pip install 'mcp>=1.0,<2')."
        ) from exc

    mcp = FastMCP(
        name="setter-memory",
        instructions=(
            "Audit trail for the AI Setter. Log every inbound message with "
            "log_message, log every agent decision with log_decision, and "
            "load message + latest decision with get_state. This server has "
            "no send capability and makes no external network calls."
        ),
    )

    @mcp.tool(description="Record an inbound message before any processing.")
    def log_message(message: dict) -> dict:
        """Store the canonical message.

        message: {"message_id", "channel", "source", "sender", "body", "timestamp"}.
        Returns {"ok": True, "message_id": ...}.
        """
        return _store().log_message(message)

    @mcp.tool(description="Record the agent's decision about a message.")
    def log_decision(
        message_id: str,
        intent: str,
        confidence: float,
        reasoning: str,
        skill: str | None,
        action_taken: str,
    ) -> dict:
        """Store one decision row (sent_sms, shadow_logged, dropped_opt_out, none, ...).
        Returns {"ok": True}.
        """
        return _store().log_decision(
            message_id, intent, confidence, reasoning, skill, action_taken
        )

    @mcp.tool(description="Load a message plus its latest decision.")
    def get_state(message_id: str) -> dict:
        """Return {"message": {...} | None, "decision": {...} | None}."""
        return _store().get_state(message_id)

    return mcp


def main() -> None:
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
