from datetime import date

from mentor_bot.llm import StatusUpdate
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

    async def set_status(self, m, s):
        self.statuses.append((m.username, s))

    async def append_mentee(self, title, display):
        pass


class FakeLLM:
    def __init__(self, kind="other", status=None):
        self.kind = kind
        self.status = status or StatusUpdate(new_status=None, confidence="low")

    async def classify(self, text):
        return self.kind

    async def parse_status(self, text, current):
        return self.status

    async def draft_answer(self, q, chunks, profile):
        return f"ЧЕРНОВИК[{q}]"

    async def update_profile(self, old, recent):
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
    def search(self, q, emb, k=5):
        return ["из материалов"]


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


async def test_question_creates_draft(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path, kind="question")
    await svc.on_incoming("ivan", "что такое mutex?", "2026-08-19T10:00:00+00:00")
    qs = await repo.open_questions()
    assert len(qs) == 1 and qs[0]["draft"] == "ЧЕРНОВИК[что такое mutex?]"
    assert any("ЧЕРНОВИК" in m[0] for m in sender.mentor_msgs)


async def test_progress_high_confidence_writes_status(tmp_path):
    repo, sheets, sender, svc = await make(
        tmp_path, kind="progress", status=StatusUpdate(new_status="Собесы", confidence="high")
    )
    await svc.on_incoming("ivan", "вышел на собесы", "2026-08-19T10:00:00+00:00")
    assert sheets.statuses == [("ivan", "Собесы")]


async def test_progress_low_confidence_creates_proposal(tmp_path):
    repo, sheets, sender, svc = await make(
        tmp_path, kind="progress", status=StatusUpdate(new_status="Собесы", confidence="low")
    )
    await svc.on_incoming("ivan", "мб начну собеситься", "2026-08-19T10:00:00+00:00")
    assert sheets.statuses == []
    assert (await repo.get_proposal(1))["new_status"] == "Собесы"


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


async def test_other_message_does_not_update_profile(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)  # kind="other" по умолчанию
    await svc.on_incoming("ivan", "ок", "2026-08-19T10:00:00+00:00")
    assert await repo.get_profile("ivan") is None


async def test_question_updates_profile(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path, kind="question")
    await svc.on_incoming("ivan", "что такое mutex?", "2026-08-19T10:00:00+00:00")
    assert await repo.get_profile("ivan") == "досье"


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
