"""SQLite-backed state management.

Replaces state.json and conversation_log.jsonl with a single SQLite database.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "calendar_assistant.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_history_chat
                ON conversation_history(chat_id);
            CREATE INDEX IF NOT EXISTS idx_log_chat
                ON conversation_log(chat_id);
        """)


# ── Conversation history ─────────────────────────────────────────────────────

def get_history(chat_id: str, limit: int = 40) -> list[dict]:
    """Retrieve the most recent conversation messages (chronological order)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversation_history "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]


def append_message(chat_id: str, role: str, content):
    """Append a message to conversation history."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversation_history (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, json.dumps(content, default=str)),
        )


def trim_history(chat_id: str, keep: int = 40):
    """Keep only the most recent `keep` messages for a chat."""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM conversation_history WHERE chat_id = ? AND id NOT IN "
            "(SELECT id FROM conversation_history WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?)",
            (chat_id, chat_id, keep),
        )


def clear_history(chat_id: str):
    """Clear all conversation history for a chat."""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM conversation_history WHERE chat_id = ?", (chat_id,)
        )


# ── Event log ────────────────────────────────────────────────────────────────

def log_event(chat_id: str, event_type: str, **fields):
    """Append a structured event to the conversation log."""
    data = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversation_log (chat_id, event_type, data) VALUES (?, ?, ?)",
            (chat_id, event_type, json.dumps(data, default=str)),
        )
