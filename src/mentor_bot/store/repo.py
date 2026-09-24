import json
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
        await cls._migrate(conn)
        await conn.commit()
        return cls(conn)

    @staticmethod
    async def _migrate(conn):
        """Догоняем схему на базах, созданных прошлыми версиями."""
        for table, col in (
            ("mentees", "status_since"), ("mentees", "last_status"),
            ("proposals", "from_status"), ("questions", "final"), ("questions", "emb"),
        ):
            cur = await conn.execute(f"PRAGMA table_info({table})")
            if col not in {row[1] for row in await cur.fetchall()}:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

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

    async def set_status_since(self, username, ts_iso):
        """Момент, когда ментор подтвердил текущий статус. От него считается ожидание."""
        await self._exec("INSERT OR IGNORE INTO mentees(username) VALUES (?)", (username,))
        await self._exec("UPDATE mentees SET status_since=? WHERE username=?", (ts_iso, username))

    async def record_status(self, username, status, ts_iso, source) -> bool:
        """Фиксирует статус, увиденный в таблице (source='sheet') или подтверждённый кнопкой
        (source='bot'). Смена пишется в status_history и сдвигает status_since.

        Первое наблюдение из таблицы — не переход: когда ученик туда попал, неизвестно,
        поэтому status_since не трогаем, а событие помечаем 'initial'.
        True — был настоящий переход."""
        status = (status or "").strip()
        await self._exec("INSERT OR IGNORE INTO mentees(username) VALUES (?)", (username,))
        rec = await self.get_mentee(username)
        prev = rec["last_status"]
        if prev == status:
            return False
        initial = prev is None and source == "sheet"
        await self._exec(
            "INSERT INTO status_history(username, from_status, to_status, ts, source) "
            "VALUES (?,?,?,?,?)",
            (username, prev, status, ts_iso, "initial" if initial else source),
        )
        if initial:
            await self._exec("UPDATE mentees SET last_status=? WHERE username=?", (status, username))
            return False
        await self._exec(
            "UPDATE mentees SET last_status=?, status_since=? WHERE username=?",
            (status, ts_iso, username),
        )
        return True

    async def status_history(self, username=None):
        if username is None:
            return await self._all("SELECT * FROM status_history ORDER BY username, ts, id")
        return await self._all(
            "SELECT * FROM status_history WHERE username=? ORDER BY ts, id", (username,)
        )

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

    async def last_out_ts(self, username):
        row = await self._one(
            "SELECT ts FROM messages WHERE username=? AND direction='out' "
            "ORDER BY ts DESC LIMIT 1",
            (username,),
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
    async def add_question(self, username, question, draft, ts_iso, emb=None) -> int:
        cur = await self._c.execute(
            "INSERT INTO questions(username, question, draft, created_ts, emb) VALUES (?,?,?,?,?)",
            (username, question, draft, ts_iso, json.dumps(emb) if emb is not None else None),
        )
        await self._c.commit()
        return cur.lastrowid

    async def get_question(self, qid):
        return await self._one("SELECT * FROM questions WHERE id=?", (qid,))

    async def set_question_state(self, qid, state):
        await self._exec("UPDATE questions SET state=? WHERE id=?", (state, qid))

    async def set_question_final(self, qid, final):
        """Что в итоге ушло ученику: черновик как есть или правка ментора."""
        await self._exec("UPDATE questions SET final=? WHERE id=?", (final, qid))

    async def record_manual_answer(self, username, text):
        """Ментор ответил в чате сам — это и есть ответ на открытые вопросы."""
        await self._exec(
            "UPDATE questions SET final=? WHERE username=? AND state='open' AND final IS NULL",
            (text, username),
        )

    async def edit_examples(self, limit=5):
        """Последние случаи, когда ментор ответил не так, как предлагал черновик."""
        return await self._all(
            "SELECT question, draft, final FROM questions "
            "WHERE final IS NOT NULL AND final != draft ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    async def answered_questions(self, limit=500):
        """Вопросы с настоящим ответом ментора и эмбеддингом вопроса — для поиска похожих."""
        rows = await self._all(
            "SELECT question, final, emb FROM questions "
            "WHERE final IS NOT NULL AND emb IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            r["emb"] = json.loads(r["emb"])
        return rows

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
    async def add_proposal(self, username, new_status, from_status=None) -> int:
        """from_status — статус на момент предложения; None — не сверять (старые записи)."""
        cur = await self._c.execute(
            "INSERT INTO proposals(username, new_status, from_status) VALUES (?,?,?)",
            (username, new_status, from_status),
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

    async def stale_profiles(self):
        """Кому пора обновить досье: есть сообщения новее последнего обновления."""
        rows = await self._all(
            "SELECT m.username AS username FROM (SELECT DISTINCT username FROM messages) m "
            "LEFT JOIN profiles p ON p.username = m.username "
            "WHERE p.updated_ts IS NULL OR p.updated_ts < "
            "(SELECT MAX(ts) FROM messages WHERE username = m.username)"
        )
        return [r["username"] for r in rows]

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

    # pending — буфер входящих до дебаунса
    async def buffer_incoming(self, username, text, ts_iso):
        row = await self._one("SELECT texts FROM pending WHERE username=?", (username,))
        texts = json.loads(row["texts"]) if row else []
        texts.append(text)
        await self._exec(
            "INSERT INTO pending(username, last_in_ts, texts) VALUES (?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "last_in_ts=excluded.last_in_ts, texts=excluded.texts",
            (username, ts_iso, json.dumps(texts, ensure_ascii=False)),
        )

    async def touch_pending(self, username, ts_iso):
        """Продлить окно, не добавляя текст (медиа без подписи). Нет буфера — no-op."""
        await self._exec("UPDATE pending SET last_in_ts=? WHERE username=?", (ts_iso, username))

    async def get_pending(self, username):
        return await self._one("SELECT * FROM pending WHERE username=?", (username,))

    async def drop_pending(self, username):
        await self._exec("DELETE FROM pending WHERE username=?", (username,))

    async def consume_pending(self, username, consumed: int):
        """Снять с буфера первые `consumed` сообщений.

        Пока дренаж ждал ответа модели, ученик мог дописать ещё — их допишет
        buffer_incoming в ту же строку. Удалять строку целиком нельзя (потеряем
        свежее), оставлять целиком тоже (обработанное уйдёт в модель второй раз).
        """
        row = await self._one("SELECT texts FROM pending WHERE username=?", (username,))
        if row is None:
            return
        rest = json.loads(row["texts"])[consumed:]
        if rest:
            await self._exec(
                "UPDATE pending SET texts=? WHERE username=?",
                (json.dumps(rest, ensure_ascii=False), username),
            )
        else:
            await self._exec("DELETE FROM pending WHERE username=?", (username,))

    async def mature_pending(self, before_iso):
        return await self._all(
            "SELECT * FROM pending WHERE last_in_ts < ? ORDER BY last_in_ts", (before_iso,)
        )

    # llm_usage — учёт токенов и денег
    async def log_usage(self, ts_iso, task, model, prompt_tokens, completion_tokens, cost):
        await self._exec(
            "INSERT INTO llm_usage(ts, task, model, prompt_tokens, completion_tokens, cost) "
            "VALUES (?,?,?,?,?,?)",
            (ts_iso, task, model, prompt_tokens, completion_tokens, cost),
        )

    async def usage_summary(self, since_iso):
        return await self._all(
            "SELECT task, COUNT(*) AS calls, SUM(prompt_tokens) AS prompt_tokens, "
            "SUM(completion_tokens) AS completion_tokens, SUM(cost) AS cost, "
            "COUNT(cost) AS priced FROM llm_usage WHERE ts >= ? GROUP BY task ORDER BY cost DESC, calls DESC",
            (since_iso,),
        )

    async def backup_to(self, path: str):
        """Консистентная копия базы через то же соединение (без гонки с записями)."""
        if os.path.exists(path):
            os.remove(path)
        await self._c.execute("VACUUM INTO ?", (path,))
