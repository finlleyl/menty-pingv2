# Полная схема для свежей базы. Старые базы догоняют миграции из repo.MIGRATIONS: индекс по
# колонке, которую добавляет миграция, сюда не кладём — на старой базе её ещё нет.
SCHEMA = """
CREATE TABLE IF NOT EXISTS mentees(
  username TEXT PRIMARY KEY,
  chat_id INTEGER,
  sheet_title TEXT,
  row INTEGER,
  paused_until TEXT,
  unanswered_pings INTEGER NOT NULL DEFAULT 0,
  status_since TEXT,
  last_status TEXT
);
-- source: 'chat' — написано в Telegram руками (ментором или учеником); 'bot' — текст модели,
-- отправленный ботом (пинг, черновик как есть); 'bot_edit' — правка ментора, отправленная ботом;
-- 'auto' — автоответ Telegram Business или отложенное сообщение
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  direction TEXT NOT NULL,
  text TEXT NOT NULL,
  ts TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'chat',
  tg_id INTEGER,            -- message_id в Telegram: повторно доставленный апдейт не задвоится
  reply_to_tg_id INTEGER    -- на какое сообщение чата это ответ
);
CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages(username, ts);
CREATE TABLE IF NOT EXISTS pings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  ts TEXT NOT NULL,
  status TEXT NOT NULL
);
-- без индекса style_samples на каждый черновик сканирует pings для каждого исходящего
CREATE INDEX IF NOT EXISTS idx_pings_user_ts ON pings(username, ts);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  question TEXT NOT NULL,
  draft TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  reminded INTEGER NOT NULL DEFAULT 0,
  final TEXT,
  emb BLOB,
  kind TEXT NOT NULL DEFAULT 'question',
  emb_model TEXT,
  msg_ids TEXT,             -- JSON: message_id сообщений ученика, из которых собран вопрос
  card_msg_id INTEGER       -- карточка в личке ментора: после действия редактируем её
);
CREATE INDEX IF NOT EXISTS idx_questions_user_state ON questions(username, state);
-- style_samples отсекает черновики, ушедшие как есть, сравнением текста — без индекса это скан
CREATE INDEX IF NOT EXISTS idx_questions_draft ON questions(draft);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  new_status TEXT NOT NULL,
  from_status TEXT,
  card_msg_id INTEGER,
  created_ts TEXT
);
CREATE TABLE IF NOT EXISTS profiles(
  username TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  updated_ts TEXT NOT NULL
);
-- окно дебаунса: когда ученик последний раз что-то присылал (texts — от старого формата буфера)
CREATE TABLE IF NOT EXISTS pending(
  username   TEXT PRIMARY KEY,
  last_in_ts TEXT NOT NULL,
  texts      TEXT NOT NULL
);
-- сами сообщения буфера — по строке на сообщение: дописать и снять разобранное можно одним
-- запросом, без чтения-изменения-записи, и параллельные апдейты не затирают друг друга
CREATE TABLE IF NOT EXISTS pending_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  text TEXT NOT NULL,
  ts TEXT NOT NULL,
  tg_id INTEGER,
  reply_to TEXT             -- на что ученик ответил: «Ментор: …» — контекст для черновика
);
CREATE INDEX IF NOT EXISTS idx_pending_messages_user ON pending_messages(username, id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  ts TEXT NOT NULL,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_history_user_ts ON status_history(username, ts);
CREATE TABLE IF NOT EXISTS interview_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  source_ts TEXT NOT NULL,
  company TEXT,
  stage TEXT,
  question TEXT NOT NULL,
  failed INTEGER NOT NULL DEFAULT 0,
  emb BLOB,
  emb_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_interview_notes_user_ts ON interview_notes(username, source_ts);
CREATE TABLE IF NOT EXISTS ping_drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  text TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  card_msg_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ping_drafts_user_state ON ping_drafts(username, state);
CREATE TABLE IF NOT EXISTS llm_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  task TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""
