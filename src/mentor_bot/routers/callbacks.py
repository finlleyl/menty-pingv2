import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

log = logging.getLogger(__name__)


async def handle_q_callback(data: str, repo, sender, service) -> str:
    _, action, qid = data.split(":")
    q = await repo.get_question(int(qid))
    if not q or q["state"] != "open":
        return "Уже обработано"
    if action == "send":
        try:
            result = await sender.send_to_mentee(q["username"], q["draft"])
        except Exception:
            log.exception("send_to_mentee failed for @%s", q["username"])
            return "Ошибка отправки, попробуй ещё раз"
        if result in ("sent", "dry"):
            await repo.set_question_state(q["id"], "sent")
            return "Отправлено" if result == "sent" else "Dry-run: ушло тебе"
        return f"Не отправлено: {result}"
    await repo.set_question_state(q["id"], "ignored")
    return "Ок, игнорирую"


async def handle_st_callback(data: str, repo, sender, service) -> str:
    _, action, pid = data.split(":")
    p = await repo.get_proposal(int(pid))
    if not p:
        return "Уже обработано"
    if action == "no":
        await repo.delete_proposal(p["id"])
        return "Ок, статус не трогаю"
    m = service.by_username.get(p["username"])
    if m is None:
        await repo.delete_proposal(p["id"])
        return "Менти не найден в таблице"
    try:
        await service.sheets.set_status(m, p["new_status"])
    except Exception:
        return "Ошибка записи в таблицу, нажми ещё раз"
    m.status = p["new_status"]
    await repo.set_status_since(p["username"], datetime.now(timezone.utc).isoformat())
    await repo.delete_proposal(p["id"])
    return f"Статус @{p['username']} → «{p['new_status']}»"


async def handle_add_callback(data: str, repo, sender, service) -> str:
    _, idx, username = data.split(":")
    if idx == "skip":
        await repo.set_setting(f"ignore_chat:{username}", "1")
        return "Ок, не менти"
    if username in service.by_username:
        return "Уже в таблице"
    titles = service.settings.active_sheet_titles
    if not idx.isdigit() or int(idx) >= len(titles):
        return "Кнопка устарела"
    title = titles[int(idx)]
    await service.sheets.append_mentee(title, f"@{username}")
    await service.sync_mentees()
    return f"Добавил @{username} в «{title}»"


def make_router(service, repo, sender) -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("q:"))
    async def on_q(cb: CallbackQuery):
        await cb.answer(await handle_q_callback(cb.data, repo, sender, service))

    @router.callback_query(F.data.startswith("st:"))
    async def on_st(cb: CallbackQuery):
        await cb.answer(await handle_st_callback(cb.data, repo, sender, service))

    @router.callback_query(F.data.startswith("add:"))
    async def on_add(cb: CallbackQuery):
        await cb.answer(await handle_add_callback(cb.data, repo, sender, service))

    return router
