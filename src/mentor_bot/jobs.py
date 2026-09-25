import asyncio
import gzip
import json
import os
import tempfile
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from mentor_bot.llm import LLMUnavailable
from mentor_bot.pings import (
    effective_last_contact,
    in_send_window,
    is_stopped,
    parse_iso_utc,
    should_ping,
)
from mentor_bot.stages import STAGE_LABELS, forbidden_hits, parse_stage

log = logging.getLogger(__name__)


async def ping_cycle(service, repo, sender, llm, settings, now_utc: datetime | None = None):
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.tz_name)
    if await sender.is_paused_all():
        return
    if not in_send_window(now_utc.astimezone(tz), settings.quiet_start_hour, settings.quiet_end_hour):
        return
    try:
        await service.sync_mentees()
    except Exception:
        log.exception("sheet sync failed")
        await sender.notify_mentor("⚠️ Не смог прочитать таблицу, цикл пингов пропущен")
        return

    errors = 0
    items = list(service.by_username.items())
    random.shuffle(items)
    for username, m in items:
        rec = await repo.get_mentee(username) or {}
        last_msg_iso = await repo.last_message_ts(username)
        last = effective_last_contact(m.last_date, last_msg_iso, tz)
        # рассинхрон: в чате связь свежее, чем дата в таблице → таблица догоняет телеграм
        if last_msg_iso:
            msg_date = datetime.fromisoformat(last_msg_iso).astimezone(tz).date()
            if m.last_date is None or msg_date > m.last_date:
                try:
                    await service.sheets.set_date(m, msg_date)
                    m.last_date = msg_date
                except Exception:
                    log.exception("sheet date backfill failed for %s", username)
                    errors += 1
        if not should_ping(
            last_contact=last, status=m.status, now_utc=now_utc,
            stop_list=settings.stop_status_list, interval_days=settings.ping_interval_days,
            unanswered=rec.get("unanswered_pings", 0),
            max_unanswered=settings.max_unanswered_pings,
            paused_until_iso=rec.get("paused_until"),
        ):
            continue

        last_ping = await repo.last_ping_ts(username)
        if last_ping is not None:
            last_ping_local = datetime.fromisoformat(last_ping).astimezone(tz).date()
            if last_ping_local == now_utc.astimezone(tz).date():
                continue  # уже пинговали сегодня — не чаще 1 пинга в сутки

        dry = await sender.is_dryrun()
        if not dry:
            if not rec.get("chat_id"):
                continue  # чат не привязан — не тратим LLM зря
            if not await repo.get_setting("bconn"):
                if not await repo.get_setting("alerted_no_bconn"):
                    await sender.notify_mentor(
                        "⚠️ Business connection не подключён — пинги не идут"
                    )
                    await repo.set_setting("alerted_no_bconn", "1")
                break  # без подключения дальше по циклу смысла нет

        review = await repo.get_setting("ping_mode", "auto") == "review"
        if await repo.open_ping_draft(username):
            continue  # прошлый пинг ещё ждёт твоего решения — второй не готовим

        stage = parse_stage(m.status)
        try:
            recent = await repo.recent_messages(username, limit=10)
            profile = await repo.get_profile(username)
            text = await llm.gen_ping(m.display, m.status, recent, profile, m.notes)
            hits = forbidden_hits(text, stage)
            if hits:
                log.warning("ping for %s touched forbidden %s, regenerating", username, hits)
                text = await llm.gen_ping(m.display, m.status, recent, profile, m.notes, avoid=hits)
                hits = forbidden_hits(text, stage)
        except Exception:
            log.exception("ping generation failed for %s", username)
            errors += 1
            continue

        if review or hits:
            # не отправляем сами: либо так настроено, либо модель дважды нарушила запрет
            pid = await repo.add_ping_draft(username, text, now_utc.isoformat())
            await repo.log_ping(username, now_utc.isoformat(), "review")
            warn = (f"\n\n⚠️ Модель дважды затронула запрещённое для стадии: {', '.join(hits)}"
                    if hits else "")
            await sender.notify_mentor(
                f"📨 Пинг для @{username} ({STAGE_LABELS[stage]}):\n{text}{warn}",
                reply_markup=ping_draft_kb(pid),
            )
            continue

        await repo.log_ping(username, now_utc.isoformat(), "attempt")
        try:
            result = await sender.send_to_mentee(username, text)
        except Exception:
            log.exception("ping send failed for %s", username)
            errors += 1
            continue
        await after_ping(result, username, m, text, service, repo, sender, settings, now_utc)
        if result == "sent":
            await asyncio.sleep(58)  # rate limit ≤1 пинг/мин

    if errors:
        await sender.notify_mentor(f"⚠️ Цикл пингов: {errors} ошибок, детали в логах")



def ping_draft_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отправить", callback_data=f"p:send:{pid}"),
        InlineKeyboardButton(text="✏️ Править", callback_data=f"p:edit:{pid}"),
        InlineKeyboardButton(text="Пропустить", callback_data=f"p:skip:{pid}"),
    ]])


async def after_ping(result, username, m, text, service, repo, sender, settings, now_utc):
    """Учёт после отправки пинга — общий для цикла и для одобренных черновиков."""
    tz = ZoneInfo(settings.tz_name)
    if result in ("sent", "dry") and parse_stage(m.status) == "resume":
        # мяч у ментора: ученику пинг, ментору напоминание, что резюме за ним
        since = (await repo.get_mentee(username) or {}).get("status_since")
        waiting = ""
        if since:
            waiting = f" — ждёт уже {(now_utc - parse_iso_utc(since)).days} дн."
        await sender.notify_mentor(f"📝 @{username} ждёт от тебя резюме{waiting}")

    if result == "sent":
        await repo.log_ping(username, now_utc.isoformat(), "sent")
        await repo.bump_unanswered(username)
        await repo.set_setting("alerted_no_bconn", "")
        rec2 = await repo.get_mentee(username) or {}
        if rec2.get("unanswered_pings", 0) >= settings.max_unanswered_pings:
            await sender.notify_mentor(
                f"🚨 @{username} игнорит {settings.max_unanswered_pings} пинга подряд — "
                f"дальше не пингую, разберись вручную"
            )
        try:
            await service.sheets.set_date(m, now_utc.astimezone(tz).date())
            m.last_date = now_utc.astimezone(tz).date()
        except Exception:
            log.exception("sheet date write failed after ping")
            await sender.notify_mentor(f"⚠️ Пинг @{username} ушёл, но дата в таблице не записана")
        await repo.log_message(username, "out", text, now_utc.isoformat())
    elif result == "dry":
        await repo.log_ping(username, now_utc.isoformat(), "dry")


@dataclass
class DraftOutcome:
    message: str
    retry: bool = False   # True — черновик остался открытым, можно повторить


async def send_ping_draft(pid, service, repo, sender, settings, text=None,
                          now_utc: datetime | None = None) -> DraftOutcome:
    """Отправка одобренного пинга: как есть (text=None) или в редакции ментора."""
    now_utc = now_utc or datetime.now(timezone.utc)
    d = await repo.get_ping_draft(pid)
    if not d or d["state"] != "open":
        return DraftOutcome("Уже обработано")
    username = d["username"]
    last_in = await repo.last_in_ts(username)
    if last_in and parse_iso_utc(last_in) > parse_iso_utc(d["created_ts"]):
        # пока пинг лежал, ученик написал сам — «ты куда пропал?» теперь неуместно
        await repo.set_ping_draft_state(pid, "stale")
        return DraftOutcome(f"@{username} уже написал сам — пинг неактуален, не отправляю")
    m = service.by_username.get(username)
    if m is None:
        await repo.set_ping_draft_state(pid, "stale")
        return DraftOutcome(f"@{username} больше нет в таблице")
    # пока черновик лежал, могли поставить паузу или стоп-статус — перепроверяем
    rec = await repo.get_mentee(username) or {}
    paused = rec.get("paused_until") and parse_iso_utc(rec["paused_until"]) > now_utc
    if paused or is_stopped(m.status, settings.stop_status_list) \
            or rec.get("unanswered_pings", 0) >= settings.max_unanswered_pings:
        await repo.set_ping_draft_state(pid, "stale")
        return DraftOutcome(f"@{username} сейчас пинговать нельзя (пауза, стоп-статус или игнор) — не отправляю")
    if not await repo.claim("ping_drafts", pid):
        return DraftOutcome("Уже обработано")   # второе быстрое нажатие
    text = text or d["text"]
    await repo.log_ping(username, now_utc.isoformat(), "attempt")
    try:
        result = await sender.send_to_mentee(username, text)
    except Exception:
        log.exception("ping draft send failed for %s", username)
        await repo.set_ping_draft_state(pid, "open")
        return DraftOutcome("Ошибка отправки", retry=True)
    if result not in ("sent", "dry"):
        await repo.set_ping_draft_state(pid, "open")
        return DraftOutcome(f"Не отправлено: {result}", retry=True)
    await repo.set_ping_draft_state(pid, result)
    await after_ping(result, username, m, text, service, repo, sender, settings, now_utc)
    return DraftOutcome(f"Пинг @{username} отправлен" if result == "sent" else "Dry-run: ушло тебе")


async def drain_pending(service, repo, sender, settings, now_utc: datetime | None = None):
    """Разбирает буферы, в которые ученик не писал дольше окна дебаунса."""
    now_utc = now_utc or datetime.now(timezone.utc)
    before = (now_utc - timedelta(minutes=settings.debounce_minutes)).isoformat()
    for row in await repo.mature_pending(before):
        username = row["username"]
        last_out = await repo.last_out_ts(username)
        if last_out and parse_iso_utc(last_out) > parse_iso_utc(row["last_in_ts"]):
            # ментор ответил сам, пока буфер зрел — LLM не трогаем
            await repo.drop_pending(username)
            continue
        texts = json.loads(row["texts"])
        text = "\n".join(texts)
        try:
            await service.handle_buffered(username, text, row["last_in_ts"])
        except LLMUnavailable as e:
            # провайдер лежит, сообщение не виновато: буфер не трогаем, следующий тик повторит.
            # Остальные буферы упрутся в то же самое — не долбим, ментору одно предупреждение
            log.warning("LLM unavailable, keeping buffer for %s: %s", username, e)
            if not await repo.get_setting("alerted_llm_down"):
                await sender.notify_mentor(
                    f"⚠️ LLM недоступен — разбор сообщений учеников на паузе, "
                    f"повторяю каждую минуту, ничего не теряется.\n{str(e)[:300]}"
                )
                await repo.set_setting("alerted_llm_down", "1")
            break
        except Exception as e:
            # битое сообщение: повтор не поможет, иначе оно дренажилось бы каждую минуту вечно
            log.exception("drain failed for %s", username)
            await sender.notify_mentor(
                f"⚠️ Не смог разобрать сообщения @{username} ({type(e).__name__}), "
                f"убрал из очереди: {text[:100]}"
            )
            await repo.consume_pending(username, len(texts))
        else:
            # снимаем ровно то, что обработали: дописанное за это время останется
            await repo.consume_pending(username, len(texts))
            if await repo.get_setting("alerted_llm_down"):
                await repo.set_setting("alerted_llm_down", "")
                await sender.notify_mentor("✅ LLM снова отвечает, разбираю отложенные сообщения")

async def remind_cycle(repo, sender, now_utc: datetime | None = None, settings=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    if settings is not None:
        tz = ZoneInfo(settings.tz_name)
        hour = now_utc.astimezone(tz).hour
        if not (9 <= hour < 23):
            return
    threshold = (now_utc - timedelta(hours=4)).isoformat()
    for q in await repo.open_questions(older_than_iso=threshold, unreminded_only=True):
        await sender.notify_mentor(
            f"⏰ Висит вопрос от @{q['username']} ({q['created_ts'][:16]}):\n{q['question'][:200]}"
        )
        await repo.mark_reminded(q["id"])


async def dossier_cycle(service, repo, llm, sender, settings, now_utc: datetime | None = None):
    """Раз в сутки обновляет досье тех, у кого с прошлого раза была переписка."""
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        await service.sync_mentees()
    except Exception:
        log.exception("sheet sync failed")
        await sender.notify_mentor("⚠️ Досье: не смог прочитать таблицу, цикл пропущен")
        return

    errors = 0
    for username in await repo.stale_profiles():
        m = service.by_username.get(username)
        if m is None:
            continue  # чат есть, а в таблице человека нет — не наш менти
        try:
            recent = await repo.recent_messages(username, limit=30)
            old = await repo.get_profile(username)
            summary = await llm.update_profile(old, recent, m.notes)
            # сначала таблица: упадёт запись — досье останется устаревшим и попадёт
            # в следующий цикл, а не «протухнет» молча при молчащем ученике
            await service.sheets.set_dossier(m, summary)
            await repo.set_profile(username, summary, now_utc.isoformat())
        except Exception:
            log.exception("dossier update failed for %s", username)
            errors += 1
    if errors:
        await sender.notify_mentor(f"⚠️ Досье: {errors} ошибок, детали в логах")


async def backup_db(repo, sender, now_utc: datetime | None = None):
    """Снимок SQLite, сжатый gzip, — файлом ментору в личку бота."""
    now_utc = now_utc or datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bot.db")
        await repo.backup_to(path)
        with open(path, "rb") as f:
            data = gzip.compress(f.read())
    name = f"mentor-bot-{now_utc:%Y%m%d-%H%M}.db.gz"
    await sender.send_file_to_mentor(data, name, caption="💾 Бэкап базы бота")
    return name


async def digest_cycle(service, repo, sender, settings, now_utc: datetime | None = None):
    from mentor_bot.digest import digest_text
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        await service.sync_mentees()
    except Exception:
        log.exception("sheet sync failed")
        await sender.notify_mentor("⚠️ Сводка: не смог прочитать таблицу, считаю по кэшу")
    await sender.notify_mentor(await digest_text(service, repo, settings, now_utc))
