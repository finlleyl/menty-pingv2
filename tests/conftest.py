import asyncio

import pytest

from mentor_bot.store import repo as repo_mod


@pytest.fixture(autouse=True)
def close_leaked_repos(monkeypatch):
    """Закрывает соединения, которые тест открыл и не закрыл.

    aiosqlite 0.22 запускает на каждое соединение non-daemon поток. Упавший тест
    держит ссылку на Repo в traceback, сборщик мусора до него не доходит, поток
    остаётся жив — и интерпретатор виснет на выходе, а не падает с отчётом.
    """
    opened = []
    original_open = repo_mod.Repo.open.__func__

    async def tracking_open(cls, path):
        inst = await original_open(cls, path)
        opened.append(inst)
        return inst

    monkeypatch.setattr(repo_mod.Repo, "open", classmethod(tracking_open))
    yield
    for repo in opened:
        try:
            asyncio.run(repo.close())
        except Exception:
            pass
