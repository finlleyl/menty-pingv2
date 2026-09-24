"""Оценка поиска по базе знаний: python -m mentor_bot.eval_kb cases.jsonl [k]

cases.jsonl — по строке на вопрос: {"question": "...", "expected": "Каналы"}, где
expected — кусок названия урока (источника) или текста нужного фрагмента.
Печатает recall@k и вопросы, для которых нужный фрагмент в top-k не попал.
Меняешь токенизацию, чанкинг или RRF — прогоняешь и сравниваешь цифру, а не впечатления."""
import asyncio
import json
import sys

from mentor_bot.config import load_settings
from mentor_bot.kb import KBIndex, recall_at_k
from mentor_bot.llm import LLM


async def main(path: str, k: int):
    settings = load_settings()
    kb = KBIndex(settings.kb_path)
    if not kb.load():
        sys.exit("база знаний пуста — сначала /reindex")
    with open(path) as f:
        cases = [json.loads(line) for line in f if line.strip()]
    llm = LLM(settings.llm_api_key, settings.llm_model_smart, settings.llm_model_fast,
              settings.embed_model, base_url=settings.llm_base_url)
    embs = []
    for i in range(0, len(cases), 100):
        embs.extend(await llm.embed([c["question"] for c in cases[i:i + 100]]))
    recall, misses = recall_at_k(kb, cases, embs, k=k)
    print(f"recall@{k} = {recall:.2%} ({len(cases) - len(misses)}/{len(cases)})")
    for q in misses:
        print(f"  промах: {q}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5))
