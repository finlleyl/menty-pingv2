import json
from datetime import date

from mentor_bot.llm import StatusUpdate, Triage
from mentor_bot.service import Service
from mentor_bot.sheets import SheetMentee
from mentor_bot.store.repo import Repo


def sm(username="ivan", status="3 спринт", last=date(2026, 8, 15)):
    return SheetMentee(username=username, display=f"Иван @{username}", status=status,
                       last_date=last, sheet_title="A", row=3, date_col=4, status_col=5)


class FakeSheets:
    def __init__(self, mentees):
        self.mentees = mentees
        self.dates, self.statuses = [], []

    async def load_mentees(self):
        return self.mentees

    async def set_date(self, m, d):
        self.dates.append((m.username, d))

    async def set_status(self, m, s, expected=None):
        from mentor_bot.sheets import StatusConflict
        if expected is not None and (m.status or "") != expected:
            raise StatusConflict(m.status or "")
        self.statuses.append((m.username, s))

    async def append_mentee(self, title, display):
        pass

    async def set_dossier(self, m, text):
        pass


# старые однословные метки → разметка сортировщика, чтобы тесты читались как раньше
_LEGACY_KINDS = {"question": ["tech_question"], "progress": ["status_change"], "other": ["smalltalk"]}


class FakeLLM:
    def __init__(self, kind="other", status=None, *, kinds=None, needs_reply=None,
                 urgent=False, milestone="none", drafts=None):
        self.kinds = kinds or _LEGACY_KINDS[kind]
        self.needs_reply = (needs_reply if needs_reply is not None else any(
            k in ("tech_question", "org_question", "feelings", "win") for k in self.kinds))
        self.urgent = urgent
        self.milestone = milestone
        self.status = status or StatusUpdate(new_status=None, confidence="low")
        self.drafts = list(drafts or [])      # очередь ответов draft_reply; пусто — эхо
        self.triage_calls, self.draft_calls, self.status_calls = [], [], []

    async def triage(self, text, recent=None, status=None):
        self.triage_calls.append({"text": text, "recent": recent, "status": status})
        return Triage(kinds=self.kinds, needs_reply=self.needs_reply, urgent=self.urgent,
                      milestone=self.milestone)

    async def parse_status(self, text, current):
        self.status_calls.append(text)
        return self.status

    async def parse_mentor_verdict(self, text, current):
        return StatusUpdate(new_status=None, confidence="low")

    async def draft_reply(self, text, kinds, **ctx):
        self.draft_ctx = ctx
        self.draft_calls.append({"text": text, "kinds": kinds, **ctx})
        return self.drafts.pop(0) if self.drafts else f"ЧЕРНОВИК[{text}]"

    async def update_profile(self, old, recent, notes=None):
        return "досье"

    async def embed(self, texts):
        return [[1.0, 0.0]]


class FakeSender:
    def __init__(self):
        self.mentor_msgs = []

    async def notify_mentor(self, text, reply_markup=None):
        self.mentor_msgs.append((text, reply_markup))

    async def is_paused_all(self) -> bool:
        return False


class FakeKB:
    def __init__(self):
        self.calls = []

    def search(self, q, emb, k=5):
        self.calls.append(q)
        return [{"text": "из материалов", "source": "урок «Каналы»"}]


class FakeSettings:
    active_sheet_titles = ["A"]
    tz_name = "Europe/Moscow"


async def make(tmp_path, kind="other", status=None):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sheets = FakeSheets([sm()])
    sender = FakeSender()
    svc = Service(repo, sheets, FakeLLM(kind, status), sender, FakeKB(), FakeSettings())
    await svc.sync_mentees()
    return repo, sheets, sender, svc


async def test_incoming_updates_date_and_resets(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    await repo.upsert_mentee("ivan", chat_id=1)
    await repo.bump_unanswered("ivan")
    await svc.on_incoming("ivan", "ок", "2026-08-19T10:00:00+00:00")
    # дата 19.08 новее 15.08 из таблицы → записана
    assert sheets.dates == [("ivan", date(2026, 8, 19))]
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 0
    assert (await repo.last_message_ts("ivan")) == "2026-08-19T10:00:00+00:00"


async def test_incoming_older_than_sheet_no_write(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    await svc.on_incoming("ivan", "ок", "2026-08-10T10:00:00+00:00")
    assert sheets.dates == []


async def test_incoming_buffers_and_never_calls_llm(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path, kind="question")

    async def boom(*a, **kw):
        raise AssertionError("on_incoming не должен трогать LLM")

    svc.llm.triage = boom
    await svc.on_incoming("ivan", "что такое mutex?", "2026-08-19T10:00:00+00:00")
    row = await repo.get_pending("ivan")
    assert json.loads(row["texts"]) == ["что такое mutex?"]
    assert await repo.open_questions() == []


async def test_handle_buffered_creates_draft(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path, kind="question")
    await svc.handle_buffered("ivan", "что такое mutex?", "2026-08-19T10:00:00+00:00")
    qs = await repo.open_questions()
    assert len(qs) == 1 and qs[0]["draft"] == "ЧЕРНОВИК[что такое mutex?]"
    assert any("ЧЕРНОВИК" in m[0] for m in sender.mentor_msgs)


async def test_outgoing_drops_pending_buffer(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    await svc.on_incoming("ivan", "вопрос", "2026-08-19T10:00:00+00:00")
    assert await repo.get_pending("ivan") is not None
    await svc.on_outgoing("ivan", "уже ответил", "2026-08-19T10:01:00+00:00")
    assert await repo.get_pending("ivan") is None


async def test_contact_only_extends_window_without_text(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    await svc.on_incoming("ivan", "смотри", "2026-08-19T10:00:00+00:00")
    await svc.on_contact_only("ivan", "in", "2026-08-19T10:02:00+00:00")
    row = await repo.get_pending("ivan")
    assert row["last_in_ts"] == "2026-08-19T10:02:00+00:00"
    assert json.loads(row["texts"]) == ["смотри"]


async def test_progress_high_confidence_creates_proposal_not_write(tmp_path):
    repo, sheets, sender, svc = await make(
        tmp_path, kind="progress", status=StatusUpdate(new_status="Собесы", confidence="high")
    )
    await svc.handle_buffered("ivan", "вышел на собесы", "2026-08-19T10:00:00+00:00")
    # даже при high confidence в таблицу ничего не пишем — только предложение с кнопками
    assert sheets.statuses == []
    assert (await repo.get_proposal(1))["new_status"] == "Собесы"
    assert any("уверенно" in m[0] for m in sender.mentor_msgs)


async def test_progress_low_confidence_creates_proposal_with_hint(tmp_path):
    repo, sheets, sender, svc = await make(
        tmp_path, kind="progress", status=StatusUpdate(new_status="Собесы", confidence="low")
    )
    await svc.handle_buffered("ivan", "мб начну собеситься", "2026-08-19T10:00:00+00:00")
    assert sheets.statuses == []
    assert (await repo.get_proposal(1))["new_status"] == "Собесы"
    assert any("под вопросом" in m[0] for m in sender.mentor_msgs)


async def test_outgoing_updates_and_alerts_on_sheet_failure(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    await repo.bump_unanswered("ivan")

    async def boom(m, d):
        raise RuntimeError("sheets down")

    sheets.set_date = boom
    await svc.on_outgoing("ivan", "привет", "2026-08-19T10:00:00+00:00")
    # сообщение залогировано, счётчик сброшен, ментор получил алерт, бот не упал
    assert (await repo.last_message_ts("ivan")) == "2026-08-19T10:00:00+00:00"
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 0
    assert any("⚠️" in m[0] for m in sender.mentor_msgs)


async def test_on_contact_only_updates_date_resets_and_logs(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    await repo.bump_unanswered("ivan")
    await svc.on_contact_only("ivan", "in", "2026-08-19T10:00:00+00:00")
    assert (await repo.last_message_ts("ivan")) == "2026-08-19T10:00:00+00:00"
    msgs = await repo.recent_messages("ivan", limit=1)
    assert msgs[0]["text"] == "[медиа]" and msgs[0]["direction"] == "in"
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 0
    # дата 19.08 новее 15.08 из таблицы → записана (МСК)
    assert sheets.dates == [("ivan", date(2026, 8, 19))]


async def test_on_outgoing_closes_open_question(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T09:00:00+00:00")
    await svc.on_outgoing("ivan", "ответил лично в чате", "2026-08-19T10:00:00+00:00")
    assert (await repo.get_question(qid))["state"] == "answered"


async def test_mentor_verdict_creates_proposal_not_write(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)

    async def verdict(text, current):
        return StatusUpdate(new_status="Спринт 4", confidence="high")

    svc.llm.parse_mentor_verdict = verdict
    await svc.on_outgoing("ivan", "сдан спринт 3, поехали дальше", "2026-08-19T10:00:00+00:00")
    assert sheets.statuses == []                                  # автозаписи нет
    assert (await repo.get_proposal(1))["new_status"] == "Спринт 4"
    assert any("Сменить статус" in m[0] for m in sender.mentor_msgs)


async def test_ordinary_mentor_message_does_not_call_llm(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)

    async def boom(text, current):
        raise AssertionError("обычное сообщение не должно уходить в LLM")

    svc.llm.parse_mentor_verdict = boom
    await svc.on_outgoing("ivan", "глянь видео по DDD", "2026-08-19T10:00:00+00:00")
    assert await repo.get_proposal(1) is None


async def test_verdict_llm_failure_does_not_break_outgoing(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)

    async def boom(text, current):
        raise RuntimeError("llm down")

    svc.llm.parse_mentor_verdict = boom
    await svc.on_outgoing("ivan", "сдан спринт 1", "2026-08-19T10:00:00+00:00")
    # сообщение всё равно залогировано, бот не упал
    assert (await repo.last_message_ts("ivan")) == "2026-08-19T10:00:00+00:00"


async def test_manual_sheet_status_change_moves_status_since(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sheets = FakeSheets([sm(status="Спринт 4")])
    svc = Service(repo, sheets, FakeLLM(), FakeSender(), FakeKB(), FakeSettings())
    await svc.sync_mentees()
    await repo.set_status_since("ivan", "2026-06-01T10:00:00+00:00")   # старое подтверждение кнопкой
    sheets.mentees[0].status = "Резюме"                                  # ментор поменял руками
    await svc.sync_mentees()
    rec = await repo.get_mentee("ivan")
    assert rec["status_since"] > "2026-09-01"
    await repo.close()


async def test_no_proposal_when_status_unchanged(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sender = FakeSender()
    llm = FakeLLM(kind="progress", status=StatusUpdate(new_status="3 спринт", confidence="high"))
    svc = Service(repo, FakeSheets([sm()]), llm, sender, FakeKB(), FakeSettings())
    await svc.sync_mentees()
    await svc.handle_buffered("ivan", "я всё ещё на третьем", "2026-08-19T10:00:00+00:00")
    assert sender.mentor_msgs == []
    await repo.close()


async def test_manual_answer_is_remembered_and_reused_for_similar_question(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    llm = FakeLLM(kind="question")
    sender = FakeSender()
    svc = Service(repo, FakeSheets([sm()]), llm, sender, FakeKB(), FakeSettings())
    await svc.sync_mentees()
    # первый вопрос: черновик ментор не отправил, а ответил в чате сам
    await svc.handle_buffered("ivan", "как закрыть канал?", "2026-08-19T10:00:00+00:00")
    await svc.on_outgoing("ivan", "закрывает канал только отправитель, получатель читает до закрытия", "2026-08-19T10:05:00+00:00")
    rows = await repo.answered_questions()
    assert [r["final"] for r in rows] == ["закрывает канал только отправитель, получатель читает до закрытия"]
    # похожий вопрос (FakeLLM.embed даёт тот же вектор) — прошлый ответ ушёл в контекст черновика
    await svc.handle_buffered("ivan", "кто закрывает канал?", "2026-08-20T10:00:00+00:00")
    assert [s["final"] for s in llm.draft_ctx["similar"]] == ["закрывает канал только отправитель, получатель читает до закрытия"]
    assert "похожее: 1" in sender.mentor_msgs[-1][0]
    await repo.close()


async def test_dissimilar_answers_are_not_used(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    llm = FakeLLM(kind="question")
    svc = Service(repo, FakeSheets([sm()]), llm, FakeSender(), FakeKB(), FakeSettings())
    await svc.sync_mentees()
    qid = await repo.add_question("ivan", "про мапы", "ч", "2026-08-19T10:00:00+00:00", emb=[0.0, 1.0])
    await repo.set_question_final(qid, "ответ про мапы")
    await svc.handle_buffered("ivan", "про каналы", "2026-08-20T10:00:00+00:00")   # эмбеддинг [1, 0]
    assert llm.draft_ctx["similar"] == []
    await repo.close()


class InterviewLLM(FakeLLM):
    def __init__(self):
        super().__init__(kind="other")
        self.extract_calls = 0

    async def extract_interview(self, text):
        from mentor_bot.llm import InterviewItem, InterviewReport
        self.extract_calls += 1
        return InterviewReport(items=[
            InterviewItem(company="Ozon", stage="техничка", question="как устроен map", failed=True),
            InterviewItem(company="Ozon", stage="техничка", question="что такое горутина", failed=False),
        ])

    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


async def test_interview_feedback_is_collected(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    llm, sender = InterviewLLM(), FakeSender()
    svc = Service(repo, FakeSheets([sm(status="Собесы")]), llm, sender, FakeKB(), FakeSettings())
    await svc.sync_mentees()
    await svc.handle_buffered("ivan", "был на техничке в Ozon, спросили про map — поплыл",
                              "2026-08-19T10:00:00+00:00")
    assert [r["question"] for r in await repo.interview_questions()] == ["как устроен map"]
    assert "Срезался на: как устроен map" in sender.mentor_msgs[-1][0]
    await repo.close()


async def test_broken_interview_extraction_does_not_lose_the_question(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    llm, sender = InterviewLLM(), FakeSender()
    llm.kinds, llm.needs_reply = ["tech_question"], True

    async def boom(text):
        raise ValueError("refusal")

    llm.extract_interview = boom
    svc = Service(repo, FakeSheets([sm(status="Собесы")]), llm, sender, FakeKB(), FakeSettings())
    await svc.sync_mentees()
    await svc.handle_buffered("ivan", "был собес. Как работают каналы?", "2026-08-19T10:00:00+00:00")
    assert len(await repo.open_questions()) == 1          # черновик на вопрос всё равно создан
    await repo.close()


async def test_duplicate_username_does_not_flap_status(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sheets = FakeSheets([sm(status="Спринт 2"), sm(status="оффер")])   # один ник в двух строках
    svc = Service(repo, sheets, FakeLLM(), FakeSender(), FakeKB(), FakeSettings())
    for _ in range(3):
        await svc.sync_mentees()
    assert len(await repo.status_history("ivan")) == 1
    await repo.close()


async def test_short_holding_reply_is_not_the_answer(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    svc = Service(repo, FakeSheets([sm()]), FakeLLM(kind="question"), FakeSender(), FakeKB(),
                  FakeSettings())
    await svc.sync_mentees()
    await svc.handle_buffered("ivan", "как закрыть канал?", "2026-08-19T10:00:00+00:00")
    await svc.on_outgoing("ivan", "щас гляну", "2026-08-19T10:01:00+00:00")
    real = "закрывает канал только отправитель, получатель читает до закрытия"
    await svc.on_outgoing("ivan", real, "2026-08-19T10:20:00+00:00")
    assert [r["final"] for r in await repo.answered_questions()] == [real]
    await repo.close()


async def test_interview_extraction_skipped_on_sprint_stage(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    llm = InterviewLLM()
    svc = Service(repo, FakeSheets([sm(status="Спринт 2")]), llm, FakeSender(), FakeKB(), FakeSettings())
    await svc.sync_mentees()
    await svc.handle_buffered("ivan", "в задаче спринта спросил бы про map", "2026-08-19T10:00:00+00:00")
    assert llm.extract_calls == 0
    await repo.close()


# --- разбор сообщений: эмоции, успехи, вопросы, регламент созвонов ---

TS = "2026-08-19T10:00:00+00:00"


async def make_triaged(tmp_path, sheet_status="3 спринт", **llm_kw):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sender, kb, llm = FakeSender(), FakeKB(), FakeLLM(**llm_kw)
    svc = Service(repo, FakeSheets([sm(status=sheet_status)]), llm, sender, kb, FakeSettings())
    await svc.sync_mentees()
    return repo, sender, kb, llm, svc


async def test_feelings_get_a_draft_without_touching_the_knowledge_base(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["feelings"])
    await svc.handle_buffered("ivan", "чёт выгорел, ничего не лезет", TS)
    assert kb.calls == []                                   # на настроение Go-материалы не ищем
    assert llm.draft_calls[0]["chunks"] is None
    assert llm.draft_calls[0]["call"] is None
    [q] = await repo.open_questions()
    assert q["kind"] == "human" and q["emb"] is not None   # эмбеддинг нужен для похожих ответов
    text, kb_markup = sender.mentor_msgs[0]
    assert text.startswith("💬 @ivan делится:\nчёт выгорел")
    assert [b.callback_data for b in kb_markup.inline_keyboard[0]] == [
        f"q:send:{q['id']}", f"q:edit:{q['id']}", f"q:ign:{q['id']}",
    ]
    assert llm.status_calls == []


async def test_tech_question_uses_the_knowledge_base(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["tech_question"])
    await svc.handle_buffered("ivan", "почему тут дедлок?", TS)
    assert kb.calls == ["почему тут дедлок?"]
    assert llm.draft_calls[0]["chunks"] == [{"text": "из материалов", "source": "урок «Каналы»"}]
    assert (await repo.open_questions())[0]["kind"] == "question"
    assert sender.mentor_msgs[0][0].startswith("❓ @ivan спрашивает:")


async def test_mixed_feelings_and_question_answers_both(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["feelings", "tech_question"])
    await svc.handle_buffered("ivan", "чувствую себя тупым, не понимаю каналы", TS)
    assert kb.calls                                          # техническая часть — по материалам
    assert llm.draft_calls[0]["kinds"] == ["feelings", "tech_question"]
    assert sender.mentor_msgs[0][0].startswith("💬 @ivan делится и спрашивает:")


async def test_win_with_status_change_gives_draft_and_proposal(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, kinds=["win", "status_change"], milestone="sprint_finished",
        status=StatusUpdate(new_status="Спринт 4", confidence="high"),
        drafts=["Красава! Давай договоримся, когда созвонимся на собес по спринту"],
    )
    await svc.handle_buffered("ivan", "сдал третий спринт!", TS)
    assert kb.calls == []
    assert llm.draft_calls[0]["call"] == "sprint"
    assert len(llm.draft_calls) == 1                          # созвон по регламенту — не переписываем
    texts = [t for t, _ in sender.mentor_msgs]
    assert texts[0].startswith("🎉 @ivan:") and "⚠️" not in texts[0]
    assert "Сменить статус «3 спринт» → «Спринт 4»" in texts[1]
    assert (await repo.get_proposal(1))["new_status"] == "Спринт 4"


async def test_smalltalk_produces_nothing(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["smalltalk"])
    await svc.handle_buffered("ivan", "спасибо!", TS)
    assert sender.mentor_msgs == [] and await repo.open_questions() == []
    assert llm.draft_calls == [] and llm.status_calls == []


async def test_reply_kind_without_needs_reply_gets_no_draft(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["win"], needs_reply=False)
    await svc.handle_buffered("ivan", "ок, понял, работает", TS)
    assert sender.mentor_msgs == []


async def test_urgent_message_is_flagged_and_always_drafted(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, kinds=["smalltalk"], needs_reply=False, urgent=True,
    )
    await svc.handle_buffered("ivan", "всё, я больше не могу, бросаю", TS)
    text = sender.mentor_msgs[0][0]
    assert text.startswith("🔥 💬 @ivan делится:")
    assert llm.draft_calls[0]["urgent"] is True


async def test_call_offer_outside_regulation_is_rewritten_once(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, kinds=["tech_question"],
        drafts=["Давай созвонимся и разберём каналы", "Скинь код и текст ошибки, гляну"],
    )
    await svc.handle_buffered("ivan", "застрял на каналах", TS)
    assert len(llm.draft_calls) == 2
    assert llm.draft_calls[1]["avoid"][0].startswith("предлагает созвон")
    assert (await repo.open_questions())[0]["draft"] == "Скинь код и текст ошибки, гляну"
    assert "⚠️" not in sender.mentor_msgs[0][0]


async def test_stubborn_call_offer_reaches_mentor_with_warning(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, kinds=["tech_question"],
        drafts=["Давай созвонимся", "Ну давай тогда в зум на полчаса"],
    )
    await svc.handle_buffered("ivan", "застрял на каналах", TS)
    assert len(llm.draft_calls) == 2                          # переписываем один раз, не больше
    text = sender.mentor_msgs[0][0]
    assert "⚠️ проверь: предлагает созвон" in text
    # предупреждение — только ментору: ученику по кнопке уйдёт чистый черновик
    assert (await repo.open_questions())[0]["draft"] == "Ну давай тогда в зум на полчаса"


async def test_cliches_trigger_rewrite(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, kinds=["feelings"],
        drafts=["Понимаю, как тебе непросто. Не переживай!", "Бывает. Отдохни вечер, завтра добьём"],
    )
    await svc.handle_buffered("ivan", "что-то сил ноль", TS)
    assert "«не переживай»" in llm.draft_calls[1]["avoid"]
    assert (await repo.open_questions())[0]["draft"] == "Бывает. Отдохни вечер, завтра добьём"


async def test_mock_stage_allows_offering_the_mock(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, sheet_status="Мок", kinds=["org_question"], drafts=["Го созвонимся на мок в четверг?"],
    )
    await svc.handle_buffered("ivan", "когда мок?", TS)
    assert llm.draft_calls[0]["call"] == "mock" and len(llm.draft_calls) == 1


async def test_legend_ready_allows_offering_the_mock(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, sheet_status="Легенда", kinds=["win", "status_change"], milestone="legend_ready",
    )
    await svc.handle_buffered("ivan", "легенду дописал", TS)
    assert llm.draft_calls[0]["call"] == "mock"


async def test_milestone_alone_still_proposes_status(tmp_path):
    # модель забыла status_change при «сдал спринт» — предложение статуса всё равно придёт
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, kinds=["win"], milestone="sprint_finished",
        status=StatusUpdate(new_status="Спринт 4", confidence="high"),
    )
    await svc.handle_buffered("ivan", "сдал третий!", TS)
    assert (await repo.get_proposal(1))["new_status"] == "Спринт 4"


async def test_draft_sees_dialog_notes_stage_and_mentor_style(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["feelings"])
    svc.by_username["ivan"].notes = "работает сменами, по выходным не трогать"
    await svc.on_outgoing("ivan", "ну чё, как каналы, разобрался?", "2026-08-19T09:00:00+00:00")
    await svc.on_incoming("ivan", "неа", "2026-08-19T10:00:00+00:00")
    await svc.on_incoming("ivan", "чёт приуныл", "2026-08-19T10:01:00+00:00")
    await svc.handle_buffered("ivan", "неа\nчёт приуныл", "2026-08-19T10:01:00+00:00")
    ctx = llm.draft_calls[0]
    # новые сообщения в переписку не попадают — модель видит их один раз, отдельно
    assert [m["text"] for m in ctx["recent"]] == ["ну чё, как каналы, разобрался?"]
    assert [m["text"] for m in llm.triage_calls[0]["recent"]] == ["ну чё, как каналы, разобрался?"]
    assert llm.triage_calls[0]["status"] == "3 спринт"
    assert ctx["notes"] == "работает сменами, по выходным не трогать"
    assert ctx["stage_label"] == "3-й спринт обучения" and ctx["status"] == "3 спринт"
    assert ctx["samples"] == ["ну чё, как каналы, разобрался?"]
    assert ctx["ongoing"] is True                              # ментор писал час назад


async def test_mentee_greeting_lets_draft_greet_back(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["tech_question"])
    await svc.on_outgoing("ivan", "ну чё, как каналы, разобрался?", "2026-08-19T09:00:00+00:00")
    await svc.handle_buffered("ivan", "Привет! а select блокируется?", TS)
    assert llm.draft_calls[0]["ongoing"] is False


async def test_stage_line_with_days_on_stage(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["win"])
    await repo.set_status_since("ivan", "2026-08-07T09:00:00+00:00")
    await svc.handle_buffered("ivan", "дошло, как работает select!", TS)
    assert "\n📍 3-й спринт обучения · на стадии 12 дн.\n" in sender.mentor_msgs[0][0]


async def test_stage_line_skipped_when_stage_unknown(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, sheet_status=None, kinds=["win"])
    await svc.handle_buffered("ivan", "дошло, как работает select!", TS)
    assert "📍" not in sender.mentor_msgs[0][0]


async def test_em_dash_replaced_when_mentor_never_types_it(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(
        tmp_path, kinds=["feelings"], drafts=["Бывает — отдохни денёк"],
    )
    await repo.log_message("ivan", "out", "скинь код - гляну вечером", "2026-08-10T10:00:00+00:00")
    await svc.handle_buffered("ivan", "устал", TS)
    assert (await repo.open_questions())[0]["draft"] == "Бывает - отдохни денёк"


async def test_human_draft_learns_only_from_human_edits(tmp_path):
    repo, sender, kb, llm, svc = await make_triaged(tmp_path, kinds=["feelings"])
    tq = await repo.add_question("petr", "как закрыть канал?", "ч1", "2026-08-01T10:00:00+00:00")
    await repo.set_question_final(tq, "close(ch) делает отправитель")
    hq = await repo.add_question("petr", "устал", "ч2", "2026-08-01T11:00:00+00:00", kind="human")
    await repo.set_question_final(hq, "отдохни, потом добьём")
    await repo.close_open_questions("petr")
    await svc.handle_buffered("ivan", "что-то сил нет", TS)
    assert [e["final"] for e in llm.draft_calls[0]["examples"]] == ["отдохни, потом добьём"]
