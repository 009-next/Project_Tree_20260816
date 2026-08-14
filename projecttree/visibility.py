"""案件の表示・非表示（デモ用の絞り込み）。

発表では橋梁工事と護岸工事だけを見せたい、という要求。
台帳のデータは消さずに、画面に出す案件だけを選べるようにする。

方針:
  - データは一切消さない。表示するかどうかだけを別テーブルに持つ。
  - 既定は「全部表示」。設定を入れたときだけ絞り込まれる。
    したがって、この機能を使わなければ今までと同じ挙動になる。
  - 絞り込みは画面の一覧にだけ効かせる。案件を直接指定した API
    （資料出力・推論など）は従来どおり動く。デモ中に裏で使うことがあるため。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS thread_visibility (
  thread_id  TEXT PRIMARY KEY,
  hidden     INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""


def ensure_tables(ledger: Ledger) -> None:
    ledger.conn.executescript(DDL)
    ledger.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hidden_ids(ledger: Ledger) -> set[str]:
    """非表示にされている案件。テーブルが無ければ空（＝全部表示）。"""
    try:
        ensure_tables(ledger)
        return {r["thread_id"] for r in ledger.conn.execute(
            "SELECT thread_id FROM thread_visibility WHERE hidden = 1").fetchall()}
    except Exception:
        return set()


def set_hidden(ledger: Ledger, thread_ids: list[str], hidden: bool) -> dict:
    """指定した案件の表示・非表示を切り替える。"""
    ensure_tables(ledger)
    now = _now()
    n = 0
    for tid in thread_ids:
        ledger.conn.execute(
            "INSERT INTO thread_visibility (thread_id, hidden, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET hidden = excluded.hidden, "
            "updated_at = excluded.updated_at",
            (tid, 1 if hidden else 0, now))
        n += 1
    ledger.commit()
    return {"status": "ok", "changed": n, "hidden": hidden}


def show_all(ledger: Ledger) -> dict:
    """絞り込みを解除して全案件を表示に戻す。"""
    ensure_tables(ledger)
    n = ledger.conn.execute("UPDATE thread_visibility SET hidden = 0 WHERE hidden = 1").rowcount
    ledger.commit()
    return {"status": "ok", "restored": n}


def keep_only(ledger: Ledger, keep_ids: list[str]) -> dict:
    """指定した案件だけを表示し、他をすべて非表示にする（デモ用の一括設定）。"""
    ensure_tables(ledger)
    keep = set(keep_ids)
    now = _now()
    shown = hidden = 0
    for r in ledger.conn.execute("SELECT thread_id FROM threads").fetchall():
        tid = r["thread_id"]
        h = 0 if tid in keep else 1
        ledger.conn.execute(
            "INSERT INTO thread_visibility (thread_id, hidden, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET hidden = excluded.hidden, "
            "updated_at = excluded.updated_at", (tid, h, now))
        if h:
            hidden += 1
        else:
            shown += 1
    ledger.commit()
    return {"status": "ok", "shown": shown, "hidden": hidden}


def keep_by_keywords(ledger: Ledger, keywords: list[str]) -> dict:
    """案件名にキーワードを含むものだけを表示する。

    デモで「橋梁」「護岸」だけ見せたい、という使い方を想定している。
    """
    if not keywords:
        return show_all(ledger)
    rows = ledger.conn.execute("SELECT thread_id, name FROM threads").fetchall()
    keep = [r["thread_id"] for r in rows
            if any(k for k in keywords if k and k in (r["name"] or ""))]
    res = keep_only(ledger, keep)
    res["keywords"] = keywords
    res["kept_names"] = [r["name"] for r in rows if r["thread_id"] in set(keep)]
    return res


def status(ledger: Ledger) -> dict:
    """今どれが表示されているか。"""
    ensure_tables(ledger)
    rows = ledger.conn.execute(
        "SELECT t.thread_id, t.name, COALESCE(v.hidden, 0) AS hidden "
        "FROM threads t LEFT JOIN thread_visibility v ON v.thread_id = t.thread_id "
        "ORDER BY t.name").fetchall()
    shown = [dict(r) for r in rows if not r["hidden"]]
    hidden = [dict(r) for r in rows if r["hidden"]]
    return {"total": len(rows), "shown": len(shown), "hidden": len(hidden),
            "filtered": bool(hidden),
            "shown_names": [r["name"] for r in shown][:20],
            "hidden_names": [r["name"] for r in hidden][:20]}
