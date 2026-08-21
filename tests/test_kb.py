from mentor_bot.kb import KBIndex, html_to_chunks

HTML = """
<html><body><nav>меню</nav>
<h1>Горутины</h1><p>Горутина — легковесный поток. Запуск: go f().</p>
<h1>Каналы</h1><p>Канал — способ связи горутин. make(chan int).</p>
</body></html>
"""


def test_html_to_chunks():
    chunks = html_to_chunks(HTML)
    assert len(chunks) >= 2
    assert any("Горутина" in c for c in chunks)
    assert any("Канал" in c for c in chunks)


def fake_emb(text: str) -> list[float]:
    # игрушечный эмбеддинг: частоты букв
    return [float(text.lower().count(ch)) for ch in "абвгк каналгорутин"]


def test_index_search(tmp_path):
    chunks = ["Горутина — легковесный поток", "Канал — связь горутин", "Слайсы и мапы"]
    idx = KBIndex(str(tmp_path))
    idx.build(chunks, [fake_emb(c) for c in chunks])

    idx2 = KBIndex(str(tmp_path))
    assert idx2.load()
    got = idx2.search("что такое канал", fake_emb("что такое канал"), k=2)
    assert "Канал — связь горутин" in got


def test_index_load_missing(tmp_path):
    assert KBIndex(str(tmp_path / "nope")).load() is False


def test_empty_index_build_and_load(tmp_path):
    idx = KBIndex(str(tmp_path))
    idx.build([], [])                     # не должно падать
    assert idx.search("что-то", [0.0]) == []
    idx2 = KBIndex(str(tmp_path))
    assert idx2.load() is True
    assert idx2.search("что-то", [0.0]) == []


def test_long_paragraph_splits_on_word_boundary():
    long_html = "<html><body><h1>Тема</h1><p>" + ("слово " * 600) + "</p></body></html>"
    chunks = html_to_chunks(long_html)
    assert len(chunks) >= 2
    for c in chunks:
        assert not c.endswith("слов")      # нет разреза посреди слова
        assert "словослово" not in c
