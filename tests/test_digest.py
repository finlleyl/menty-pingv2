from datetime import datetime, timezone

from mentor_bot.digest import digest_text, fails_text
from mentor_bot.llm import InterviewItem
from mentor_bot.service import Service
from mentor_bot.store.repo import Repo
from tests.test_commands import Cfg
from tests.test_service import FakeKB, FakeLLM, FakeSender, FakeSheets, sm

NOW = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


async def test_digest_funnel_moves_stuck_and_medians(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    mentees = [sm("a", "Спринт 2"), sm("b", "Спринт 2"), sm("c", "Спринт 3"), sm("d", "Спринт 3"),
               sm("e", "Спринт 3"), sm("slow", "Спринт 2")]
    svc = Service(repo, FakeSheets(mentees), FakeLLM(), FakeSender(), FakeKB(), Cfg())
    # история: c, d, e прошли второй спринт за 10, 12 и 14 дней; slow сидит на нём 40 дней
    for user, start, end in (("c", "2026-07-01", "2026-07-11"), ("d", "2026-07-01", "2026-07-13"),
                             ("e", "2026-08-01", "2026-08-15")):
        await repo.record_status(user, "Спринт 1", f"{start}T00:00:00+00:00", "sheet")   # initial
        await repo.record_status(user, "Спринт 2", f"{start}T00:00:00+00:00", "bot")
        await repo.record_status(user, "Спринт 3", f"{end}T00:00:00+00:00", "bot")
    await repo.record_status("slow", "Спринт 1", "2026-07-01T00:00:00+00:00", "sheet")
    await repo.record_status("slow", "Спринт 2", "2026-07-23T10:00:00+00:00", "bot")
    await repo.record_status("a", "Спринт 1", "2026-08-01T00:00:00+00:00", "sheet")
    await repo.record_status("a", "Спринт 2", "2026-08-28T00:00:00+00:00", "sheet")   # на этой неделе
    await svc.sync_mentees()
    out = await digest_text(svc, repo, Cfg(), NOW)
    assert "Воронка: Спринт 2: 3 · Спринт 3: 3" in out
    assert "@a Спринт 1 → Спринт 2" in out
    assert "@slow — Спринт 2 40 дн. (75% проходят за 14 дн.)" in out
    assert "@a —" not in out.split("Застряли")[1].split("\n")[0]   # 4 дня — не застрял
    assert "Спринт 2: 12 дн. (прошли 3, сейчас на ней 2)" in out
    await repo.close()


async def test_fails_text_ranks_by_distinct_people(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    it = lambda q, c=None: InterviewItem(company=c, stage=None, question=q, failed=True)
    await repo.add_interview_notes("a", "t1", [it("как устроен map", "Ozon")], [[1.0, 0.0]])
    await repo.add_interview_notes("b", "t2", [it("устройство мапы", "Avito")], [[0.99, 0.1]])
    await repo.add_interview_notes("c", "t3", [it("select", None), it("select 2")], [[0.0, 1.0], [0.0, 1.0]])
    out = await fails_text(repo)
    lines = out.splitlines()
    assert lines[1].startswith("• как устроен map — 2× у 2 чел. [Avito, Ozon]")
    assert lines[2].startswith("• select — 2× у 1 чел.")
    await repo.close()
