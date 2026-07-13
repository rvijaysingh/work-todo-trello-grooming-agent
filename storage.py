"""
SQLite persistence for the grooming agent (schema per design.md §7.1).

Tables: snapshots, actions, proposals, rejections, recovery_ledger, plus a
singleton run_state row for the consecutive-failure counter and paused flag
(three consecutive failed runs pause the agent until manually cleared — see
README "Clearing the pause flag").

Connections and table creation go through agent_shared.infra db helpers; this
module owns only the schema SQL and the agent's query/insert logic. The db_path
is always passed in — never hardcoded.
"""

from __future__ import annotations

import json
import logging

from agent_shared.infra import db_connection, ensure_table, get_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        run_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        card_id TEXT NOT NULL,
        list_id TEXT,
        name TEXT,
        desc_hash TEXT,
        labels_json TEXT,
        due TEXT,
        date_last_activity TEXT,
        PRIMARY KEY (run_id, card_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actions (
        action_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        tier INTEGER,
        action_type TEXT NOT NULL,
        card_ids_json TEXT NOT NULL,
        payload_json TEXT,
        status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        card_ids_json TEXT NOT NULL,
        action_json TEXT,
        reason TEXT,
        status TEXT NOT NULL CHECK(status IN ('open','approved','rejected','expired')),
        opened_ts TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rejections (
        fingerprint TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        ts TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_ledger (
        card_id TEXT PRIMARY KEY,
        source_list TEXT,
        disposition TEXT,
        ts TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS archive_ledger (
        card_id TEXT PRIMARY KEY,
        entered_ts TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_state (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 0,
        updated_ts TEXT
    )
    """,
]


def init_storage(db_path: str) -> None:
    """Create all tables if absent and ensure the singleton run_state row."""
    conn = get_db_connection(db_path)
    try:
        for stmt in _SCHEMA:
            ensure_table(conn, stmt)
        conn.execute(
            "INSERT OR IGNORE INTO run_state (id, consecutive_failures, paused) VALUES (1, 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Storage initialized at %s", db_path)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def save_snapshot(db_path: str, run_id: str, ts: str, cards) -> None:
    """Persist the current board cards as a snapshot for this run."""
    with db_connection(db_path) as conn:
        for c in cards:
            conn.execute(
                """
                INSERT OR REPLACE INTO snapshots
                    (run_id, ts, card_id, list_id, name, desc_hash, labels_json, due, date_last_activity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, ts, c.id, c.list_id, c.name,
                    _desc_hash(c.desc), json.dumps(sorted(c.label_names)),
                    c.due, c.last_activity,
                ),
            )
    logger.debug("Saved snapshot run_id=%s (%d cards)", run_id, len(list(cards)))


def _desc_hash(desc: str) -> str:
    import hashlib

    return hashlib.sha256((desc or "").encode("utf-8")).hexdigest()


def latest_prior_run_id(db_path: str, current_run_id: str | None = None) -> str | None:
    """Return the most recent run_id in snapshots, excluding current_run_id."""
    with db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT run_id, ts FROM snapshots ORDER BY ts DESC"
        ).fetchall()
    for r in rows:
        if current_run_id is None or r["run_id"] != current_run_id:
            return r["run_id"]
    return None


def get_snapshot(db_path: str, run_id: str) -> dict[str, dict]:
    """Return {card_id: row-dict} for a given snapshot run_id."""
    with db_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM snapshots WHERE run_id = ?", (run_id,)).fetchall()
    return {r["card_id"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def record_action(db_path: str, run_id: str, ts: str, tier, action_type: str,
                  card_ids, payload: dict, status: str) -> None:
    """Record an executed (or attempted) action."""
    with db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO actions (run_id, ts, tier, action_type, card_ids_json, payload_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, ts, tier, action_type, json.dumps(sorted(card_ids)),
             json.dumps(payload), status),
        )


def get_actions(db_path: str, action_type: str | None = None, run_id: str | None = None) -> list[dict]:
    """Fetch action rows, optionally filtered by type and/or run_id."""
    q = "SELECT * FROM actions WHERE 1=1"
    params: list = []
    if action_type is not None:
        q += " AND action_type = ?"
        params.append(action_type)
    if run_id is not None:
        q += " AND run_id = ?"
        params.append(run_id)
    q += " ORDER BY ts"
    with db_connection(db_path) as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["card_ids"] = json.loads(d["card_ids_json"])
        d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

def add_proposal(db_path: str, run_id: str, fingerprint: str, card_ids,
                 action: dict, reason: str, opened_ts: str) -> int:
    """Insert an open Tier 2 proposal; returns its proposal_id."""
    with db_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO proposals
                (run_id, fingerprint, card_ids_json, action_json, reason, status, opened_ts)
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (run_id, fingerprint, json.dumps(sorted(card_ids)), json.dumps(action),
             reason, opened_ts),
        )
        return int(cur.lastrowid)


def get_open_proposals(db_path: str) -> list[dict]:
    """Return all proposals with status 'open'."""
    with db_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM proposals WHERE status = 'open'").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["card_ids"] = json.loads(d["card_ids_json"])
        d["action"] = json.loads(d["action_json"]) if d["action_json"] else {}
        out.append(d)
    return out


def count_open_proposals(db_path: str) -> int:
    with db_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM proposals WHERE status = 'open'").fetchone()
    return int(row["n"])


def set_proposal_status(db_path: str, proposal_id: int, status: str) -> None:
    with db_connection(db_path) as conn:
        conn.execute("UPDATE proposals SET status = ? WHERE proposal_id = ?", (status, proposal_id))


# ---------------------------------------------------------------------------
# Rejections (the "never re-propose" ledger)
# ---------------------------------------------------------------------------

def add_rejection(db_path: str, fingerprint: str, source: str, ts: str) -> None:
    with db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rejections (fingerprint, source, ts) VALUES (?, ?, ?)",
            (fingerprint, source, ts),
        )


def is_rejected(db_path: str, fingerprint: str) -> bool:
    with db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM rejections WHERE fingerprint = ? LIMIT 1", (fingerprint,)
        ).fetchone()
    return row is not None


def get_rejections(db_path: str) -> list[dict]:
    with db_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM rejections ORDER BY ts").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Recovery ledger
# ---------------------------------------------------------------------------

def add_recovery(db_path: str, card_id: str, source_list: str, disposition: str, ts: str) -> None:
    with db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO recovery_ledger (card_id, source_list, disposition, ts) VALUES (?, ?, ?, ?)",
            (card_id, source_list, disposition, ts),
        )


def processed_recovery_ids(db_path: str) -> set[str]:
    with db_connection(db_path) as conn:
        rows = conn.execute("SELECT card_id FROM recovery_ledger").fetchall()
    return {r["card_id"] for r in rows}


# ---------------------------------------------------------------------------
# Archive ledger — entry timestamps for cards parked in the Agent Archive list
# ---------------------------------------------------------------------------

def add_archive_entry(db_path: str, card_id: str, entered_ts: str) -> None:
    """Record (or refresh) when a card entered the Agent Archive list.

    INSERT OR IGNORE so a card already tracked keeps its original entry time —
    the 60-day Trello-archive clock starts when the card first lands there.
    """
    with db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO archive_ledger (card_id, entered_ts) VALUES (?, ?)",
            (card_id, entered_ts),
        )


def archive_entry_ts(db_path: str, card_id: str) -> str | None:
    with db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT entered_ts FROM archive_ledger WHERE card_id = ?", (card_id,)
        ).fetchone()
    return row["entered_ts"] if row else None


def remove_archive_entry(db_path: str, card_id: str) -> None:
    """Drop a card from the archive ledger (Trello-archived, or pulled back out)."""
    with db_connection(db_path) as conn:
        conn.execute("DELETE FROM archive_ledger WHERE card_id = ?", (card_id,))


# ---------------------------------------------------------------------------
# Key/value scratch state (e.g. last week the spine-review reminder was created)
# ---------------------------------------------------------------------------

def kv_get(db_path: str, key: str) -> str | None:
    with db_connection(db_path) as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(db_path: str, key: str, value: str) -> None:
    with db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
        )


# ---------------------------------------------------------------------------
# Run state — pause flag and consecutive-failure counter
# ---------------------------------------------------------------------------

def get_run_state(db_path: str) -> dict:
    with db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM run_state WHERE id = 1").fetchone()
    if row is None:
        return {"consecutive_failures": 0, "paused": 0}
    return dict(row)


def is_paused(db_path: str) -> bool:
    return bool(get_run_state(db_path).get("paused"))


def record_success(db_path: str, ts: str) -> None:
    """Reset the consecutive-failure counter after a successful run."""
    with db_connection(db_path) as conn:
        conn.execute(
            "UPDATE run_state SET consecutive_failures = 0, updated_ts = ? WHERE id = 1", (ts,)
        )


def record_failure(db_path: str, ts: str, auto_pause_after: int) -> tuple[int, bool]:
    """Increment the failure counter; pause if it reaches auto_pause_after.

    Returns (consecutive_failures, paused_now).
    """
    with db_connection(db_path) as conn:
        row = conn.execute("SELECT consecutive_failures FROM run_state WHERE id = 1").fetchone()
        failures = (int(row["consecutive_failures"]) if row else 0) + 1
        paused = 1 if failures >= auto_pause_after else 0
        conn.execute(
            "UPDATE run_state SET consecutive_failures = ?, paused = ?, updated_ts = ? WHERE id = 1",
            (failures, paused, ts),
        )
    return failures, bool(paused)


_STATE_TABLES = ("snapshots", "actions", "proposals", "rejections",
                 "recovery_ledger", "archive_ledger", "kv")


def reset_state(db_path: str) -> dict[str, int]:
    """Wipe all run/diff state so no stale (e.g. dry-run-contaminated) rows leak
    into a later run's diff. Clears snapshots, actions, proposals, rejections,
    recovery/archive ledgers, and kv, and resets the failure/pause counters.

    Safe to run before go-live: it deletes ONLY agent-tracked state, never board
    data. Returns {table: rows_deleted}.
    """
    deleted: dict[str, int] = {}
    with db_connection(db_path) as conn:
        for table in _STATE_TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            deleted[table] = int(n)
        conn.execute(
            "UPDATE run_state SET consecutive_failures = 0, paused = 0 WHERE id = 1"
        )
    logger.info("reset_state cleared: %s", deleted)
    return deleted


def clear_pause(db_path: str, ts: str) -> None:
    """Clear the paused flag and reset the failure counter (manual recovery)."""
    with db_connection(db_path) as conn:
        conn.execute(
            "UPDATE run_state SET paused = 0, consecutive_failures = 0, updated_ts = ? WHERE id = 1",
            (ts,),
        )
    logger.info("Pause flag cleared")


if __name__ == "__main__":
    import tempfile

    logging.basicConfig(level=logging.INFO)
    tmp = tempfile.mktemp(suffix=".db")
    init_storage(tmp)
    add_rejection(tmp, "merge|a,b", "edit", "2026-07-11T00:00:00+00:00")
    print("is_rejected(merge|a,b) ->", is_rejected(tmp, "merge|a,b"))
    print("failure ->", record_failure(tmp, "2026-07-11T00:00:00+00:00", 3))
    print("run_state ->", get_run_state(tmp))
