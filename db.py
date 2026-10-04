"""Хранилище бота: SQLite, только стандартная библиотека.

Все функции синхронные. В bot.py они вызываются через asyncio.to_thread,
поэтому бот не зависает при работе с базой.
"""
import sqlite3
import time
from contextlib import closing

DB_PATH = "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subs (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    paid_until   INTEGER NOT NULL DEFAULT 0,  -- unix-время конца подписки
    reminded_for INTEGER NOT NULL DEFAULT 0,  -- для какого paid_until уже отправлено напоминание
    active       INTEGER NOT NULL DEFAULT 0   -- 1 = человек сейчас должен быть в канале
);
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,              -- в копейках
    currency   TEXT NOT NULL,
    charge_id  TEXT NOT NULL UNIQUE,          -- защита от двойной обработки одного платежа
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    admin_msg_id INTEGER PRIMARY KEY,         -- id сообщения у админа
    user_id      INTEGER NOT NULL             -- кому отвечать
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init(path: str | None = None) -> None:
    global DB_PATH
    if path:
        DB_PATH = path
    with closing(_conn()) as c:
        c.executescript(SCHEMA)
        c.commit()


def get_sub(user_id: int) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM subs WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def extend(user_id: int, username: str | None, days: float, now: int | None = None) -> int:
    """Продлевает подписку. Если она ещё действует, дни добавляются к концу, а не к сегодняшнему дню."""
    now = now or int(time.time())
    with closing(_conn()) as c:
        row = c.execute("SELECT paid_until FROM subs WHERE user_id = ?", (user_id,)).fetchone()
        base = max(now, row["paid_until"]) if row else now
        new_until = base + int(days * 86400)
        c.execute(
            """INSERT INTO subs (user_id, username, paid_until, active)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(user_id) DO UPDATE SET
                   username = COALESCE(excluded.username, subs.username),
                   paid_until = excluded.paid_until,
                   active = 1""",
            (user_id, username, new_until),
        )
        c.commit()
        return new_until


def add_payment(user_id: int, amount: int, currency: str, charge_id: str) -> bool:
    """False, если платёж с таким charge_id уже записан (повторная доставка уведомления)."""
    try:
        with closing(_conn()) as c:
            c.execute(
                "INSERT INTO payments (user_id, amount, currency, charge_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, amount, currency, charge_id, int(time.time())),
            )
            c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def due_reminders(now: int, window_sec: int) -> list[dict]:
    """Активные подписки, которые закончатся в ближайшие window_sec, и по ним ещё не было напоминания."""
    with closing(_conn()) as c:
        rows = c.execute(
            """SELECT * FROM subs
               WHERE active = 1 AND paid_until > ? AND paid_until <= ? AND reminded_for != paid_until""",
            (now, now + window_sec),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminded(user_id: int, paid_until: int) -> None:
    with closing(_conn()) as c:
        c.execute("UPDATE subs SET reminded_for = ? WHERE user_id = ?", (paid_until, user_id))
        c.commit()


def expired(now: int) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute("SELECT * FROM subs WHERE active = 1 AND paid_until <= ?", (now,)).fetchall()
        return [dict(r) for r in rows]


def deactivate(user_id: int, zero_time: bool = False) -> None:
    with closing(_conn()) as c:
        if zero_time:
            c.execute("UPDATE subs SET active = 0, paid_until = 0 WHERE user_id = ?", (user_id,))
        else:
            c.execute("UPDATE subs SET active = 0 WHERE user_id = ?", (user_id,))
        c.commit()


def stats(now: int) -> dict:
    with closing(_conn()) as c:
        active = c.execute("SELECT COUNT(*) FROM subs WHERE active = 1 AND paid_until > ?", (now,)).fetchone()[0]
        soon = c.execute(
            "SELECT COUNT(*) FROM subs WHERE active = 1 AND paid_until > ? AND paid_until <= ?",
            (now, now + 7 * 86400),
        ).fetchone()[0]
        paid = c.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments").fetchone()
        return {"active": active, "soon": soon, "payments": paid[0], "total_kopecks": paid[1]}


def save_question(admin_msg_id: int, user_id: int) -> None:
    with closing(_conn()) as c:
        c.execute(
            "INSERT OR REPLACE INTO questions (admin_msg_id, user_id) VALUES (?, ?)",
            (admin_msg_id, user_id),
        )
        c.commit()


def get_question_user(admin_msg_id: int) -> int | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT user_id FROM questions WHERE admin_msg_id = ?", (admin_msg_id,)).fetchone()
        return row["user_id"] if row else None


def get_setting(key: str) -> str | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with closing(_conn()) as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        c.commit()
