"""
db.py — persistence layer for Two Truths and a Lie.

Automatically uses PostgreSQL when the DATABASE_URL environment variable is
set (Railway injects this when you add the Postgres plugin), and falls back
to SQLite for local development.  All public functions have identical
signatures regardless of which backend is active, so bot.py never needs to
know which one is running.

PostgreSQL backend:  survives redeploys, scales across dynos.
SQLite backend:      zero-config, but the .db file is lost on Railway
                     redeploys (ephmeral filesystem).  Fine for local dev/
                     testing; add the Railway Postgres plugin for production.
"""

import json
import os
import time
from contextlib import contextmanager
from typing import Optional

# ------------------------------------------------------------------ backend detection --

DATABASE_URL = os.getenv("DATABASE_URL", "")

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    _BACKEND = "postgres"
else:
    import sqlite3
    _BACKEND = "sqlite"

DB_PATH = "two_truths.db"   # only used by SQLite backend


def _pg_connect():
    url = DATABASE_URL
    # Railway sometimes gives postgres:// but psycopg2 needs postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


@contextmanager
def _cursor():
    if _BACKEND == "postgres":
        conn = _pg_connect()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield cur
            conn.commit()
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        finally:
            conn.close()


def _placeholder(n: int = 1) -> str:
    """Return the right parameter placeholder for the active backend."""
    return "%s" if _BACKEND == "postgres" else "?" * n


def ph(n: int = 1):
    """Return a tuple of n placeholders as a comma-joined string."""
    p = "%s" if _BACKEND == "postgres" else "?"
    return ", ".join([p] * n)


def P():
    """Single placeholder."""
    return "%s" if _BACKEND == "postgres" else "?"


# ------------------------------------------------------------------ init --

def init_db(path: str = DB_PATH) -> None:
    global DB_PATH
    if _BACKEND == "sqlite":
        DB_PATH = path

    if _BACKEND == "postgres":
        with _cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS leaderboard (
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    total_points INTEGER NOT NULL DEFAULT 0,
                    times_bluffed_others INTEGER NOT NULL DEFAULT 0,
                    times_caught_lie INTEGER NOT NULL DEFAULT 0,
                    best_bluffer_count INTEGER NOT NULL DEFAULT 0,
                    sharpest_eye_count INTEGER NOT NULL DEFAULT 0,
                    easiest_read_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS known_dm_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS game_snapshots (
                    chat_id BIGINT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                )
                """
            )
    else:
        with _cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS leaderboard (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    total_points INTEGER NOT NULL DEFAULT 0,
                    times_bluffed_others INTEGER NOT NULL DEFAULT 0,
                    times_caught_lie INTEGER NOT NULL DEFAULT 0,
                    best_bluffer_count INTEGER NOT NULL DEFAULT 0,
                    sharpest_eye_count INTEGER NOT NULL DEFAULT 0,
                    easiest_read_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS known_dm_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS game_snapshots (
                    chat_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )


# ---------------------------------------------------------------- DM gating --

def record_known_dm_user(user_id: int, username: str) -> None:
    p = P()
    with _cursor() as cur:
        if _BACKEND == "postgres":
            cur.execute(
                f"INSERT INTO known_dm_users (user_id, username) VALUES ({p}, {p}) "
                f"ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username",
                (user_id, username),
            )
        else:
            cur.execute(
                f"INSERT INTO known_dm_users (user_id, username) VALUES ({p}, {p}) "
                f"ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (user_id, username),
            )


def has_dmed(user_id: int) -> bool:
    p = P()
    with _cursor() as cur:
        cur.execute(f"SELECT 1 FROM known_dm_users WHERE user_id = {p}", (user_id,))
        return cur.fetchone() is not None


# ------------------------------------------------------------- leaderboard --

def ensure_player_row(chat_id: int, user_id: int, username: str) -> None:
    p = P()
    with _cursor() as cur:
        if _BACKEND == "postgres":
            cur.execute(
                f"INSERT INTO leaderboard (chat_id, user_id, username) VALUES ({p},{p},{p}) "
                f"ON CONFLICT(chat_id, user_id) DO UPDATE SET username=EXCLUDED.username",
                (chat_id, user_id, username),
            )
        else:
            cur.execute(
                f"INSERT INTO leaderboard (chat_id, user_id, username) VALUES ({p},{p},{p}) "
                f"ON CONFLICT(chat_id, user_id) DO UPDATE SET username=excluded.username",
                (chat_id, user_id, username),
            )


def apply_end_of_game_stats(chat_id: int, per_player_deltas: dict) -> None:
    p = P()
    ex = "EXCLUDED" if _BACKEND == "postgres" else "excluded"
    with _cursor() as cur:
        for user_id, d in per_player_deltas.items():
            cur.execute(
                f"""
                INSERT INTO leaderboard
                    (chat_id, user_id, username, games_played, total_points,
                     times_bluffed_others, times_caught_lie,
                     best_bluffer_count, sharpest_eye_count, easiest_read_count)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    username = {ex}.username,
                    games_played = leaderboard.games_played + {ex}.games_played,
                    total_points = leaderboard.total_points + {ex}.total_points,
                    times_bluffed_others = leaderboard.times_bluffed_others + {ex}.times_bluffed_others,
                    times_caught_lie = leaderboard.times_caught_lie + {ex}.times_caught_lie,
                    best_bluffer_count = leaderboard.best_bluffer_count + {ex}.best_bluffer_count,
                    sharpest_eye_count = leaderboard.sharpest_eye_count + {ex}.sharpest_eye_count,
                    easiest_read_count = leaderboard.easiest_read_count + {ex}.easiest_read_count
                """,
                (
                    chat_id, user_id,
                    d.get("username", "unknown"),
                    d.get("played", 1),
                    d.get("points", 0),
                    d.get("bluffed_others", 0),
                    d.get("caught_lie", 0),
                    d.get("best_bluffer", 0),
                    d.get("sharpest_eye", 0),
                    d.get("easiest_read", 0),
                ),
            )


def get_leaderboard(chat_id: int) -> list:
    p = P()
    with _cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM leaderboard
            WHERE chat_id = {p}
            ORDER BY total_points DESC, games_played ASC
            """,
            (chat_id,),
        )
        return [dict(row) for row in cur.fetchall()]


# ------------------------------------------------------------- snapshots ---

def save_snapshot(chat_id: int, state_dict: dict) -> None:
    p = P()
    ex = "EXCLUDED" if _BACKEND == "postgres" else "excluded"
    with _cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO game_snapshots (chat_id, state_json, updated_at)
            VALUES ({p}, {p}, {p})
            ON CONFLICT(chat_id) DO UPDATE SET
                state_json = {ex}.state_json,
                updated_at = {ex}.updated_at
            """,
            (chat_id, json.dumps(state_dict), time.time()),
        )


def load_snapshot(chat_id: int) -> Optional[dict]:
    p = P()
    with _cursor() as cur:
        cur.execute(f"SELECT state_json FROM game_snapshots WHERE chat_id = {p}", (chat_id,))
        row = cur.fetchone()
        return json.loads(row["state_json"]) if row else None


def delete_snapshot(chat_id: int) -> None:
    p = P()
    with _cursor() as cur:
        cur.execute(f"DELETE FROM game_snapshots WHERE chat_id = {p}", (chat_id,))


def list_snapshot_chat_ids() -> list:
    with _cursor() as cur:
        cur.execute("SELECT chat_id FROM game_snapshots")
        return [row["chat_id"] for row in cur.fetchall()]


def backend_name() -> str:
    return _BACKEND
