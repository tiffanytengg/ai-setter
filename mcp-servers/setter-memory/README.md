# setter-memory

Stdio MCP server over Postgres. **Trust domain: the database only.** No
external network access, no send capability by design — this server is the
audit trail: every message and every agent decision gets logged here so
accuracy is computed, not asserted.

## Tools

| Tool | Signature | What it does |
|---|---|---|
| `log_message` | `log_message(message: dict)` | Stores the canonical message (`message_id, channel, source, sender, body, timestamp`). Idempotent on `message_id` (re-deliveries collapse). Returns `{"ok": true, "message_id": ...}`. |
| `log_decision` | `log_decision(message_id, intent, confidence, reasoning, skill, action_taken)` | Stores one decision row. `skill` nullable; `action_taken` e.g. `sent_sms`, `shadow_logged`, `dropped_opt_out`, `none`. Returns `{"ok": true}`. |
| `get_state` | `get_state(message_id)` | Returns `{"message": {...} \| null, "decision": {...} \| null}` — the message plus its **latest** decision, for agent context loading. |

## Environment

- `SETTER_MEMORY_DATABASE_URL` — Postgres connection string. **Env var only:
  never in code, never logged.** When unset and memory mode wasn't
  explicitly requested, the server raises an error naming this variable —
  there is no silent fallback to non-persistent storage.
- Memory mode (tests/demos only): pass `--memory` or set
  `SETTER_MEMORY_MEMORY_MODE=1`. The server loudly warns on stderr that
  nothing is persisted.

## Schema

`schema.sql` (apply once per database: `psql "$SETTER_MEMORY_DATABASE_URL" -f schema.sql`):

- `messages` — one row per inbound message. `message_id` (unique, from the
  webhook) is the canonical key; `id` is our surrogate PK; `channel, source,
  sender, body, received_at`, `created_at` default `now()`.
- `decisions` — one row per agent decision, FK `message_id → messages`,
  `ON DELETE CASCADE`. `intent, confidence, reasoning, skill (nullable),
  action_taken`, `created_at` default `now()`. Multiple decisions per
  message allowed; readers take the latest by `created_at`.

## What's stubbed / untested

- **No live Postgres connection has been tested here.** Postgres is
  unavailable in the dev environment, so `PostgresStore` (SQL + psycopg
  wiring) is written but untested until deployment. The Railway-hosted
  database is the real DB; verify with `schema.sql` + a live
  `log_message`/`log_decision`/`get_state` round-trip after deploy.
- `MemoryStore` (in-memory, non-persistent) is fully exercised by the
  demo below and is the only path verified locally.

## Run

```bash
pip install -e .
setter-memory-server                 # stdio; needs SETTER_MEMORY_DATABASE_URL
setter-memory-server --memory        # explicit non-persistent mode

# Demo against MemoryStore (stdlib + package only):
cd mcp-servers/setter-memory/src && python -m setter_memory.demo_memory
```
