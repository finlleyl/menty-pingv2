import os
import aiosqlite

from .db import SCHEMA


class Repo:
    def __init__(self, conn: aiosqlite.Connection):
        self._c = conn

    @classmethod
    async def open(cls, path: str) -> "Repo":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self):
        await self._c.close()

    async def _exec(self, sql: str, args: tuple = ()):
        await self._c.execute(sql, args)
        await self._c.commit()

    async def _one(self, sql: str, args: tuple = ()):
        cur = await self._c.execute(sql, args)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, args: tuple = ()):
        cur = await self._c.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]

    # mentees
    async def upsert_mentee(self, username, chat_id=None, sheet_title=None, row=None):
        await self._exec("INSERT OR IGNORE INTO mentees(username) VALUES (?)", (username,))
        for col, val in (("chat_id", chat_id), ("sheet_title", sheet_title), ("row", row)):
            if val is not None:
                await self._exec(f"UPDATE mentees SET {col}=? WHERE username=?", (val, username))

    async def get_mentee(self, username):
        return await self._one("SELECT * FROM mentees WHERE username=?", (username,))

    async def all_mentees(self):
        return await self._all("SELECT * FROM mentees")

    async def set_pause(self, username, until_iso):
        await self._exec("UPDATE mentees SET paused_until=? WHERE username=?", (until_iso, username))

    async def bump_unanswered(self, username):
        await self._exec(
            "UPDATE mentees SET unanswered_pings=unanswered_pings+1 WHERE username=?", (username,)
        )

    async def reset_unanswered(self, username):
        await self._exec("UPDATE mentees SET unanswered_pings=0 WHERE username=?", (username,))

    # messages
    async def log_message(self, username, direction, text, ts_iso):
        await self._exec(
            "INSERT INTO messages(username, direction, text, ts) VALUES (?,?,?,?)",
            (username, direction, text, ts_iso),
        )

    async def last_message_ts(self, username):
        row = await self._one(
            "SELECT ts FROM messages WHERE username=? ORDER BY ts DESC LIMIT 1", (username,)
        )
        return row["ts"] if row else None

    async def recent_messages(self, username, limit=15):
        rows = await self._all(
            "SELECT direction, text, ts FROM messages WHERE username=? ORDER BY ts DESC LIMIT ?",
            (username, limit),
        )
        return list(reversed(rows))

    # pings
    async def log_ping(self, username, ts_iso, status):
        await self._exec(
            "INSERT INTO pings(username, ts, status) VALUES (?,?,?)", (username, ts_iso, status)
        )

    async def last_ping_ts(self, username):
        row = await self._one(
            "SELECT ts FROM pings WHERE username=? ORDER BY ts DESC LIMIT 1", (username,)
        )
        return row["ts"] if row else None

    # questions
    async def add_question(self, username, question, draft, ts_iso) -> int:
        cur = await self._c.execute(
            "INSERT INTO questions(username, question, draft, created_ts) VALUES (?,?,?,?)",
            (username, question, draft, ts_iso),
        )
        await self._c.commit()
        return cur.lastrowid

    async def get_question(self, qid):
        return await self._one("SELECT * FROM questions WHERE id=?", (qid,))

    async def set_question_state(self, qid, state):
        await self._exec("UPDATE questions SET state=? WHERE id=?", (state, qid))

    async def open_questions(self, older_than_iso=None, unreminded_only=False):
        sql = "SELECT * FROM questions WHERE state='open'"
        args: list = []
        if older_than_iso:
            sql += " AND created_ts < ?"
            args.append(older_than_iso)
        if unreminded_only:
            sql += " AND reminded=0"
        return await self._all(sql, tuple(args))

    async def mark_reminded(self, qid):
        await self._exec("UPDATE questions SET reminded=1 WHERE id=?", (qid,))

    async def close_open_questions(self, username):
        await self._exec(
            "UPDATE questions SET state='answered' WHERE username=? AND state='open'", (username,)
        )

    # proposals
    async def add_proposal(self, username, new_status) -> int:
        cur = await self._c.execute(
            "INSERT INTO proposals(username, new_status) VALUES (?,?)", (username, new_status)
        )
        await self._c.commit()
        return cur.lastrowid

    async def get_proposal(self, pid):
        return await self._one("SELECT * FROM proposals WHERE id=?", (pid,))

    async def delete_proposal(self, pid):
        await self._exec("DELETE FROM proposals WHERE id=?", (pid,))

    # profiles
    async def get_profile(self, username):
        row = await self._one("SELECT summary FROM profiles WHERE username=?", (username,))
        return row["summary"] if row else None

    async def set_profile(self, username, summary, ts_iso):
        await self._exec(
            "INSERT INTO profiles(username, summary, updated_ts) VALUES (?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET summary=excluded.summary, updated_ts=excluded.updated_ts",
            (username, summary, ts_iso),
        )

    # settings
    async def get_setting(self, key, default=None):
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key, value):
        await self._exec(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
