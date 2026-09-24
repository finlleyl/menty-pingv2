import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

from mentor_bot.sheets import RowNotFound, StatusConflict

log = logging.getLogger(__name__)


EDIT_KEY = "edit_target"
EDIT_TTL = timedelta(minutes=30)   # забытая «✏️ Править» не должна отправить текст через сутки


async def start_edit(repo, kind: str, ident: int):
    await repo.set_setting(EDIT_KEY, f"{kind}:{ident}:{datetime.now(timezone.utc).isoformat()}")


async def handle_q_callback(data: str, repo, sender, service) -> str:
    _, action, qid = data.split(":")
    q = await repo.get_question(int(qid))
    if not q or q["state"] != "open":
        return "Уже обработано"
    if action == "edit":
        await start_edit(repo, "q", q["id"])
        await sender.notify_mentor(
            f"✏️ Пришли одним сообщением ответ для @{q['username']} — отправлю его вместо черновика. "
            f"/cancel — передумал."
        )
        return "Жду текст"
    if action == "send":
        if not await repo.claim("questions", q["id"]):
            return "Уже обработано"   # второе быстрое нажатие
        try:
            result = await sender.send_to_mentee(q["username"], q["draft"])
        except Exception:
            log.exception("send_to_mentee failed for @%s", q["username"])
            await repo.set_question_state(q["id"], "open")
            return "Ошибка отправки, попробуй ещё раз"
        if result in ("sent", "dry"):
            await repo.set_question_state(q["id"], "sent")
            await repo.set_question_final(q["id"], q["draft"])
            return "Отправлено" if result == "sent" else "Dry-run: ушло тебе"
        await repo.set_question_state(q["id"], "open")
        return f"Не отправлено: {result}"
    await repo.set_question_state(q["id"], "ignored")
    return "Ок, игнорирую"


async def handle_edit_text(text: str, repo, sender, service) -> str | None:
    """Текст ментора в личке бота после «✏️ Править». None — правка не ожидалась."""
    target = await repo.get_setting(EDIT_KEY)
    if not target:
        return None
    kind, ident, started = (target.split(":", 2) + ["", ""])[:3]
    try:
        expired = datetime.now(timezone.utc) - datetime.fromisoformat(started) > EDIT_TTL
    except ValueError:
        expired = True
    if expired:
        await repo.set_setting(EDIT_KEY, "")
        return "Правка ждала дольше 30 минут и отменена — ничего не отправил. Нажми «✏️ Править» ещё раз"
    if kind == "q":
        q = await repo.get_question(int(ident))
        if not q or q["state"] != "open" or not await repo.claim("questions", q["id"]):
            await repo.set_setting(EDIT_KEY, "")
            return "Вопрос уже закрыт — ничего не отправил"
        try:
            result = await sender.send_to_mentee(q["username"], text)
        except Exception:
            log.exception("send_to_mentee failed for @%s", q["username"])
            await repo.set_question_state(q["id"], "open")
            return "Ошибка отправки — пришли текст ещё раз или /cancel"
        if result not in ("sent", "dry"):
            await repo.set_question_state(q["id"], "open")
            return f"Не отправлено: {result}. Пришли ещё раз или /cancel"
        await repo.set_question_state(q["id"], "sent")
        await repo.set_question_final(q["id"], text)
        await repo.set_setting(EDIT_KEY, "")
        return f"Отправил @{q['username']} твой вариант" if result == "sent" else "Dry-run: ушло тебе"
    if kind == "p":
        from mentor_bot.jobs import send_ping_draft
        out = await send_ping_draft(int(ident), service, repo, sender, service.settings, text=text)
        if out.retry:
            return out.message + " Пришли ещё раз или /cancel"
        await repo.set_setting(EDIT_KEY, "")
        return out.message
    await repo.set_setting(EDIT_KEY, "")
    return None


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
        # сверка с таблицей: кнопка могла пролежать неделю, а статус с тех пор сменился
        await service.sheets.set_status(m, p["new_status"], expected=p.get("from_status"))
    except StatusConflict as e:
        await repo.delete_proposal(p["id"])
        m.status = e.current or None
        return f"Статус уже «{e.current or '—'}» — предложение устарело, не трогаю"
    except RowNotFound:
        await repo.delete_proposal(p["id"])
        return f"@{p['username']} больше нет в таблице"
    except Exception:
        log.exception("set_status failed for @%s", p["username"])
        return "Ошибка записи в таблицу, нажми ещё раз"
    m.status = p["new_status"]
    await repo.record_status(p["username"], p["new_status"],
                             datetime.now(timezone.utc).isoformat(), "bot")
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


async def handle_p_callback(data: str, repo, sender, service) -> str:
    from mentor_bot.jobs import send_ping_draft
    _, action, pid = data.split(":")
    d = await repo.get_ping_draft(int(pid))
    if not d or d["state"] != "open":
        return "Уже обработано"
    if action == "skip":
        await repo.set_ping_draft_state(d["id"], "skipped")
        return "Ок, этот пинг пропускаю"
    if action == "edit":
        await start_edit(repo, "p", d["id"])
        await sender.notify_mentor(
            f"✏️ Пришли одним сообщением пинг для @{d['username']} — отправлю его. /cancel — передумал."
        )
        return "Жду текст"
    return (await send_ping_draft(d["id"], service, repo, sender, service.settings)).message


def make_router(service, repo, sender) -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("q:"))
    async def on_q(cb: CallbackQuery):
        await cb.answer(await handle_q_callback(cb.data, repo, sender, service))

    @router.callback_query(F.data.startswith("st:"))
    async def on_st(cb: CallbackQuery):
        await cb.answer(await handle_st_callback(cb.data, repo, sender, service))

    @router.callback_query(F.data.startswith("p:"))
    async def on_p(cb: CallbackQuery):
        await cb.answer(await handle_p_callback(cb.data, repo, sender, service))

    @router.callback_query(F.data.startswith("add:"))
    async def on_add(cb: CallbackQuery):
        await cb.answer(await handle_add_callback(cb.data, repo, sender, service))

    return router
