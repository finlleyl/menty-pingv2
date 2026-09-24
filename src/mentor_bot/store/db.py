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
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  direction TEXT NOT NULL,
  text TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages(username, ts);
CREATE TABLE IF NOT EXISTS pings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  ts TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  question TEXT NOT NULL,
  draft TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  reminded INTEGER NOT NULL DEFAULT 0,
  final TEXT,
  emb TEXT
);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  new_status TEXT NOT NULL,
  from_status TEXT
);
CREATE TABLE IF NOT EXISTS profiles(
  username TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  updated_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending(
  username   TEXT PRIMARY KEY,
  last_in_ts TEXT NOT NULL,
  texts      TEXT NOT NULL
);
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
  emb TEXT
);
CREATE INDEX IF NOT EXISTS idx_interview_notes_user_ts ON interview_notes(username, source_ts);
CREATE TABLE IF NOT EXISTS ping_drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  text TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS llm_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  task TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL
);
"""
