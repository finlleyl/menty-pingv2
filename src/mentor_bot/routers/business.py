import logging
from datetime import timezone

from aiogram import Router
from aiogram.types import BusinessConnection, Message

log = logging.getLogger(__name__)


def make_router(service, repo, mentor_user_id: int) -> Router:
    router = Router()

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
        else:
            # fail-closed: пустой bconn заставляет Sender отказывать в отправке
            await repo.set_setting("bconn", "")
            log.info("business connection %s disabled", conn.id)

    @router.business_message()
    async def on_business_message(message: Message):
        stored = await repo.get_setting("bconn")
        if not stored or message.business_connection_id != stored:
            # рассинхрон id (переподключение при переносе БД) или чужой канал —
            # сообщение не обрабатываем, но громко пишем в лог, чтобы это было видно
            log.warning(
                "business message dropped: connection %s != stored %s "
                "(если это твой канал — выключи/включи бота в Telegram Business)",
                message.business_connection_id, stored or "<пусто>",
            )
            return

        text = message.text or message.caption or ""
        peer = message.chat  # личный чат ученика
        username = (peer.username or "").lower()
        ts_iso = message.date.astimezone(timezone.utc).isoformat()
        outgoing = message.from_user is not None and message.from_user.id == mentor_user_id
        direction = "out" if outgoing else "in"
        if not username:
            return  # без username в таблицу не привязать; ученики ментора все с @

        if not text:
            if username not in service.by_username:
                return  # неизвестный менти прислал медиа без текста — не за что зацепиться
            await repo.upsert_mentee(username, chat_id=peer.id)
            await service.on_contact_only(username, direction, ts_iso)
            return

        if username not in service.by_username:
            if await repo.get_setting(f"ignore_chat:{username}") == "1":
                return
            # фиксируем сообщение сразу, чтобы не потерять контакт при рассинхроне кэша
            await repo.log_message(username, direction, text, ts_iso)
            if await repo.get_mentee(username) is None:
                await repo.upsert_mentee(username, chat_id=peer.id)
                await service.on_unknown_chat(username, peer.full_name or username)
            else:
                await repo.upsert_mentee(username, chat_id=peer.id)
            return
        await repo.upsert_mentee(username, chat_id=peer.id)
        if outgoing:
            await service.on_outgoing(username, text, ts_iso)
        else:
            await service.on_incoming(username, text, ts_iso)

    return router
