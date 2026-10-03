import asyncio
import logging
from datetime import timezone

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BusinessConnection, Message

from mentor_bot.store.repo import SRC_AUTO

log = logging.getLogger(__name__)

NO_REPLY_HINT = (
    "⚠️ У бота нет права отвечать от твоего имени — черновики по кнопке и пинги не уйдут. "
    "Разреши: «Telegram для бизнеса» → «Чат-боты» → отвечать на сообщения."
)


ALERT_KEY = "alerted_bconn_unverified"


REPLY_SNIPPET = 300   # сколько текста исходного сообщения брать в контекст ответа


def reply_context(message: Message, mentor_user_id: int) -> str | None:
    """«Ментор: «…»» — на что ученик ответил reply-ем или цитатой. Без этого «а почему так?»
    непонятно, о чём: ответить могли на сообщение недельной давности."""
    src = message.reply_to_message
    if src is None:
        return None
    text = (message.quote.text if message.quote else None) or src.text or src.caption or ""
    text = " ".join(text.split())
    if not text:
        return None   # ответили на стикер или голосовое — пересказать нечего
    mine = src.sender_business_bot is not None or (
        src.from_user is not None and src.from_user.id == mentor_user_id)
    cut = text[:REPLY_SNIPPET] + ("…" if len(text) > REPLY_SNIPPET else "")
    return f"{'Ментор' if mine else 'Ученик'}: «{cut}»"


def can_reply(conn: BusinessConnection) -> bool:
    """Может ли бот писать от имени ментора. В Bot API 9 право лежит в rights, раньше —
    в can_reply самого подключения."""
    if conn.rights is not None:
        return bool(conn.rights.can_reply)
    return bool(conn.can_reply)


async def check_connection(bot, repo, sender, mentor_user_id: int):
    """Старт: живо ли сохранённое подключение. Апдейт business_connection Telegram хранит
    сутки, и после долгого простоя или переезда базы бот может держать мёртвый id."""
    stored = await repo.get_setting("bconn")
    if not stored:
        return   # подключимся по первому сообщению из чата с учеником
    try:
        conn = await bot.get_business_connection(stored)
    except TelegramBadRequest:
        conn = None
    except Exception:
        # сеть или Telegram прилёг — это не повод забывать рабочее подключение
        log.warning("business connection %s not verified on startup", stored, exc_info=True)
        return
    if conn is None or conn.user.id != mentor_user_id or not conn.is_enabled:
        await repo.set_setting("bconn", "")
        await sender.notify_mentor(
            "⚠️ Сохранённое подключение к Telegram для бизнеса больше не действует. Как только "
            "ученик напишет, подключусь заново сам; если нет — выключи и включи бота в "
            "«Telegram для бизнеса» → «Чат-боты»."
        )
    elif not can_reply(conn):
        await sender.notify_mentor(NO_REPLY_HINT)


def make_router(service, repo, mentor_user_id: int) -> Router:
    router = Router()
    adopt_lock = asyncio.Lock()
    foreign: set[str] = set()   # чужие подключения: спрашивать о них Telegram заново незачем

    async def adopt(bot: Bot, conn_id: str) -> bool:
        """Сообщение пришло по подключению, которого бот не знает: апдейт business_connection
        пропал (Telegram хранит его сутки), база новая или ментор переподключал бота, пока тот
        лежал. Спрашиваем у Telegram, чьё оно, и если ментора — запоминаем и работаем дальше."""
        if conn_id in foreign:
            return False
        async with adopt_lock:
            # пачка сообщений приходит разом — Telegram спросит первый, остальные дождутся
            if await repo.get_setting("bconn") == conn_id:
                return True
            try:
                conn = await bot.get_business_connection(conn_id)
            except Exception as e:
                log.warning("business message dropped: connection %s not verified", conn_id,
                            exc_info=True)
                if isinstance(e, TelegramBadRequest) and not await repo.get_setting(ALERT_KEY):
                    # сеть повторится со следующим сообщением сама, а отказ Telegram — нет
                    await repo.set_setting(ALERT_KEY, "1")
                    await service.sender.notify_mentor(
                        "⚠️ Сообщения из чатов приходят по подключению к Telegram для бизнеса, "
                        "которое Telegram не подтверждает, — не разбираю их. Выключи и включи "
                        "бота в «Telegram для бизнеса» → «Чат-боты»."
                    )
                return False
            if conn.user.id != mentor_user_id or not conn.is_enabled:
                log.warning("business message dropped: connection %s belongs to %s (enabled=%s)",
                            conn_id, conn.user.id, conn.is_enabled)
                if conn.user.id != mentor_user_id:
                    foreign.add(conn_id)
                return False
            await repo.set_setting("bconn", conn.id)
            await repo.set_setting(ALERT_KEY, "")
            log.info("business connection %s adopted from an incoming message", conn.id)
            note = "" if can_reply(conn) else "\n\n" + NO_REPLY_HINT
            try:
                await service.sender.notify_mentor(
                    "🔌 Подключение к Telegram для бизнеса восстановлено — снова разбираю "
                    f"сообщения учеников.{note}"
                )
            except Exception:
                log.exception("mentor alert failed after adopting business connection")
            return True

    @router.business_connection()
    async def on_connection(conn: BusinessConnection):
        if conn.user.id != mentor_user_id:
            log.warning(
                "business connection %s belongs to user %s, not mentor — ignored",
                conn.id, conn.user.id,
            )
            return
        if conn.is_enabled:
            await repo.set_setting("bconn", conn.id)
            log.info("business connection %s enabled", conn.id)
            if not can_reply(conn):
                await service.sender.notify_mentor(NO_REPLY_HINT)
        elif await repo.get_setting("bconn") == conn.id:
            # fail-closed: пустой bconn заставляет Sender отказывать в отправке.
            # Выключили старое подключение, а живёт уже новое — его не трогаем
            await repo.set_setting("bconn", "")
            log.info("business connection %s disabled", conn.id)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot):
        if message.sender_business_bot is not None:
            # эхо собственной отправки бота: в переписку её уже записал тот, кто отправлял.
            # Пропустить дальше — значит принять за ручной ответ ментора: закрыть чужие вопросы,
            # снести буфер ученика, обнулить счётчик игнора пингов
            return
        if message.business_connection_id != await repo.get_setting("bconn"):
            if not await adopt(bot, message.business_connection_id):
                return

        text = message.text or message.caption or ""
        peer = message.chat  # личный чат ученика
        username = (peer.username or "").lower()
        ts_iso = message.date.astimezone(timezone.utc).isoformat()
        outgoing = message.from_user is not None and message.from_user.id == mentor_user_id
        direction = "out" if outgoing else "in"
        if not username:
            return  # без username в таблицу не привязать; ученики ментора все с @

        tg_id = message.message_id
        if outgoing and message.is_from_offline:
            # автоответ Business («меня нет», приветствие) или отложенное сообщение: в переписку
            # пишем, но ответом ментора не считаем — иначе «меня нет» снесло бы буфер с вопросом
            if username in service.by_username:
                await repo.log_message(username, "out", text or "[медиа]", ts_iso,
                                       source=SRC_AUTO, tg_id=tg_id)
            return

        if not text:
            if username not in service.by_username:
                return  # неизвестный менти прислал медиа без текста — не за что зацепиться
            await repo.upsert_mentee(username, chat_id=peer.id)
            await service.on_contact_only(username, direction, ts_iso, tg_id=tg_id)
            return

        if username not in service.by_username:
            if await repo.get_setting(f"ignore_chat:{username}") == "1":
                return
            # фиксируем сообщение сразу, чтобы не потерять контакт при рассинхроне кэша
            if not await repo.log_message(username, direction, text, ts_iso, tg_id=tg_id):
                return  # повторная доставка апдейта — второй раз не спрашиваем
            if await repo.get_mentee(username) is None:
                await repo.upsert_mentee(username, chat_id=peer.id)
                await service.on_unknown_chat(username, peer.full_name or username)
            else:
                await repo.upsert_mentee(username, chat_id=peer.id)
            return
        await repo.upsert_mentee(username, chat_id=peer.id)
        if outgoing:
            reply_to = message.reply_to_message
            await service.on_outgoing(username, text, ts_iso, tg_id=tg_id,
                                      reply_to_tg_id=reply_to.message_id if reply_to else None)
        else:
            await service.on_incoming(username, text, ts_iso, tg_id=tg_id,
                                      reply_to=reply_context(message, mentor_user_id))

    return router
