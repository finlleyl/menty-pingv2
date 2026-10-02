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


async def test_record_status_initial_then_transition(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    # первое наблюдение: когда ученик попал в статус — неизвестно, status_since не трогаем
    assert await repo.record_status("ivan", "Спринт 1", "2026-08-01T10:00:00+00:00", "sheet") is False
    assert (await repo.get_mentee("ivan"))["status_since"] is None
    assert await repo.record_status("ivan", "Спринт 1", "2026-08-02T10:00:00+00:00", "sheet") is False
    assert await repo.record_status("ivan", "Спринт 2", "2026-08-03T10:00:00+00:00", "sheet") is True
    rec = await repo.get_mentee("ivan")
    assert rec["status_since"] == "2026-08-03T10:00:00+00:00" and rec["last_status"] == "Спринт 2"
    assert [h["source"] for h in await repo.status_history("ivan")] == ["initial", "sheet"]
    await repo.close()


async def test_proposal_keeps_from_status(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    pid = await repo.add_proposal("ivan", "Резюме", from_status="Спринт 4")
    assert (await repo.get_proposal(pid))["from_status"] == "Спринт 4"
    await repo.close()


async def test_blank_cell_and_rename_do_not_reset_stage_timer(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.record_status("ivan", "Спринт 1", "2026-08-01T00:00:00+00:00", "sheet")
    await repo.record_status("ivan", "Спринт 3", "2026-08-02T00:00:00+00:00", "sheet")
    assert await repo.record_status("ivan", "", "2026-08-03T00:00:00+00:00", "sheet") is False
    assert await repo.record_status("ivan", "3 спринт", "2026-08-04T00:00:00+00:00", "sheet") is False
    rec = await repo.get_mentee("ivan")
    assert rec["status_since"] == "2026-08-02T00:00:00+00:00" and rec["last_status"] == "3 спринт"
    await repo.close()


async def test_claim_is_single_winner(tmp_path):
    import asyncio
    repo = await Repo.open(str(tmp_path / "t.db"))
    pid = await repo.add_ping_draft("ivan", "т", "2026-08-01T00:00:00+00:00")
    got = await asyncio.gather(repo.claim("ping_drafts", pid), repo.claim("ping_drafts", pid))
    assert sorted(got) == [False, True]
    await repo.close()


async def test_backup_with_uncommitted_write(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo._c.execute("INSERT INTO settings(key, value) VALUES ('x', '1')")   # без commit
    await repo.backup_to(str(tmp_path / "copy.db"))
    assert (tmp_path / "copy.db").exists()
    await repo.close()


async def test_style_samples_are_real_mentor_messages(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    ts = iter(f"2026-09-01T10:{i:02d}:00+00:00" for i in range(60))
    await repo.log_message("ivan", "out", "скинь код, гляну вечером", next(ts))
    await repo.log_message("ivan", "out", "ок", next(ts))                         # слишком коротко
    await repo.log_message("ivan", "out", "[медиа]", next(ts))
    await repo.log_message("ivan", "out", "х" * 700, next(ts))                     # простыня
    await repo.log_message("ivan", "in", "а как закрыть канал правильно?", next(ts))   # не ментор
    # пинг, который отправил бот, — текст модели: в образцы не идёт
    ping_ts = next(ts)
    await repo.log_ping("ivan", ping_ts, "sent")
    await repo.log_message("ivan", "out", "Как там спринт, где застрял?", ping_ts)
    # черновик, ушедший как есть, — тоже текст модели
    await repo.add_question("ivan", "вопрос", "Канал закрывает отправитель, не получатель", next(ts))
    await repo.log_message("ivan", "out", "Канал закрывает отправитель, не получатель", next(ts))
    await repo.log_message("petr", "out", "Скинь код, гляну вечером", next(ts))     # дубль другим регистром
    await repo.log_message("petr", "out", "красава, го дальше по плану", next(ts))
    assert await repo.style_samples() == [
        "красава, го дальше по плану", "Скинь код, гляну вечером",
    ]
    await repo.close()


async def test_style_samples_cap_per_mentee_and_total(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    for i in range(5):
        await repo.log_message("ivan", "out", f"ответ ивану номер {i}", f"2026-09-01T10:0{i}:00+00:00")
    for i in range(5):
        await repo.log_message("petr", "out", f"ответ пете номер {i}", f"2026-09-01T09:0{i}:00+00:00")
    got = await repo.style_samples(limit=3, per_user=2)
    # свежие первыми, но не больше двух на ученика — тон не задаёт один длинный диалог
    assert got == ["ответ ивану номер 4", "ответ ивану номер 3", "ответ пете номер 4"]
    await repo.close()


async def test_question_kind_migrates_on_old_database(tmp_path):
    import aiosqlite

    path = str(tmp_path / "old.db")
    # таблица questions в том виде, в каком она жила до разбора эмоций (ещё и без final/emb)
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "CREATE TABLE questions(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, "
        "question TEXT NOT NULL, draft TEXT NOT NULL, created_ts TEXT NOT NULL, "
        "state TEXT NOT NULL DEFAULT 'open', reminded INTEGER NOT NULL DEFAULT 0)"
    )
    await conn.execute(
        "INSERT INTO questions(username, question, draft, created_ts) "
        "VALUES ('ivan', 'как закрыть канал?', 'черновик', '2026-08-01T10:00:00+00:00')"
    )
    await conn.commit()
    await conn.close()

    repo = await Repo.open(path)
    old = await repo.get_question(1)
    assert old["kind"] == "question" and old["final"] is None    # старый черновик — ответ на вопрос
    await repo.set_question_final(1, "закрывает отправитель")
    hid = await repo.add_question("ivan", "устал", "бывает", "2026-08-02T10:00:00+00:00", kind="human")
    await repo.set_question_final(hid, "отдохни денёк, потом добьём")
    # правки не смешиваются: тёплые ответы не учат отвечать про каналы и наоборот
    assert [e["final"] for e in await repo.edit_examples(kind="question")] == ["закрывает отправитель"]
    assert [e["final"] for e in await repo.edit_examples(kind="human")] == ["отдохни денёк, потом добьём"]
    assert len(await repo.edit_examples()) == 2
    await repo.close()
    # повторное открытие не пытается добавить колонку второй раз
    repo = await Repo.open(path)
    assert (await repo.get_question(hid))["kind"] == "human"
    await repo.close()
