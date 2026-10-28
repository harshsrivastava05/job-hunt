-- One row per unique careers URL; this is what the watcher polls.
CREATE TABLE IF NOT EXISTS sources (
  id              INTEGER PRIMARY KEY,
  url             TEXT NOT NULL UNIQUE,
  ats             TEXT,                -- workday, greenhouse, ...; NULL until classified
  ats_key         TEXT,                -- tenant/slug the ATS adapter needs
  active          INTEGER NOT NULL DEFAULT 1,
  interval_min    INTEGER,             -- NULL = settings.interval_min
  next_check_at   TEXT,                -- UTC ISO; NULL = due now
  last_ok_at      TEXT,
  fail_count      INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  list_hash       TEXT,                -- cursor: unchanged job list => skip the source
  bootstrapped_at TEXT,                -- cursor: first crawl is stored as baseline, not judged
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Spreadsheet rows. Several companies can share one source (ABB + ABB India).
-- A source no company points at any more is skipped by the scheduler.
CREATE TABLE IF NOT EXISTS companies (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  city      TEXT NOT NULL DEFAULT '',
  source_id INTEGER NOT NULL REFERENCES sources(id),
  UNIQUE (name, city)
);
CREATE INDEX IF NOT EXISTS companies_source ON companies(source_id);

-- Every posting a source has listed. (source_id, external_id) is the cursor: a job is judged at most once.
-- All timestamps are UTC 'YYYY-MM-DD HH:MM:SS', the same format as SQLite's datetime('now').
CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY,
  source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  external_id   TEXT NOT NULL,
  url           TEXT NOT NULL,
  title         TEXT NOT NULL,
  location      TEXT NOT NULL DEFAULT '',
  posted_at     TEXT,                -- from the board; NULL = it doesn't say
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,       -- equals sources.last_ok_at while the job is still listed
  description   TEXT,                -- kept only for jobs that will be judged
  status        TEXT NOT NULL,       -- baseline (open before we started watching) | new | later: stale, filtered, matched, rejected, error, expired
  score         INTEGER,
  verdict       TEXT,                -- JSON from the LLM judge
  resume_id     INTEGER,
  apply_status  TEXT,                -- queued | opened | applied | skipped
  applied_at    TEXT,
  UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

-- One row per decision-model request, for the cost view.
CREATE TABLE IF NOT EXISTS llm_calls (
  id            INTEGER PRIMARY KEY,
  at            TEXT NOT NULL DEFAULT (datetime('now')),
  kind          TEXT NOT NULL,       -- judge | extract
  model         TEXT NOT NULL,
  in_tokens     INTEGER,
  out_tokens    INTEGER,
  cached_tokens INTEGER,
  provider      TEXT,                -- jev | openai
  questions     INTEGER,             -- extract: links asked about; judge: questions about the one job
  usd           REAL                 -- at decide.PRICES when the call was made
);

-- The model's answer per link on a company-built page: a changed page only asks about links it hasn't shown before.
CREATE TABLE IF NOT EXISTS link_answers (
  key      TEXT PRIMARY KEY,         -- sha1(page, url, text)[:16]
  p        REAL NOT NULL,            -- probability the link is a job opening
  seen_at  TEXT NOT NULL
) WITHOUT ROWID;

-- The active resume is what jobs are judged against; uploading a new one re-judges recent jobs.
CREATE TABLE IF NOT EXISTS resumes (
  id          INTEGER PRIMARY KEY,
  uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
  filename    TEXT NOT NULL,
  pdf         BLOB,
  text        TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1
);

-- Editable from the Settings page; values are JSON. Word lists match whole words, case-insensitively.
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings (key, value) VALUES
  ('interval_min',   '60'),
  ('max_age_days',   '2'),
  ('retention_days', '7'),
  ('min_score',      '35'),   -- queue a job when the judge's fit probability is >= this % (rule: judge.wanted)
  ('title_include',  '["engineer", "engineering", "developer", "sde", "software", "backend", "back-end", "frontend", "front-end", "full stack", "full-stack", "fullstack", "data", "machine learning", "ml", "ai", "genai", "llm", "devops", "sre", "cloud", "platform", "python", "programmer", "technical staff", "mts", "intern", "internship", "graduate", "trainee", "fresher", "research"]'),
  ('title_exclude',  '["senior", "sr", "principal", "lead", "manager", "director", "head", "architect", "vp", "vice president", "sales", "presales", "pre-sales", "account executive", "recruiter", "ii", "iii", "iv", "sde 2", "sde2", "sde 3", "sde3", "swe 2", "swe 3", "engineer 2", "engineer 3", "developer 2", "developer 3"]');
