"""Здоровье бота: журнал фоновых задач, предупреждения без повторов и сводка /health.

Самые опасные сбои — пропавшее подключение, упавшая задача, лежащий LLM — раньше было видно
только в docker logs. Теперь у каждой задачи есть журнал, а /health отвечает на вопрос
«всё ли работает и почему пинги не идут» одним сообщением."""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

JOB_LABELS = {
    "ping_cycle": "пинги", "remind_cycle": "напоминания", "drain_pending": "разбор сообщений",
    "dossier_cycle": "досье", "digest_cycle": "сводка", "nightly_backup": "бэкап",
    "calls_cycle": "созвоны из переписки", "calls_morning": "утренние созвоны",
}

# Задачи, которые жалко пропустить из-за простоя: если последний успешный запуск старше
# периода с запасом, после старта они запускаются сразу (APScheduler пропущенное не догоняет)
CATCH_UP = {
    "nightly_backup": timedelta(hours=26),
    "dossier_cycle": timedelta(hours=26),
    "digest_cycle": timedelta(days=8),
}

STUCK_BUFFER = timedelta(minutes=30)   # буфер старше — разбор сообщений встал


async def alert_once(repo, sender, key: str, text: str):
    """Предупреждение ментору без повторов: то же самое не шлём, пока проблема не сменится
    или не уйдёт. Пустой text — проблемы нет, следующее появление снова придёт."""
    prev = await repo.get_setting(f"alert:{key}", "")
    if text == prev:
        return
    if text:
        try:
            await sender.notify_mentor(text)
        except Exception:
            # не дошло (сеть, лимит Telegram) — не запоминаем: иначе следующая попытка
            # сочла бы, что ментор уже предупреждён, и он так и не узнал бы о проблеме
            log.warning("alert %s not delivered", key, exc_info=True)
            return
    await repo.set_setting(f"alert:{key}", text)


def tracked(name: str, fn, repo, sender):
    """Обёртка фоновой задачи: журнал запусков и одно предупреждение, если она падает."""
    async def run(*args, **kwargs):
        await repo.job_started(name, _now())
        try:
            await fn(*args, **kwargs)
        except Exception as e:
            log.exception("job %s failed", name)
            error = f"{type(e).__name__}: {e}"[:300]
            await repo.job_finished(name, _now(), error=error)
            await alert_once(repo, sender, f"job:{name}",
                             f"⚠️ Упала задача «{JOB_LABELS.get(name, name)}»: {error}")
            return
        await repo.job_finished(name, _now())
        await alert_once(repo, sender, f"job:{name}", "")
    run.__name__ = name
    return run


def due_catch_ups(runs: list[dict], now_utc: datetime) -> list[str]:
    """Какие задачи догнать после старта. Только те, что уже работали: на свежей установке
    внезапная сводка в момент запуска была бы сюрпризом."""
    last_ok = {r["job"]: r.get("last_ok") for r in runs}
    due = []
    for job, period in CATCH_UP.items():
        ok = last_ok.get(job)
        if ok and now_utc - datetime.fromisoformat(ok) > period:
            due.append(job)
    return due


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _at(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%d.%m %H:%M")


async def business_line(bot, repo) -> str:
    from mentor_bot.routers.business import can_reply
    bconn = await repo.get_setting("bconn")
    if not bconn:
        return "❌ не подключён — подключусь по первому сообщению ученика"
    try:
        conn = await bot.get_business_connection(bconn)
    except Exception as e:
        return f"⚠️ Telegram не подтверждает подключение ({type(e).__name__})"
    if not conn.is_enabled:
        return "❌ выключено в настройках Telegram"
    return "✅ подключён" if can_reply(conn) else "⚠️ подключён, но нет права отвечать"


async def health_text(service, repo, settings, bot, now_utc: datetime) -> str:
    tz = ZoneInfo(settings.tz_name)
    llm_down = await repo.get_setting("alerted_llm_down")
    chunks = len(getattr(service.kb, "chunks", []) or [])
    sheet_problem = (await repo.get_setting("alert:sheets", "") or "").split("\n")[0]
    q = await repo.queue_stats((now_utc - timedelta(hours=4)).isoformat())
    lines = [
        "🩺 Здоровье бота",
        f"Telegram для бизнеса: {await business_line(bot, repo)}",
        "LLM: " + ("❌ недоступен — разбор сообщений на паузе" if llm_down else "✅ отвечает"),
        "Таблица: " + (sheet_problem or
                       f"✅ учеников {len(service.by_username)}, синк "
                       f"{_at(await repo.get_setting('last_sync'), tz)}"),
        "База знаний: " + (f"✅ фрагментов {chunks}" if chunks else "❌ пусто — запусти /reindex"),
        f"Последнее сообщение из чатов: {_at(q['last_chat'], tz)}",
    ]
    buffer = f"в буфере {q['pending']} сообщ."
    if q["pending_oldest"] and now_utc - datetime.fromisoformat(q["pending_oldest"]) > STUCK_BUFFER:
        buffer += " ⚠️ давно не разбирались"
    lines.append(f"Очередь: {buffer}; черновиков без ответа {q['open_questions']} "
                 f"(старше 4 ч — {q['old_questions']}); пингов ждут тебя {q['waiting_pings']}")
    lines.append("Задачи:")
    runs = {r["job"]: r for r in await repo.job_runs()}
    for job, label in JOB_LABELS.items():
        r = runs.get(job)
        if not r:
            lines.append(f"• {label} — ещё не запускалась")
            continue
        failed = r.get("last_error_ts") and (not r.get("last_ok") or r["last_error_ts"] > r["last_ok"])
        state = (f"⚠️ упала {_at(r['last_error_ts'], tz)}: {r['last_error']}" if failed
                 else f"✅ {_at(r.get('last_ok'), tz)}")
        missed = f", пропусков {r['missed']}" if r.get("missed") else ""
        lines.append(f"• {label} — {state}{missed}")
    return "\n".join(lines)
