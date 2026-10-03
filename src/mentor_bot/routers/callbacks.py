import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery

from mentor_bot.cards import NOOP, hhmm
from mentor_bot.sheets import RowNotFound, SheetSchemaChanged, StatusConflict
from mentor_bot.store.repo import SRC_BOT, SRC_BOT_EDIT, UNTIL_REPLY
from mentor_bot.style import todo_marks

log = logging.getLogger(__name__)


EDIT_KEY = "edit_target"
EDIT_TTL = timedelta(minutes=30)   # забытая «✏️ Править» не должна отправить текст через сутки


async def _log_sent(repo, username: str, text: str, source: str):
    """Отправленное ботом — в переписку. Эхо этой отправки бизнес-роутер пропускает, иначе оно
    сошло бы за ручной ответ ментора и закрыло чужие вопросы."""
    await repo.log_message(username, "out", text, datetime.now(timezone.utc).isoformat(),
                           source=source)


# итог на карточке, если её кнопку нажали, когда запись уже закрыта (в том числе карточки,
# отправленные до живых карточек): старые кнопки убираем, чтобы не путали
_DONE = {
    "sent": "✅ Отправлено", "dry": "🧪 Dry-run", "answered": "💬 Ответил в чате",
    "ignored": "🙈 Игнор", "stale": "⏭ Неактуален", "skipped": "⏭ Пропущен",
    "expired": "⌛ Истёк", "sending": "⏳ Отправляется", "handoff": "➡️ Отправь сам",
    "sent_manual": "✅ Отправил сам", "snoozed": "⏸ Отложен",
}


def _tz(service) -> str:
    return service.settings.tz_name


async def start_edit(repo, kind: str, ident: int):
    await repo.set_setting(EDIT_KEY, f"{kind}:{ident}:{datetime.now(timezone.utc).isoformat()}")


async def handle_q_callback(data: str, repo, sender, service, card=None) -> str:
    """card — message_id нажатой карточки: запасной вариант для записей без card_msg_id."""
    _, action, qid = data.split(":")
    q = await repo.get_question(int(qid))
    if not q:
        return "Уже обработано"
    card = q.get("card_msg_id") or card
    if q["state"] != "open":
        await sender.close_card(card, _DONE.get(q["state"], "✔️ Обработано"), q["username"])
        return "Уже обработано"
    if action == "edit":
        await start_edit(repo, "q", q["id"])
        await sender.notify_mentor(
            f"✏️ Пришли одним сообщением ответ для @{q['username']} — отправлю его вместо черновика. "
            f"/cancel — передумал."
        )
        return "Жду текст"
    if action == "send":
        # «[по этому в материалах нет - допиши сам]» с аккаунта ментора — хуже любого штампа
        marks = todo_marks(q["draft"])
        if marks:
            # ответ на нажатие — всплывашка до 200 символов
            return f"В черновике осталась пометка {marks[0][:120]} - нажми ✏️ Править"
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
            if result == "sent":
                await _log_sent(repo, q["username"], q["draft"], SRC_BOT)
            label = f"✅ Отправлено {hhmm(_tz(service))}" if result == "sent" else "🧪 Dry-run: ушло тебе"
            await sender.close_card(card, label, q["username"])
            return "Отправлено" if result == "sent" else "Dry-run: ушло тебе"
        await repo.set_question_state(q["id"], "open")
        return f"Не отправлено: {result}"
    await repo.set_question_state(q["id"], "ignored")
    await sender.close_card(card, "🙈 Игнор", q["username"])
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
            return "Черновик уже закрыт — ничего не отправил"
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
        if result == "sent":
            await _log_sent(repo, q["username"], text, SRC_BOT_EDIT)
        await sender.close_card(
            q.get("card_msg_id"),
            f"✏️ Ушла твоя правка {hhmm(_tz(service))}" if result == "sent" else "🧪 Dry-run: ушло тебе",
            q["username"],
        )
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


async def handle_st_callback(data: str, repo, sender, service, card=None) -> str:
    _, action, pid = data.split(":")
    p = await repo.get_proposal(int(pid))
    if not p:
        await sender.close_card(card, "✔️ Обработано")
        return "Уже обработано"
    card = p.get("card_msg_id") or card
    user = p["username"]
    if action == "no":
        await repo.delete_proposal(p["id"])
        await sender.close_card(card, "❌ Статус не меняем", user)
        return "Ок, статус не трогаю"
    m = service.by_username.get(p["username"])
    if m is None:
        await repo.delete_proposal(p["id"])
        await sender.close_card(card, "⏭ Нет в таблице", user)
        return "Менти не найден в таблице"
    try:
        # сверка с таблицей: кнопка могла пролежать неделю, а статус с тех пор сменился
        await service.sheets.set_status(m, p["new_status"], expected=p.get("from_status"))
    except StatusConflict as e:
        await repo.delete_proposal(p["id"])
        m.status = e.current or None
        await sender.close_card(card, f"⏭ Уже «{e.current or '—'}»", user)
        return f"Статус уже «{e.current or '—'}» — предложение устарело, не трогаю"
    except RowNotFound:
        await repo.delete_proposal(p["id"])
        await sender.close_card(card, "⏭ Нет в таблице", user)
        return f"@{p['username']} больше нет в таблице"
    except SheetSchemaChanged as e:
        # повтор не поможет, пока не починят шапку; предложение не удаляем — нажмёшь потом
        return f"Не пишу в таблицу: {e.problem}"[:200]
    except Exception:
        log.exception("set_status failed for @%s", p["username"])
        return "Ошибка записи в таблицу, нажми ещё раз"
    m.status = p["new_status"]
    await repo.record_status(p["username"], p["new_status"],
                             datetime.now(timezone.utc).isoformat(), "bot")
    await repo.delete_proposal(p["id"])
    await sender.close_card(card, f"✅ «{p['new_status']}»", user)
    return f"Статус @{p['username']} → «{p['new_status']}»"


async def handle_add_callback(data: str, repo, sender, service, card=None) -> str:
    _, idx, username = data.split(":")
    if idx == "skip":
        await repo.set_setting(f"ignore_chat:{username}", "1")
        await sender.close_card(card, "🚫 Не менти")
        return "Ок, не менти"
    if username in service.by_username:
        await sender.close_card(card, "✔️ Уже в таблице", username)
        return "Уже в таблице"
    titles = service.settings.active_sheet_titles
    if not idx.isdigit() or int(idx) >= len(titles):
        return "Кнопка устарела"
    title = titles[int(idx)]
    await service.sheets.append_mentee(title, f"@{username}")
    await service.sync_mentees()
    await sender.close_card(card, f"➕ В «{title}»", username)
    return f"Добавил @{username} в «{title}»"


async def handle_p_callback(data: str, repo, sender, service, card=None) -> str:
    from mentor_bot.jobs import send_ping_draft
    parts = data.split(":")
    action, pid = parts[1], parts[2]
    d = await repo.get_ping_draft(int(pid))
    if not d:
        return "Уже обработано"
    card = d.get("card_msg_id") or card
    user = d["username"]
    if d["state"] not in ("open", "handoff") or (d["state"] == "handoff" and action in ("send", "edit")):
        await sender.close_card(card, _DONE.get(d["state"], "✔️ Обработано"), user)
        return "Уже обработано"
    now = datetime.now(timezone.utc)
    if action in ("skip", "snooze"):
        # «Пропустить» — до следующего обычного пинга, а не до завтра: раньше на следующий
        # день готовился такой же черновик и тратился новый вызов модели
        days = (int(parts[3]) if action == "snooze"
                else getattr(service.settings, "ping_interval_days", 3))
        until = now + timedelta(days=days)
        await repo.set_pause(user, until.isoformat())
        await repo.set_ping_draft_state(d["id"], "skipped" if action == "skip" else "snoozed")
        day = until.astimezone(ZoneInfo(_tz(service))).strftime("%d.%m")
        label = f"⏭ Пропущен до {day}" if action == "skip" else f"⏸ До {day}"
        await sender.close_card(card, label, user)
        return f"Ок, следующий пинг @{user} не раньше {day}"
    if action == "until":
        await repo.set_pause(user, UNTIL_REPLY)
        await repo.set_ping_draft_state(d["id"], "snoozed")
        await sender.close_card(card, "🔕 До ответа ученика", user)
        return f"Ок, @{user} не пингую, пока сам не напишет"
    if action == "edit":
        await start_edit(repo, "p", d["id"])
        await sender.notify_mentor(
            f"✏️ Пришли одним сообщением пинг для @{d['username']} — отправлю его. /cancel — передумал."
        )
        return "Жду текст"
    return (await send_ping_draft(d["id"], service, repo, sender, service.settings,
                                  card=card)).message


async def handle_esc_callback(data: str, repo, sender, service, card=None) -> str:
    """Ученик игнорит пинги: «Пинговать снова» обнуляет счётчик; «Через 14 дн» — то же,
    но с паузой, чтобы следующий пинг пришёл не завтра."""
    parts = data.split(":")
    action, username = parts[1], parts[2]
    await repo.reset_unanswered(username)
    if action == "pause":
        until = datetime.now(timezone.utc) + timedelta(days=int(parts[3]))
        await repo.set_pause(username, until.isoformat())
        day = until.astimezone(ZoneInfo(_tz(service))).strftime("%d.%m")
        await sender.close_card(card, f"⏸ Пингую снова после {day}", username)
        return f"Ок, @{username} снова пингую после {day}"
    await repo.set_pause(username, None)
    await sender.close_card(card, "🔁 Снова пингую", username)
    return f"Ок, @{username} снова в пингах"


def make_router(service, repo, sender) -> Router:
    router = Router()

    def card(cb: CallbackQuery):
        return cb.message.message_id if cb.message else None

    @router.callback_query(F.data.startswith("q:"))
    async def on_q(cb: CallbackQuery):
        await cb.answer(await handle_q_callback(cb.data, repo, sender, service, card(cb)))

    @router.callback_query(F.data.startswith("st:"))
    async def on_st(cb: CallbackQuery):
        await cb.answer(await handle_st_callback(cb.data, repo, sender, service, card(cb)))

    @router.callback_query(F.data.startswith("p:"))
    async def on_p(cb: CallbackQuery):
        await cb.answer(await handle_p_callback(cb.data, repo, sender, service, card(cb)))

    @router.callback_query(F.data.startswith("add:"))
    async def on_add(cb: CallbackQuery):
        await cb.answer(await handle_add_callback(cb.data, repo, sender, service, card(cb)))

    @router.callback_query(F.data.startswith("esc:"))
    async def on_esc(cb: CallbackQuery):
        await cb.answer(await handle_esc_callback(cb.data, repo, sender, service, card(cb)))

    @router.callback_query(F.data == NOOP)
    async def on_noop(cb: CallbackQuery):
        await cb.answer("Уже обработано")

    return router
