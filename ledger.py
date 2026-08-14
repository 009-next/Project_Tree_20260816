"""SQLite アクセス層。DB操作はここに集約する（rules.md）。"""

import json
import sqlite3
from pathlib import Path

from paths import app_dir

DB_PATH = app_dir() / "ledger.db"

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
  doc_id          TEXT PRIMARY KEY,
  content_sha256  TEXT NOT NULL UNIQUE,
  source_path     TEXT NOT NULL,
  source_type     TEXT NOT NULL CHECK(source_type IN ('minutes','email','report','memo','attachment')),
  title           TEXT NOT NULL,
  occurred_at     TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  author          TEXT,
  participants    TEXT,
  text            TEXT NOT NULL,
  line_count      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id             TEXT PRIMARY KEY,
  started_at         TEXT NOT NULL,
  finished_at        TEXT,
  stage              TEXT NOT NULL CHECK(stage IN ('extract','thread','pattern','insight','verbalize','reconcile')),
  model              TEXT NOT NULL,
  prompt_version     TEXT NOT NULL,
  parent_run_id      TEXT REFERENCES runs(run_id),
  doc_count          INTEGER NOT NULL DEFAULT 0,
  event_count        INTEGER NOT NULL DEFAULT 0,
  rejected_count     INTEGER NOT NULL DEFAULT 0,
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd           REAL NOT NULL DEFAULT 0,
  batch_id           TEXT
);

CREATE TABLE IF NOT EXISTS threads (
  thread_id  TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  aliases    TEXT,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  status     TEXT NOT NULL CHECK(status IN ('active','dormant','closed'))
);

CREATE TABLE IF NOT EXISTS events (
  event_id       TEXT PRIMARY KEY,
  doc_id         TEXT NOT NULL REFERENCES documents(doc_id),
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  occurred_on    TEXT NOT NULL,
  date_precision TEXT NOT NULL CHECK(date_precision IN ('exact','month','quarter','unknown')),
  thread_id      TEXT REFERENCES threads(thread_id),
  thread_hint    TEXT,
  kind           TEXT NOT NULL CHECK(kind IN
                   ('decision','issue','request','completion','concern',
                    'schedule_change','cost_change','info')),
  summary        TEXT NOT NULL,
  detail         TEXT,
  actors         TEXT,
  targets        TEXT,
  mag_value      REAL,
  mag_unit       TEXT,
  span_start     INTEGER NOT NULL,
  span_end       INTEGER NOT NULL,
  span_quote     TEXT NOT NULL,
  certainty      TEXT NOT NULL CHECK(certainty IN ('explicit','inferred')),
  status         TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','superseded')),
  supersedes     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id, occurred_on);
CREATE INDEX IF NOT EXISTS idx_events_kind   ON events(kind, occurred_on);
CREATE INDEX IF NOT EXISTS idx_events_run    ON events(run_id);

CREATE TABLE IF NOT EXISTS objects (
  object_id TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  aliases   TEXT,
  kind      TEXT
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id       TEXT PRIMARY KEY,
  doc_id         TEXT NOT NULL REFERENCES documents(doc_id),
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  object_id      TEXT REFERENCES objects(object_id),
  object_hint    TEXT,
  object_surface TEXT NOT NULL,
  attribute      TEXT NOT NULL,
  value_raw      TEXT NOT NULL,
  value_norm     TEXT,
  unit           TEXT,
  effective_on   TEXT,
  revision       TEXT,
  scope          TEXT,
  span_start     INTEGER NOT NULL,
  span_end       INTEGER NOT NULL,
  span_quote     TEXT NOT NULL,
  certainty      TEXT NOT NULL CHECK(certainty IN ('explicit','inferred'))
);
CREATE INDEX IF NOT EXISTS idx_claims_obj ON claims(object_id, attribute);

CREATE TABLE IF NOT EXISTS discrepancies (
  discrepancy_id   TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  object_id        TEXT NOT NULL REFERENCES objects(object_id),
  attribute        TEXT NOT NULL,
  detection_method TEXT NOT NULL CHECK(detection_method IN ('rule','llm')),
  status           TEXT NOT NULL CHECK(status IN ('open','benign','resolved')),
  claim_ids        TEXT NOT NULL,
  newest_claim_id  TEXT REFERENCES claims(claim_id),
  stale_claim_ids  TEXT,
  benign_reason    TEXT CHECK(benign_reason IN ('scope_differs','revision_ordered','conditional')),
  explanation      TEXT,
  CHECK (json_array_length(claim_ids) >= 2),
  -- newest_claim_id が必須なのは rule レーンかつ status='open' のときだけ。
  -- scope 違い等で自動的に benign と判定された場合は「最新」という概念自体が無く、null を許す。
  CHECK (detection_method <> 'rule' OR explanation IS NULL),
  CHECK (detection_method <> 'rule' OR status <> 'open' OR newest_claim_id IS NOT NULL),
  CHECK (detection_method <> 'llm'  OR explanation IS NOT NULL),
  CHECK (status <> 'benign' OR benign_reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS insights (
  insight_id       TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  generated_at     TEXT NOT NULL,
  thread_id        TEXT REFERENCES threads(thread_id),
  detection_method TEXT NOT NULL CHECK(detection_method IN ('rule','llm')),
  type             TEXT NOT NULL CHECK(type IN ('observation','pattern','forecast','advice')),
  label            TEXT NOT NULL CHECK(label IN ('fact','interpretation','prediction')),
  statement        TEXT NOT NULL,
  basis_event_ids  TEXT NOT NULL,
  pattern_type     TEXT CHECK(pattern_type IN ('recurrence','chain')),
  pattern_evidence TEXT,
  basis_rule       TEXT,
  horizon          TEXT,
  CHECK (detection_method <> 'rule' OR
         (pattern_type IS NOT NULL AND pattern_evidence IS NOT NULL
          AND json_array_length(basis_event_ids) >= 2)),
  CHECK (detection_method <> 'llm' OR
         (basis_rule IS NOT NULL AND horizon IS NOT NULL
          AND json_array_length(basis_event_ids) >= 2))
);

CREATE TABLE IF NOT EXISTS gaps (
  gap_id          TEXT PRIMARY KEY,
  thread_id       TEXT NOT NULL REFERENCES threads(thread_id),
  kind            TEXT NOT NULL CHECK(kind IN ('no_record_after','unanswered_request','unresolved_issue')),
  period_start    TEXT NOT NULL,
  period_end      TEXT,
  anchor_event_id TEXT REFERENCES events(event_id),
  description     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_refs (
  image_id        TEXT PRIMARY KEY,
  doc_id          TEXT NOT NULL REFERENCES documents(doc_id),
  at_line         INTEGER NOT NULL,
  media_type      TEXT NOT NULL,
  verbalized_text TEXT NOT NULL,
  verbalized_at   TEXT NOT NULL,
  run_id          TEXT NOT NULL REFERENCES runs(run_id)
);

-- ここから Project_Tree (Enhancement.md) 用の追加テーブル。
-- 既存テーブルは一切変更しない。IF NOT EXISTS なので既存 ledger.db にも安全に適用できる。

-- ストーリーテリングの5段階（Enhancement.md 2-2）
CREATE TABLE IF NOT EXISTS stages (
  stage_id   TEXT PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads(thread_id),
  stage_no   INTEGER NOT NULL CHECK(stage_no BETWEEN 1 AND 5),
  title      TEXT NOT NULL,
  summary    TEXT,
  method     TEXT NOT NULL CHECK(method IN ('rule','llm')),
  created_at TEXT NOT NULL,
  UNIQUE(thread_id, stage_no)
);

-- 段階と既存イベントの紐付け（段階→時系列→原文をたどる経路）
CREATE TABLE IF NOT EXISTS stage_events (
  stage_id TEXT NOT NULL REFERENCES stages(stage_id),
  event_id TEXT NOT NULL REFERENCES events(event_id),
  PRIMARY KEY (stage_id, event_id)
);

-- 2D/3Dモデルの部位と段階の対応（Enhancement.md 2-4）
CREATE TABLE IF NOT EXISTS model_parts (
  part_id     TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL REFERENCES threads(thread_id),
  part_key    TEXT NOT NULL,            -- GLB/SVG 内の要素名。クリック時の同定キー
  name        TEXT NOT NULL,
  shape       TEXT NOT NULL CHECK(shape IN ('box','cylinder','slope')),
  size_json   TEXT NOT NULL,            -- [x,y,z]
  pos_json    TEXT NOT NULL,            -- [x,y,z]
  stage_no    INTEGER NOT NULL CHECK(stage_no BETWEEN 1 AND 5),
  params_json TEXT,                     -- slope_ratio 等の形状固有パラメータ
  source      TEXT NOT NULL DEFAULT 'local' CHECK(source IN ('local','llm','meshy')),
  UNIQUE(thread_id, part_key)
);

-- 生成した資料用画像・2D/3Dモデルの実体
CREATE TABLE IF NOT EXISTS assets (
  asset_id     TEXT PRIMARY KEY,
  thread_id    TEXT NOT NULL REFERENCES threads(thread_id),
  stage_no     INTEGER CHECK(stage_no BETWEEN 1 AND 5),
  kind         TEXT NOT NULL CHECK(kind IN ('image','2d','3d')),
  fmt          TEXT NOT NULL,           -- png / svg / dxf / glb / stl
  path         TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'local' CHECK(source IN ('local','llm','meshy'))
);
CREATE INDEX IF NOT EXISTS idx_assets_thread ON assets(thread_id, kind, stage_no);
"""


class Ledger:
    def __init__(self, db_path: Path = DB_PATH):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def init_db(self):
        self.conn.executescript(DDL)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- documents ---

    def document_exists(self, content_sha256: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE content_sha256 = ?", (content_sha256,)
        ).fetchone()
        return row is not None

    def insert_document(self, doc: dict):
        self.conn.execute(
            """INSERT INTO documents
               (doc_id, content_sha256, source_path, source_type, title,
                occurred_at, ingested_at, author, participants, text, line_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc["doc_id"], doc["content_sha256"], doc["source_path"], doc["source_type"],
                doc["title"], doc["occurred_at"], doc["ingested_at"], doc.get("author"),
                json.dumps(doc.get("participants", []), ensure_ascii=False),
                doc["text"], doc["line_count"],
            ),
        )
        self.conn.commit()

    def get_document(self, doc_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()

    def all_documents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents ORDER BY occurred_at").fetchall()

    def documents_without_events(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT d.* FROM documents d
               WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.doc_id = d.doc_id)
               ORDER BY d.occurred_at"""
        ).fetchall()

    # --- runs ---

    def insert_run(self, run: dict):
        self.conn.execute(
            """INSERT INTO runs
               (run_id, started_at, finished_at, stage, model, prompt_version, parent_run_id,
                doc_count, event_count, rejected_count, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, cost_usd, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run["run_id"], run["started_at"], run.get("finished_at"), run["stage"],
                run["model"], run["prompt_version"], run.get("parent_run_id"),
                run.get("doc_count", 0), run.get("event_count", 0), run.get("rejected_count", 0),
                run.get("input_tokens", 0), run.get("output_tokens", 0),
                run.get("cache_read_tokens", 0), run.get("cache_write_tokens", 0),
                run.get("cost_usd", 0.0), run.get("batch_id"),
            ),
        )
        self.conn.commit()

    def all_runs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()

    def update_run(self, run_id: str, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {cols} WHERE run_id = ?", (*fields.values(), run_id))
        self.conn.commit()

    # --- events ---

    def insert_event(self, ev: dict):
        self.conn.execute(
            """INSERT INTO events
               (event_id, doc_id, run_id, occurred_on, date_precision, thread_id, thread_hint,
                kind, summary, detail, actors, targets, mag_value, mag_unit,
                span_start, span_end, span_quote, certainty, status, supersedes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ev["event_id"], ev["doc_id"], ev["run_id"], ev["occurred_on"], ev["date_precision"],
                ev.get("thread_id"), ev.get("thread_hint"), ev["kind"], ev["summary"], ev.get("detail"),
                json.dumps(ev.get("actors", []), ensure_ascii=False),
                json.dumps(ev.get("targets", []), ensure_ascii=False),
                ev.get("mag_value"), ev.get("mag_unit"),
                ev["span_start"], ev["span_end"], ev["span_quote"], ev["certainty"],
                ev.get("status", "open"), json.dumps(ev.get("supersedes", []), ensure_ascii=False),
            ),
        )

    def all_events(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events ORDER BY occurred_on").fetchall()

    # --- claims ---

    def insert_claim(self, c: dict):
        self.conn.execute(
            """INSERT INTO claims
               (claim_id, doc_id, run_id, object_id, object_hint, object_surface, attribute,
                value_raw, value_norm, unit, effective_on, revision, scope,
                span_start, span_end, span_quote, certainty)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                c["claim_id"], c["doc_id"], c["run_id"], c.get("object_id"), c.get("object_hint"),
                c["object_surface"], c["attribute"], c["value_raw"], c.get("value_norm"), c.get("unit"),
                c.get("effective_on"), c.get("revision"), c.get("scope"),
                c["span_start"], c["span_end"], c["span_quote"], c["certainty"],
            ),
        )

    def all_claims(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM claims ORDER BY doc_id").fetchall()

    def commit(self):
        self.conn.commit()
