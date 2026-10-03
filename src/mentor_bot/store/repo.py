import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta

import aiosqlite
import numpy as np

from .db import SCHEMA

MIN_ANSWER_CHARS = 40       # короче — «ок, щас гляну», а не ответ
ANSWER_WINDOW_HOURS = 6     # сколько после вопроса ждём настоящего ответа ментора

# questions.kind — группа черновика. 'question' — в сообщении есть вопрос (технический или
# организационный), ответ опирается на материалы; 'human' — только переживания или успех.
# Группы не смешиваем в примерах правок: правка тёплого ответа не учит отвечать про каналы.
KIND_QUESTION = "question"
KIND_HUMAN = "human"

# messages.source — откуда сообщение. Ответом ментора «в чате» считается только SRC_CHAT:
# отправки самого бота и автоответы Business не закрывают вопросы и не учат черновики
SRC_CHAT = "chat"            # написано руками в Telegram — ментором или учеником
SRC_BOT = "bot"              # текст модели, отправленный ботом: пинг, черновик как есть
SRC_BOT_EDIT = "bot_edit"    # правка ментора, отправленная ботом
SRC_AUTO = "auto"            # автоответ Telegram Business или отложенное сообщение

# Образцы стиля ментора: короче — «ок», длиннее — простыня, по которой тон не поймать
STYLE_MIN_CHARS = 15
STYLE_MAX_CHARS = 600


def pack_emb(emb) -> bytes:
    """Вектор → float32 BLOB: в 5–6 раз компактнее JSON-текста, бэкап и память меньше."""
    return np.asarray(emb, dtype=np.float32).tobytes()


def unpack_emb(value):
    """BLOB (или JSON-текст старого формата) → np.ndarray; None — вектора нет."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(value), dtype=np.float32)
    return np.asarray(json.loads(value), dtype=np.float32)


async def _has_column(conn, table, col) -> bool:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return col in {row[1] for row in await cur.fetchall()}


async def _add_column(conn, table, col, decl):
    if not await _has_column(conn, table, col):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# Миграции — шаги схемы по порядку; номер последнего применённого лежит в PRAGMA user_version.
# Каждый шаг безопасен и для свежей базы (SCHEMA уже создала всё нужное), и для старой.
# SCHEMA выполняется раньше миграций, поэтому индексы по колонкам, которые добавляет
# миграция, создаёт сама миграция, а не SCHEMA: на старой базе этих колонок ещё нет.

async def _m1_legacy_columns(conn):
    """Колонки, которые прошлые версии догоняли при каждом старте."""
    for table, col, decl in (
        ("mentees", "status_since", "TEXT"), ("mentees", "last_status", "TEXT"),
        ("proposals", "from_status", "TEXT"), ("questions", "final", "TEXT"),
        ("questions", "emb", "TEXT"),
        # до разбора эмоций все черновики были ответами на вопросы — отсюда и DEFAULT
        ("questions", "kind", f"TEXT NOT NULL DEFAULT '{KIND_QUESTION}'"),
        ("messages", "source", f"TEXT NOT NULL DEFAULT '{SRC_CHAT}'"),
    ):
        await _add_column(conn, table, col, decl)


async def _m2_pending_rows(conn):
    """Буфер старого формата (JSON-список в pending.texts) — в построчный pending_messages."""
    cur = await conn.execute(
        "SELECT username, last_in_ts, texts FROM pending WHERE texts NOT IN ('', '[]')"
    )
    for username, last_in_ts, texts in await cur.fetchall():
        try:
            old = json.loads(texts)
        except ValueError:
            old = []      # битый буфер не должен ронять старт бота
        for text in old:
            await conn.execute(
                "INSERT INTO pending_messages(username, text, ts) VALUES (?,?,?)",
                (username, text, last_in_ts),
            )
        await conn.execute("UPDATE pending SET texts='[]' WHERE username=?", (username,))


async def _m3_embeddings(conn):
    """Эмбеддинги — float32 BLOB с меткой модели. Раньше после смены EMBED_MODEL векторы разной
    длины сравнивались друг с другом, и падали поиск похожих ответов, /fails и сводка."""
    for table in ("questions", "interview_notes"):
        await _add_column(conn, table, "emb_model", "TEXT")
        cur = await conn.execute(f"SELECT id, emb FROM {table} WHERE typeof(emb)='text'")
        for rid, emb in await cur.fetchall():
            try:
                blob = pack_emb(json.loads(emb))
            except ValueError:
                blob = None
            await conn.execute(f"UPDATE {table} SET emb=? WHERE id=?", (blob, rid))


async def _m4_message_ids(conn):
    """message_id Telegram: защита от повторно доставленных апдейтов, связь ответа ментора
    с вопросом по reply и контекст «на что ответил ученик»."""
    await _add_column(conn, "messages", "tg_id", "INTEGER")
    await _add_column(conn, "messages", "reply_to_tg_id", "INTEGER")
    await _add_column(conn, "pending_messages", "tg_id", "INTEGER")
    await _add_column(conn, "pending_messages", "reply_to", "TEXT")
    await _add_column(conn, "questions", "msg_ids", "TEXT")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_tg ON messages(username, tg_id) "
        "WHERE tg_id IS NOT NULL"
    )


MIGRATIONS = (_m1_legacy_columns, _m2_pending_rows, _m3_embeddings, _m4_message_ids)


class Repo:
    def __init__(self, conn: aiosqlite.Connection):
        self._c = conn
        # Соединение одно на всё приложение, а хендлеры и джобы пишут конкурентно. Без замка
        # commit одной корутины фиксировал бы полузаписанную транзакцию другой
        self._write = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> "Repo":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        # WAL: чтение (бэкап, sqlite3 или Datasette рядом с ботом) не ждёт записи и не мешает ей;
        # busy_timeout — подождать занятый файл, а не упасть сразу с «database is locked»
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        await cls._migrate(conn)
        return cls(conn)

    @staticmethod
    async def _migrate(conn):
        cur = await conn.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
            await step(conn)
            await conn.execute(f"PRAGMA user_version={number}")
            await conn.commit()
        await conn.commit()

    async def close(self):
        await self._c.close()

    async def _exec(self, sql: str, args: tuple = ()):
        async with self._write:
            cur = await self._c.execute(sql, args)
            await self._c.commit()
            return cur

    @asynccontextmanager
    async def _tx(self):
        """Несколько записей одним целым: либо все, либо ни одной. Внутри — только
        self._c напрямую: методы с _exec возьмут тот же замок и повиснут."""
        async with self._write:
            try:
                yield self._c
            except BaseException:
                await self._c.rollback()
                raise
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
        async with self._tx() as c:
            await c.execute("INSERT OR IGNORE INTO mentees(username) VALUES (?)", (username,))
            for col, val in (("chat_id", chat_id), ("sheet_title", sheet_title), ("row", row)):
                if val is not None:
                    await c.execute(f"UPDATE mentees SET {col}=? WHERE username=?", (val, username))

    async def get_mentee(self, username):
        return await self._one("SELECT * FROM mentees WHERE username=?", (username,))

    async def all_mentees(self):
        return await self._all("SELECT * FROM mentees")

    async def set_status_since(self, username, ts_iso):
        """Момент, когда ментор подтвердил текущий статус. От него считается ожидание."""
        async with self._tx() as c:
            await c.execute("INSERT OR IGNORE INTO mentees(username) VALUES (?)", (username,))
            await c.execute("UPDATE mentees SET status_since=? WHERE username=?", (ts_iso, username))

    async def record_status(self, username, status, ts_iso, source) -> bool:
        """Фиксирует статус, увиденный в таблице (source='sheet') или подтверждённый кнопкой
        (source='bot'). Смена пишется в status_history и сдвигает status_since.

        Первое наблюдение из таблицы — не переход: когда ученик туда попал, неизвестно,
        поэтому status_since не трогаем, а событие помечаем 'initial'.
        True — был настоящий переход."""
        from mentor_bot.stages import parse_stage
        status = (status or "").strip()
        # чтение и запись — одной транзакцией: синк таблицы и кнопка «Да» иначе читали бы один
        # и тот же прошлый статус и писали в историю два перехода
        async with self._tx() as c:
            await c.execute("INSERT OR IGNORE INTO mentees(username) VALUES (?)", (username,))
            cur = await c.execute("SELECT last_status FROM mentees WHERE username=?", (username,))
            prev = (await cur.fetchone())[0]
            if prev == status:
                return False
            if not status and prev:
                return False  # ячейку на минуту очистили — это не смена стадии
            initial = prev is None and source == "sheet"
            await c.execute(
                "INSERT INTO status_history(username, from_status, to_status, ts, source) "
                "VALUES (?,?,?,?,?)",
                (username, prev, status, ts_iso, "initial" if initial else source),
            )
            if initial or parse_stage(prev) == parse_stage(status):
                # первое наблюдение или переименование («3 спринт» → «Спринт 3»): таймер стадии не сбрасываем
                await c.execute("UPDATE mentees SET last_status=? WHERE username=?", (status, username))
                return False
            await c.execute(
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
    async def log_message(self, username, direction, text, ts_iso, source=SRC_CHAT,
                          tg_id=None, reply_to_tg_id=None) -> bool:
        """False — это сообщение (тот же message_id в том же чате) уже записано: Telegram
        доставил апдейт повторно, обрабатывать его второй раз нельзя."""
        cur = await self._exec(
            "INSERT OR IGNORE INTO messages(username, direction, text, ts, source, tg_id, "
            "reply_to_tg_id) VALUES (?,?,?,?,?,?,?)",
            (username, direction, text, ts_iso, source, tg_id, reply_to_tg_id),
        )
        return cur.rowcount == 1

    async def last_message_ts(self, username):
        row = await self._one(
            "SELECT ts FROM messages WHERE username=? ORDER BY ts DESC LIMIT 1", (username,)
        )
        return row["ts"] if row else None

    async def last_out_ts(self, username):
        """Когда ментор последний раз написал ученику сам, в Telegram. Отправки бота (черновик
        по кнопке, пинг) и автоответы Business не в счёт: это не «ментор ответил сам»."""
        row = await self._one(
            "SELECT ts FROM messages WHERE username=? AND direction='out' "
            f"AND source='{SRC_CHAT}' ORDER BY ts DESC LIMIT 1",
            (username,),
        )
        return row["ts"] if row else None

    async def last_in_ts(self, username):
        row = await self._one(
            "SELECT ts FROM messages WHERE username=? AND direction='in' "
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
    async def add_question(self, username, question, draft, ts_iso, emb=None,
                           kind=KIND_QUESTION, emb_model=None, msg_ids=None) -> int:
        cur = await self._exec(
            "INSERT INTO questions(username, question, draft, created_ts, emb, kind, emb_model, "
            "msg_ids) VALUES (?,?,?,?,?,?,?,?)",
            (username, question, draft, ts_iso, pack_emb(emb) if emb is not None else None,
             kind, emb_model, json.dumps(msg_ids) if msg_ids else None),
        )
        return cur.lastrowid

    async def question_for_message(self, username, tg_id):
        """Вопрос, собранный из сообщения ученика с этим message_id (ментор ответил на него
        reply-ем). Свежий и ещё без ответа ментора — None, если такого нет."""
        return await self._one(
            "SELECT * FROM questions WHERE username=? AND state IN ('open', 'answered') "
            "AND msg_ids IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM json_each(questions.msg_ids) WHERE value = ?) "
            "ORDER BY id DESC LIMIT 1",
            (username, tg_id),
        )

    async def get_question(self, qid):
        return await self._one("SELECT * FROM questions WHERE id=?", (qid,))

    async def set_question_state(self, qid, state):
        await self._exec("UPDATE questions SET state=? WHERE id=?", (state, qid))

    async def set_question_final(self, qid, final):
        """Что в итоге ушло ученику: черновик как есть или правка ментора."""
        await self._exec("UPDATE questions SET final=? WHERE id=?", (final, qid))

    async def record_manual_answer(self, username, text, now_utc, question_id=None):
        """Ментор ответил в чате сам — это и есть ответ на вопрос. question_id — ответил
        reply-ем на сообщение этого вопроса: пишем только в него, а не во все за окно.

        Короткое «щас гляну» ответом не считаем: вопрос оно закроет (close_open_questions),
        а настоящий ответ, пришедший следом в пределах окна, запишется уже в закрытый вопрос."""
        if len(text.strip()) < MIN_ANSWER_CHARS:
            return
        if question_id is not None:
            await self._exec(
                "UPDATE questions SET final=? WHERE id=? AND final IS NULL", (text, question_id)
            )
            return
        since = (now_utc - timedelta(hours=ANSWER_WINDOW_HOURS)).isoformat()
        await self._exec(
            "UPDATE questions SET final=? WHERE username=? AND final IS NULL "
            "AND state IN ('open', 'answered') AND created_ts >= ?",
            (text, username, since),
        )

    async def edit_examples(self, limit=5, kind=None):
        """Последние случаи, когда ментор ответил не так, как предлагал черновик.
        kind — только из этой группы черновиков (None — из всех)."""
        sql = "SELECT question, draft, final FROM questions WHERE final IS NOT NULL AND final != draft"
        args: tuple = ()
        if kind is not None:
            sql += " AND kind=?"
            args = (kind,)
        return await self._all(sql + " ORDER BY id DESC LIMIT ?", args + (limit,))

    async def style_samples(self, limit=10, per_user=2, scan=400):
        """Настоящие сообщения ментора ученикам — образец тона для модели.

        Только ученикам из таблицы: Business-подключение видит все личные чаты, и переписка
        с незнакомым чатом пишется в messages, пока ментор не нажал «Не менти» (у таких
        sheet_title пуст). Личным сообщениям ментора не место ни в промпте, ни у провайдера.
        Пинги, которые отправил бот, и черновики, ушедшие как есть, — это текст модели, а не
        ментора: учиться на них — значит закреплять ту самую «нейронистость». Не больше
        per_user на ученика, чтобы один длинный диалог не задавал тон за всех."""
        from mentor_bot.style import call_hits
        rows = await self._all(
            "SELECT m.username, m.text FROM messages m "
            "WHERE m.direction='out' AND m.text != '[медиа]' AND length(m.text) BETWEEN ? AND ? "
            f"AND m.source IN ('{SRC_CHAT}', '{SRC_BOT_EDIT}') "
            "AND m.username IN (SELECT username FROM mentees WHERE sheet_title IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM pings p WHERE p.username=m.username "
            "AND p.ts=m.ts AND p.status='sent') "
            "AND NOT EXISTS (SELECT 1 FROM questions q WHERE q.draft=m.text) "
            "ORDER BY m.ts DESC, m.id DESC LIMIT ?",
            (STYLE_MIN_CHARS, STYLE_MAX_CHARS, scan),
        )
        out, seen, per = [], set(), {}
        for r in rows:
            key = " ".join(r["text"].lower().split())
            if key in seen or per.get(r["username"], 0) >= per_user:
                continue
            # «го созвон в 8 по спринту?» — законно по регламенту, но с пометкой «копируй
            # лексику» модель начнёт звать на созвон в каждом черновике и пинге
            if call_hits(r["text"]):
                continue
            seen.add(key)
            per[r["username"]] = per.get(r["username"], 0) + 1
            out.append(r["text"].strip())
            if len(out) >= limit:
                break
        return out

    async def answered_questions(self, limit=500, model=None):
        """Вопросы с настоящим ответом ментора и эмбеддингом вопроса — для поиска похожих.
        model — только векторы этой модели (и старые, без метки): чужие с ними не сравнить.

        Технический черновик, отправленный как есть, — ответ, который ментор проверил: факты
        в нём годятся. А тёплый ответ без правки — это тон модели, и подсовывать его как
        «так ответил ментор» значит учить нейронку на самой себе (как и в style_samples)."""
        rows = await self._all(
            "SELECT question, final, emb FROM questions "
            "WHERE final IS NOT NULL AND emb IS NOT NULL "
            f"AND (kind != '{KIND_HUMAN}' OR final != draft) "
            "AND (? IS NULL OR emb_model IS NULL OR emb_model = ?) ORDER BY id DESC LIMIT ?",
            (model, model, limit),
        )
        for r in rows:
            r["emb"] = unpack_emb(r["emb"])
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

    async def close_open_questions(self, username, question_id=None):
        """question_id — закрыть только этот вопрос (ментор ответил reply-ем на него)."""
        if question_id is not None:
            await self._exec(
                "UPDATE questions SET state='answered' WHERE id=? AND state='open'", (question_id,)
            )
            return
        await self._exec(
            "UPDATE questions SET state='answered' WHERE username=? AND state='open'", (username,)
        )

    # proposals
    async def add_proposal(self, username, new_status, from_status=None) -> int:
        """from_status — статус на момент предложения; None — не сверять (старые записи)."""
        cur = await self._exec(
            "INSERT INTO proposals(username, new_status, from_status) VALUES (?,?,?)",
            (username, new_status, from_status),
        )
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

    # pending — буфер входящих до дебаунса. Окно (last_in_ts) — в pending, сами сообщения —
    # построчно в pending_messages: aiogram разбирает апдейты параллельно, и чтение-изменение-
    # запись одного JSON-списка теряло сообщения, пришедшие пачкой
    async def buffer_incoming(self, username, text, ts_iso, tg_id=None, reply_to=None):
        async with self._tx() as c:
            await c.execute(
                "INSERT INTO pending(username, last_in_ts, texts) VALUES (?,?,'[]') "
                "ON CONFLICT(username) DO UPDATE SET last_in_ts=MAX(last_in_ts, excluded.last_in_ts)",
                (username, ts_iso),
            )
            await c.execute(
                "INSERT INTO pending_messages(username, text, ts, tg_id, reply_to) VALUES (?,?,?,?,?)",
                (username, text, ts_iso, tg_id, reply_to),
            )

    async def touch_pending(self, username, ts_iso):
        """Продлить окно, не добавляя текст (медиа без подписи). Нет буфера — no-op."""
        await self._exec(
            "UPDATE pending SET last_in_ts=MAX(last_in_ts, ?) WHERE username=? "
            "AND EXISTS (SELECT 1 FROM pending_messages WHERE username=?)",
            (ts_iso, username, username),
        )

    async def get_pending(self, username):
        """{"last_in_ts", "texts": [...]} или None, если буфер пуст."""
        rows = await self._all(
            "SELECT text, ts FROM pending_messages WHERE username=? ORDER BY id", (username,)
        )
        if not rows:
            return None
        window = await self._one("SELECT last_in_ts FROM pending WHERE username=?", (username,))
        last = max([r["ts"] for r in rows] + ([window["last_in_ts"]] if window else []))
        return {"username": username, "last_in_ts": last, "texts": [r["text"] for r in rows]}

    async def drop_pending(self, username):
        async with self._tx() as c:
            await c.execute("DELETE FROM pending_messages WHERE username=?", (username,))
            await c.execute("DELETE FROM pending WHERE username=?", (username,))

    async def consume_pending(self, username, upto_id: int):
        """Снять с буфера разобранное — сообщения с id не больше upto_id.

        Пока дренаж ждал ответа модели, ученик мог дописать ещё, а ментор — ответить сам
        (drop_pending) и ученик — написать снова. Снимаем по id, а не «первые N»: дописанное
        после разбора останется, даже если буфер успели удалить и завести заново."""
        async with self._tx() as c:
            await c.execute(
                "DELETE FROM pending_messages WHERE username=? AND id<=?", (username, upto_id)
            )
            await c.execute(
                "DELETE FROM pending WHERE username=? "
                "AND NOT EXISTS (SELECT 1 FROM pending_messages WHERE username=?)",
                (username, username),
            )

    async def mature_pending(self, before_iso):
        """Буферы, в которые ученик не писал с before_iso: [{username, last_in_ts, max_id, texts,
        tg_ids, replies}]. max_id — до какого сообщения снимать после разбора (consume_pending);
        replies — на что ученик отвечал (без повторов), tg_ids — message_id сообщений буфера."""
        # LEFT JOIN: сообщение без строки окна (её успел снять consume) всё равно разберётся
        windows = await self._all(
            "SELECT m.username, MAX(m.id) AS max_id, "
            "MAX(MAX(m.ts), COALESCE(p.last_in_ts, '')) AS last_in_ts "
            "FROM pending_messages m LEFT JOIN pending p ON p.username = m.username "
            "GROUP BY m.username HAVING MAX(MAX(m.ts), COALESCE(p.last_in_ts, '')) < ? "
            "ORDER BY last_in_ts",
            (before_iso,),
        )
        for w in windows:
            rows = await self._all(
                "SELECT text, tg_id, reply_to FROM pending_messages WHERE username=? AND id<=? "
                "ORDER BY id",
                (w["username"], w["max_id"]),
            )
            w["texts"] = [r["text"] for r in rows]
            w["tg_ids"] = [r["tg_id"] for r in rows if r["tg_id"] is not None]
            w["replies"] = list(dict.fromkeys(r["reply_to"] for r in rows if r["reply_to"]))
        return windows

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
        async with self._write:
            await self._c.commit()   # VACUUM INTO не работает внутри открытой транзакции
            await self._c.execute("VACUUM INTO ?", (path,))

    # ping_drafts — пинги, ждущие решения ментора (режим review или провал проверки)
    async def add_ping_draft(self, username, text, ts_iso) -> int:
        cur = await self._exec(
            "INSERT INTO ping_drafts(username, text, created_ts) VALUES (?,?,?)",
            (username, text, ts_iso),
        )
        return cur.lastrowid

    async def get_ping_draft(self, pid):
        return await self._one("SELECT * FROM ping_drafts WHERE id=?", (pid,))

    async def open_ping_draft(self, username):
        return await self._one(
            "SELECT * FROM ping_drafts WHERE username=? AND state='open' ORDER BY id DESC LIMIT 1",
            (username,),
        )

    async def claim(self, table, rid, state="sending") -> bool:
        """Атомарно забрать запись open → state. Два быстрых нажатия «Отправить» обрабатываются
        конкурентно; отправит только тот, чей UPDATE реально сменил состояние."""
        assert table in ("ping_drafts", "questions")
        cur = await self._exec(
            f"UPDATE {table} SET state=? WHERE id=? AND state='open'", (state, rid)
        )
        return cur.rowcount == 1

    async def set_ping_draft_state(self, pid, state):
        await self._exec("UPDATE ping_drafts SET state=? WHERE id=?", (state, pid))

    async def close_ping_drafts(self, username, state="stale"):
        await self._exec(
            "UPDATE ping_drafts SET state=? WHERE username=? AND state='open'", (state, username)
        )

    # interview_notes — вопросы с собесов, которые пересказал ученик
    async def add_interview_notes(self, username, source_ts, items, embs, emb_model=None):
        async with self._tx() as c:
            for it, emb in zip(items, embs):
                await c.execute(
                    "INSERT INTO interview_notes(username, source_ts, company, stage, question, "
                    "failed, emb, emb_model) VALUES (?,?,?,?,?,?,?,?)",
                    (username, source_ts, it.company, it.stage, it.question, int(it.failed),
                     pack_emb(emb), emb_model),
                )

    async def interview_questions(self, failed_only=True, since_iso=None, model=None):
        """model — только векторы этой модели (и старые, без метки)."""
        sql = "SELECT * FROM interview_notes WHERE emb IS NOT NULL"
        args: list = []
        if model:
            sql += " AND (emb_model IS NULL OR emb_model = ?)"
            args.append(model)
        if failed_only:
            sql += " AND failed=1"
        if since_iso:
            sql += " AND source_ts >= ?"
            args.append(since_iso)
        rows = await self._all(sql + " ORDER BY id", tuple(args))
        for r in rows:
            r["emb"] = unpack_emb(r["emb"])
        return rows
