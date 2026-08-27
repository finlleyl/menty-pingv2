from mentor_bot.store.repo import Repo


async def test_mentee_roundtrip(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.upsert_mentee("ivan", chat_id=111, sheet_title="A", row=5)
    await repo.upsert_mentee("ivan", chat_id=222)  # частичный апдейт не затирает
    m = await repo.get_mentee("ivan")
    assert m["chat_id"] == 222 and m["sheet_title"] == "A" and m["row"] == 5
    assert m["unanswered_pings"] == 0
    await repo.bump_unanswered("ivan")
    await repo.bump_unanswered("ivan")
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 2
    await repo.reset_unanswered("ivan")
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 0
    await repo.close()


async def test_messages_questions_settings(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.log_message("ivan", "in", "привет", "2026-08-19T10:00:00+00:00")
    await repo.log_message("ivan", "out", "здарова", "2026-08-20T10:00:00+00:00")
    assert (await repo.last_message_ts("ivan")) == "2026-08-20T10:00:00+00:00"

    qid = await repo.add_question("ivan", "что такое mutex?", "черновик", "2026-08-20T10:00:00+00:00")
    q = await repo.get_question(qid)
    assert q["state"] == "open" and q["question"] == "что такое mutex?"
    await repo.set_question_state(qid, "sent")
    assert (await repo.get_question(qid))["state"] == "sent"

    pid = await repo.add_proposal("ivan", "Собесы")
    assert (await repo.get_proposal(pid))["new_status"] == "Собесы"
    await repo.delete_proposal(pid)
    assert await repo.get_proposal(pid) is None

    assert await repo.get_setting("dryrun", "1") == "1"
    await repo.set_setting("dryrun", "0")
    assert await repo.get_setting("dryrun") == "0"
    await repo.close()


async def test_last_ping_ts_and_close_open_questions(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    assert await repo.last_ping_ts("ivan") is None
    await repo.log_ping("ivan", "2026-08-19T10:00:00+00:00", "attempt")
    await repo.log_ping("ivan", "2026-08-20T10:00:00+00:00", "sent")
    assert await repo.last_ping_ts("ivan") == "2026-08-20T10:00:00+00:00"

    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    other_qid = await repo.add_question("petr", "другой вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    await repo.close_open_questions("ivan")
    assert (await repo.get_question(qid))["state"] == "answered"
    assert (await repo.get_question(other_qid))["state"] == "open"  # чужие вопросы не трогает
    await repo.close()


async def test_stale_profiles(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    # у ivan досье нет вовсе → устарело
    await repo.log_message("ivan", "in", "привет", "2026-08-27T10:00:00+00:00")
    # у petr досье свежее последнего сообщения → не устарело
    await repo.log_message("petr", "in", "привет", "2026-08-27T10:00:00+00:00")
    await repo.set_profile("petr", "досье", "2026-08-27T11:00:00+00:00")
    # у sveta досье старее последнего сообщения → устарело
    await repo.set_profile("sveta", "досье", "2026-08-20T10:00:00+00:00")
    await repo.log_message("sveta", "in", "новое", "2026-08-27T10:00:00+00:00")
    assert sorted(await repo.stale_profiles()) == ["ivan", "sveta"]
    await repo.close()


async def test_status_since_stamped_and_migrated(tmp_path):
    import aiosqlite

    path = str(tmp_path / "old.db")
    # база, созданная до появления колонки status_since
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "CREATE TABLE mentees(username TEXT PRIMARY KEY, chat_id INTEGER, "
        "sheet_title TEXT, row INTEGER, paused_until TEXT, "
        "unanswered_pings INTEGER NOT NULL DEFAULT 0)"
    )
    await conn.execute("INSERT INTO mentees(username) VALUES ('ivan')")
    await conn.commit()
    await conn.close()

    repo = await Repo.open(path)                       # миграция на открытии
    assert (await repo.get_mentee("ivan"))["status_since"] is None
    await repo.set_status_since("ivan", "2026-08-27T10:00:00+00:00")
    assert (await repo.get_mentee("ivan"))["status_since"] == "2026-08-27T10:00:00+00:00"
    await repo.set_status_since("petr", "2026-08-27T10:00:00+00:00")   # ещё не заведён
    assert (await repo.get_mentee("petr"))["status_since"] == "2026-08-27T10:00:00+00:00"
    await repo.close()
