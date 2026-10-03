import pytest

from mentor_bot.style import ai_tells, call_hits, draft_problems, normalize_dashes, todo_marks


@pytest.mark.parametrize("text", [
    "Давай созвонимся завтра и разберём",
    "Можем созвон в четверг устроить",
    "позвоню тебе вечером",
    "Давай я перезвоню",
    "Кинь ссылку на зум",
    "го в zoom",
    "Скинь ссылку на google meet",
    "Давай в дискорде обсудим",
    "го в discord",
    "лучше голосом обсудим",
    "можно голосовой чат поднять",
    "давай по видеосвязи",
    "давай встретимся и разберём",
    "назначим встречу на пятницу",
    "при встрече покажу",
    "можем встречаться раз в неделю",
    "закинь call на завтра",
    "давай колл в 19",
    "Телемост подойдёт?",
    "Набери, обсудим по звонку",
    "Скину ссылку на телемосте",
    "Давай созвонимся с тобой в четверг",
    # синонимы, которыми модель заменяет запрещённое слово при переписке
    "Давай я тебе наберу",
    "Набери меня вечером",
    "Давай наберу тебя вечером",
    "обсудим по видео",
    "Обсудим по видео?",
    "Скинь ссылку на гугл мит",
    "Можем вживую разобрать, расшарю экран",
    "Давай в войсе обсудим",
    # повторный мок и «пробный собес» — тоже созвон вне регламента
    "Давай прогоним ещё один мок по алгоритмам",
    "Давай устроим пробный собес в четверг",
    "Можем потренироваться вместе перед собесом",
])
def test_call_hits_catches_call_offers(text):
    assert call_hits(text), text


@pytest.mark.parametrize("text", [
    "Этот паттерн встречается в уроке про каналы",           # «встречается» = попадается
    "Я тоже встретил эту ошибку на первом спринте",
    "Спроси у коллеги, он такое делал",
    "Коллекция горутин растёт, глянь утечку",
    "Это всё зумеры придумали",
    "Посмотри call stack в панике",
    "Тут function call через интерфейс, поэтому аллокация",
    "Вызов идёт через `reflect.Value.Call(args)`",
    "```go\nresp := client.Call(ctx)\n```",
    "Как позвоночник после восьми часов за ноутом?",
    "Отправь код и текст ошибки, гляну",
    # собес ученика, а не созвон с ментором — на «Собесах» и «Рынке» об этом спрашивают законно
    "Как прошёл созвон с HR?",
    "Перед созвоном с тимлидом повтори каналы",
    "На встрече с работодателем спросят про опыт",
    "Жду, когда будет звонок от рекрутера",
    # третье лицо и прошедшее — рассказ о чужом звонке, а не предложение созвониться
    "Рекрутеры звонят?",
    "Если тебе позвонят из Тинькофф, спроси про вилку",
    "Когда позвонит рекрутер, спроси про вилку",
    "Метод Call у интерфейса принимает контекст",
    "Набери go env в терминале",
    "По видео из урока про каналы всё понятно?",
    # mock-объект из тестов — это не мок-собес
    "Сделай мок для репозитория и прогони тесты с моком",
    "После легенды будет мок-собес",
    # пометка ментору — вопрос ему, а не предложение созвона ученику
    "Скинь код, на чём встал. [просит созвон - реши сам]",
])
def test_call_hits_ignores_lookalikes(text):
    assert call_hits(text) == [], text


def test_ai_tells_finds_cliches_and_markdown():
    text = ("Отличный вопрос! Понимаю, как тебе непросто. **Главное** — не переживай.\n"
            "## Итог\nНадеюсь, это поможет, обращайся!")
    tells = ai_tells(text, ["feelings"])
    for label in ("«отличный вопрос»", "«понимаю, как тебе непросто»", "«не переживай»",
                  "markdown: **жирный**", "markdown: заголовки", "«надеюсь, это поможет»",
                  "«не стесняйся/обращайся»"):
        assert label in tells, label


def test_ai_tells_therapist_mirroring_and_triple_cheer():
    tells = ai_tells("Похоже, ты чувствуешь усталость. Это нормально чувствовать такое.", ["feelings"])
    assert "отзеркаливание чувств, как у психолога" in tells
    cheer = ai_tells("Молодец! Так держать! Всё получится!", ["win"])
    assert any("три восклицания" in t for t in cheer)
    assert not any("три восклицания" in t for t in ai_tells("Красава!!! Го дальше", ["win"]))
    # «!=» в Go-коде без бэктиков — не восклицания
    code = "Проверь if err != nil, потом ok != true, и если v != nil - паника"
    assert ai_tells(code, ["tech_question"]) == []


def test_mentor_own_loud_style_is_not_a_cliche():
    loud = "Красава! Поздравляю! Теперь резюме, я займусь! 🔥🔥🔥"
    assert ai_tells(loud, ["win"], samples=["го дальше"]) == [
        "эмодзи пачкой", "три восклицания: «Молодец! Так держать! Всё получится!»",
    ]
    # ментор сам так пишет — копировать его пунктуацию и эмодзи и просили
    assert ai_tells(loud, ["win"], samples=["Огонь! Красава! 🚀🚀"]) == []


@pytest.mark.parametrize("text, label", [
    ("Это абсолютно нормально, все через это проходят.", "«абсолютно нормально»"),
    ("Ты справишься!", "«ты справишься»"),
    ("Ты на правильном пути, не сдавайся", "«ты справишься»"),
    ("Помни, ты не один", "«ты справишься»"),
    ("Понимаю тебя. Сам так сидел", "«понимаю тебя»"),
    ("Понимаю. Бывает", "«понимаю тебя»"),
    ("Отличная работа", "«отличная работа»"),
    ("Ты проделал огромную работу", "«отличная работа»"),
    ("Так держать", "«отличная работа»"),
    ("Горжусь тобой", "«отличная работа»"),
])
def test_ai_tells_catches_consolation_cliches(text, label):
    assert label in ai_tells(text, ["feelings"]), text


def test_consolation_lookalikes_are_not_cliches():
    assert ai_tells("Код отлично работает, гоняй тесты", ["tech_question"]) == []
    assert ai_tells("Ты не один такой, на каналах все тупят", ["feelings"]) == []
    assert ai_tells("Не понимаю тебя, что за ошибка? Скинь текст", ["tech_question"]) == []


def test_bullets_flagged_only_outside_technical_replies():
    text = "Смотри:\n- закрывает отправитель\n- читать можно до конца"
    assert "markdown: список" in ai_tells(text, ["feelings"])
    assert "markdown: список" not in ai_tells(text, ["tech_question"])
    assert "markdown: список" not in ai_tells(text, ["feelings", "tech_question"])


def test_ai_tells_leaves_plain_human_text_alone():
    assert ai_tells("Да бывает, у меня на каналах тоже клинило. Скинь код, гляну", ["feelings"]) == []
    # «обращайся к полю» — техника, а не прощальная формула
    assert ai_tells("Обращайся к полю через указатель, иначе копия", ["tech_question"]) == []
    # «данные» в Go — это data, а не канцелярит
    assert ai_tells("Данные в канал пишет одна горутина", ["tech_question"]) == []


def test_greeting_flagged_only_in_ongoing_dialog():
    assert "«Привет!» посреди идущего диалога" in ai_tells("Привет! Скинь код", [], ongoing=True)
    assert ai_tells("Привет! Скинь код", [], ongoing=False) == []


def test_dashes_replaced_when_mentor_never_uses_them():
    samples = ["скинь код - гляну", "го дальше"]
    assert normalize_dashes("Канал — это труба — по ней данные", samples) == \
        "Канал - это труба - по ней данные"
    assert normalize_dashes("— ну что, как ты?", samples) == "- ну что, как ты?"
    assert normalize_dashes("диапазон 1–4 спринта", samples) == "диапазон 1–4 спринта"   # не тире


def test_dashes_kept_when_mentor_uses_them_or_unknown():
    assert normalize_dashes("Канал — труба", ["я тоже ставлю — тире"]) == "Канал — труба"
    assert normalize_dashes("Канал — труба", []) == "Канал — труба"   # образцов нет — не трогаем


def test_draft_problems_respects_call_permission():
    text = "Красава! Давай созвонимся на неделе и проведём собес по спринту"
    assert draft_problems(text, ["win"], call=None)[0].startswith("упоминает созвон")
    assert draft_problems(text, ["win"], call="sprint") == []
    assert draft_problems(text, ["win"], call="mock") == []


def test_todo_marks_found_and_invisible_to_lint():
    text = ("Каналы закрывает отправитель. [по этому в материалах нет — допиши сам]\n"
            "Скинь, на чём встал. [просит созвон - реши сам]")
    assert todo_marks(text) == ["[по этому в материалах нет — допиши сам]",
                                "[просит созвон - реши сам]"]
    assert todo_marks("Скинь код, гляну [вот сюда]") == []
    assert draft_problems(text, ["tech_question"], call=None) == []


def test_leaks_private_catches_quotes_from_notes_and_dossier():
    from mentor_bot.style import leaks_private
    notes = "ленивый, мотивировать деньгами иначе забивает на учёбу"
    profile = "Работает сменами на складе, по выходным не трогать"
    assert leaks_private("Слушай, мотивировать деньгами иначе забивает — так что давай", notes) == [
        "цитата из заметок: «мотивировать деньгами иначе забивает»"]
    assert leaks_private("Знаю, ты работает сменами на складе, но спринт сам себя не сдаст",
                         profile=profile) == ["цитата из досье: «работает сменами на складе»"]
    # общие слова и короткие совпадения — не цитата
    assert leaks_private("Как там спринт, разобрался с каналами?", notes, profile) == []
    assert leaks_private("и в том числе", "и в том числе") == []


def test_leaks_private_catches_other_mentees():
    from mentor_bot.style import leaks_private
    others = [("petr_dev", "Пётр Иванов"), ("sasha", "Саша")]
    assert leaks_private("Вон @petr_dev уже сдал третий", others=others) == ["чужой ученик: @petr_dev"]
    assert leaks_private("Пётр Иванов тоже застревал на каналах", others=others) == [
        "чужой ученик: Пётр Иванов"]
    assert leaks_private("Саша, как дела?", others=others) == []   # одно имя — слишком частое
    assert leaks_private("почта ivan@petr_dev.ru", others=others) == []
