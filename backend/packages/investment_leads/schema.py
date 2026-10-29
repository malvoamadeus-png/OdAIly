from __future__ import annotations

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS investment_leads_meta (
  singleton_key TEXT PRIMARY KEY CHECK(singleton_key='global'),
  capture_started_at TEXT NOT NULL,
  worker_last_seen_at TEXT,
  last_maintenance_at TEXT,
  model_error TEXT
);
CREATE TABLE IF NOT EXISTS investment_lead_materials (
  material_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  posted_at TEXT,
  activity_at TEXT,
  activity_time_complete INTEGER NOT NULL DEFAULT 0,
  relation TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  first_observed_at TEXT NOT NULL,
  last_observed_at TEXT NOT NULL,
  completeness TEXT NOT NULL DEFAULT 'complete',
  PRIMARY KEY(material_id,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_investment_material_recent
  ON investment_lead_materials(posted_at DESC,material_id);
CREATE TABLE IF NOT EXISTS investment_lead_observations (
  material_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  screen_name_lower TEXT NOT NULL REFERENCES hottopic_accounts(screen_name_lower),
  relation TEXT NOT NULL,
  first_observed_at TEXT NOT NULL,
  last_observed_at TEXT NOT NULL,
  PRIMARY KEY(material_id,content_hash,screen_name_lower,relation),
  FOREIGN KEY(material_id,content_hash) REFERENCES investment_lead_materials(material_id,content_hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_investment_observations_account
  ON investment_lead_observations(screen_name_lower,material_id,content_hash);
CREATE TABLE IF NOT EXISTS investment_lead_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('scan','derive','supplement')),
  prompt_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','succeeded','no_candidate','needs_material','budget_paused','ignored','failed','expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  next_attempt_at TEXT,
  actual_model TEXT,
  fallback_reason TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(material_id,content_hash,stage,prompt_version),
  FOREIGN KEY(material_id,content_hash) REFERENCES investment_lead_materials(material_id,content_hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_investment_tasks_due
  ON investment_lead_tasks(stage,status,next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS investment_lead_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  classification TEXT NOT NULL CHECK(classification IN ('driver_candidate','composable','no_candidate','incomplete')),
  subject TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  change_object TEXT NOT NULL DEFAULT '',
  event_time TEXT NOT NULL DEFAULT '',
  impact_relations_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  actual_model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(material_id,content_hash,prompt_version),
  FOREIGN KEY(material_id,content_hash) REFERENCES investment_lead_materials(material_id,content_hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_investment_events_recent
  ON investment_lead_events(created_at DESC,subject);
CREATE TABLE IF NOT EXISTS investment_leads (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  driver_logic TEXT NOT NULL,
  conditions TEXT NOT NULL DEFAULT '',
  assets_json TEXT NOT NULL DEFAULT '[]',
  source_claims_json TEXT NOT NULL DEFAULT '[]',
  first_seen_at TEXT NOT NULL,
  last_substantive_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','unlocated')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_investment_leads_window
  ON investment_leads(last_substantive_at DESC,status);
CREATE TABLE IF NOT EXISTS investment_lead_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id TEXT NOT NULL REFERENCES investment_leads(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  subject TEXT NOT NULL,
  driver_logic TEXT NOT NULL,
  conditions TEXT NOT NULL DEFAULT '',
  assets_json TEXT NOT NULL DEFAULT '[]',
  source_claims_json TEXT NOT NULL DEFAULT '[]',
  source_materials_json TEXT NOT NULL DEFAULT '[]',
  actual_model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(lead_id,revision)
);
CREATE TABLE IF NOT EXISTS investment_lead_sources (
  lead_id TEXT NOT NULL REFERENCES investment_leads(id) ON DELETE CASCADE,
  material_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(lead_id,material_id,content_hash,role),
  FOREIGN KEY(material_id,content_hash) REFERENCES investment_lead_materials(material_id,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_investment_sources_material
  ON investment_lead_sources(material_id,content_hash);
CREATE TABLE IF NOT EXISTS investment_lead_usage (
  usage_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('scan','derive','search','supplement')),
  calls INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(usage_date,kind)
);
CREATE TABLE IF NOT EXISTS investment_lead_identity_searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id TEXT NOT NULL REFERENCES investment_leads(id) ON DELETE CASCADE,
  asset_key TEXT NOT NULL,
  asset_name TEXT NOT NULL,
  context_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','succeeded','unresolved','budget_paused','ignored','failed','expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  next_attempt_at TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(lead_id,asset_key)
);
CREATE INDEX IF NOT EXISTS idx_investment_identity_search_due
  ON investment_lead_identity_searches(status,next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS investment_lead_scheduler_cursor (
  stage TEXT PRIMARY KEY CHECK(stage IN ('scan','derive','identity_search')),
  account_lower TEXT NOT NULL
);
"""


def ensure_schema(connection: sqlite3.Connection, *, capture_started_at: str) -> None:
    connection.executescript(SCHEMA)
    meta_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(investment_leads_meta)").fetchall()}
    if "model_error" not in meta_columns:
        connection.execute("ALTER TABLE investment_leads_meta ADD COLUMN model_error TEXT")
    connection.execute(
        "INSERT OR IGNORE INTO investment_leads_meta(singleton_key,capture_started_at) VALUES('global',?)",
        (capture_started_at,),
    )
