"""資料と案件の対応表。

intake.py の intake_file() は thread_id を引数に取るが、実際には使っていない
（documents テーブルに案件の列が無い）。そのため「この案件に入れる」という
利用者の指定は、これまで記録されず消えていた。

一方 threader.py は資料の中身だけを見て案件を推定するので、
名前の似た別案件へ引き寄せられることがある。実際、フォルダ名で
「第二排水路改修工事」と明示して入れた資料が「農業用水路改修工事」へ
割り当てられた。

そこで、取り込み時点の指定を別テーブルに残しておき、threader が推定した
あとで上書きする。人が明示した所属は、機械の推定より優先されるべきなので。

intake.py / threader.py は変更しない。ここは対応表と補正だけを持つ。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS document_threads (
  doc_id      TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  source      TEXT NOT NULL,      -- どの経路で指定されたか（folder / intake）
  assigned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_threads ON document_threads(thread_id);
"""


def ensure_tables(ledger: Ledger) -> None:
    ledger.conn.executescript(DDL)
    ledger.commit()


def remember(ledger: Ledger, doc_id: str, thread_id: str, source: str = "intake") -> None:
    """「この資料はこの案件のもの」という指定を残す。"""
    if not doc_id or not thread_id:
        return
    ensure_tables(ledger)
    ledger.conn.execute(
        "INSERT INTO document_threads (doc_id, thread_id, source, assigned_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(doc_id) DO UPDATE SET thread_id = excluded.thread_id, "
        "source = excluded.source, assigned_at = excluded.assigned_at",
        (doc_id, thread_id, source,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    ledger.commit()


def reassign(ledger: Ledger, thread_id: str | None = None) -> dict:
    """対応表に従って events.thread_id を直す。

    thread_id を指定するとその案件ぶんだけ、省略すると全件を対象にする。
    threader を走らせたあとに呼ぶ。
    """
    ensure_tables(ledger)
    sql = ("SELECT dt.doc_id, dt.thread_id FROM document_threads dt "
           "WHERE EXISTS (SELECT 1 FROM documents d WHERE d.doc_id = dt.doc_id)")
    params: list = []
    if thread_id:
        sql += " AND dt.thread_id = ?"
        params.append(thread_id)

    moved = 0
    docs = 0
    for r in ledger.conn.execute(sql, params).fetchall():
        cur = ledger.conn.execute(
            "UPDATE events SET thread_id = ? WHERE doc_id = ? "
            "AND (thread_id IS NULL OR thread_id <> ?)",
            (r["thread_id"], r["doc_id"], r["thread_id"]))
        if cur.rowcount:
            moved += cur.rowcount
            docs += 1
    if moved:
        ledger.commit()
        _refresh_span(ledger, thread_id)
    return {"moved_events": moved, "documents": docs}


def _refresh_span(ledger: Ledger, thread_id: str | None) -> None:
    """記録を移したぶん、案件の期間表示がずれるので取り直す。"""
    ids = ([thread_id] if thread_id else
           [r["thread_id"] for r in ledger.conn.execute(
               "SELECT DISTINCT thread_id FROM document_threads").fetchall()])
    for tid in ids:
        span = ledger.conn.execute(
            "SELECT MIN(occurred_on) a, MAX(occurred_on) b FROM events WHERE thread_id = ?",
            (tid,)).fetchone()
        if span and span["a"]:
            ledger.conn.execute(
                "UPDATE threads SET first_seen = ?, last_seen = ? WHERE thread_id = ?",
                (span["a"], span["b"], tid))
    ledger.commit()


def listing(ledger: Ledger, thread_id: str) -> list[dict]:
    ensure_tables(ledger)
    rows = ledger.conn.execute(
        "SELECT dt.doc_id, dt.source, dt.assigned_at, d.title "
        "FROM document_threads dt JOIN documents d ON d.doc_id = dt.doc_id "
        "WHERE dt.thread_id = ? ORDER BY dt.assigned_at", (thread_id,)).fetchall()
    return [dict(r) for r in rows]
